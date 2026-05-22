"""Real-time push pipeline: in-process pub/sub + SSE endpoint.

Why this exists:
- The frontend used to poll /api/signals every 30s. Median time-to-user was
  ~25s in the worst case for any new event. With SSE + in-process pub/sub,
  median drops to <2s on the same scraper cadence.
- Two-stage broadcast: stage 1 ("raw") fires on event ingest before forensic
  scoring; stage 2 ("scored") fires after the slow path completes, upgrading
  the same card in place via the event_id.

Plug points:
- backend/api.py registers the blueprint and stage-1/stage-2 hooks.
- scraper/hybrid_scraper.py calls broadcast_raw() on each new article cluster.
- post-forensic enrichment calls broadcast_scored() once alpha + forensic_band
  are available.

Threading: Flask dev server is single-process; for prod, use gevent or
gunicorn-with-gevent. SSE keeps a long-lived response open per subscriber.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

from flask import Blueprint, Response, request, stream_with_context

logger = logging.getLogger(__name__)

# Optional enrichments — both are pure-Python, no extra dependencies beyond
# what's already in requirements.txt (cachetools + yfinance). Imports are
# guarded so a missing module degrades to "no enrichment" rather than crashing
# the entire broadcast path.
try:
    from voice_phrase import compact_from_event as _compact_voice_phrase
except Exception as _vp_err:  # pragma: no cover — import-time defensive
    logger.warning("voice_phrase unavailable, skipping audio enrichment: %s", _vp_err)
    _compact_voice_phrase = None  # type: ignore[assignment]

try:
    from event_backtest import backtest_event as _backtest_event
except Exception as _bt_err:  # pragma: no cover
    logger.warning("event_backtest unavailable, skipping backtest enrichment: %s", _bt_err)
    _backtest_event = None  # type: ignore[assignment]

# Optional: pull historically-similar event dates from the DB so the
# backtest aggregates over N comparable events instead of just T+0 alone.
try:
    from event_history import find_similar_event_dates as _find_similar_event_dates
except Exception as _eh_err:  # pragma: no cover
    logger.debug("event_history unavailable: %s", _eh_err)
    _find_similar_event_dates = None  # type: ignore[assignment]

# Optional: WebSocket transport. Falls back to SSE-only if flask-sock isn't
# installed — useful for local dev environments and CI.
try:
    from flask_sock import Sock  # type: ignore
    _sock: Optional[Sock] = Sock()
except Exception as _ws_err:  # pragma: no cover
    logger.warning("flask-sock unavailable, WebSocket endpoint disabled: %s", _ws_err)
    _sock = None  # type: ignore[assignment]

# Backtest worker pool — yfinance calls are seconds-long and MUST NOT block
# the SSE fan-out. Size is conservative; the cache TTL absorbs repeats.
_BACKTEST_WORKERS = int(os.getenv("BACKTEST_WORKERS", "2"))
_BACKTEST_POOL: Optional[ThreadPoolExecutor] = None
_BACKTEST_POOL_LOCK = threading.Lock()
# Deduplicate in-flight backtest requests so a flood of scored events for the
# same ticker doesn't queue 50 redundant lookups behind the cache miss.
_BACKTEST_INFLIGHT: set = set()
_BACKTEST_INFLIGHT_LOCK = threading.Lock()

# Captured at register() time so the backtest worker can pull similar
# historical events for richer aggregation. None outside a registered app.
_GET_DB: Optional[Callable] = None


def _get_backtest_pool() -> Optional[ThreadPoolExecutor]:
    global _BACKTEST_POOL
    if _backtest_event is None or _BACKTEST_WORKERS <= 0:
        return None
    if _BACKTEST_POOL is None:
        with _BACKTEST_POOL_LOCK:
            if _BACKTEST_POOL is None:
                _BACKTEST_POOL = ThreadPoolExecutor(
                    max_workers=_BACKTEST_WORKERS,
                    thread_name_prefix="backtest",
                )
    return _BACKTEST_POOL


# ---- IN-PROCESS PUB/SUB ----------------------------------------------------

_SUBSCRIBERS: List["Subscriber"] = []
_SUBS_LOCK = threading.Lock()
_RECENT: Deque[Dict] = deque(maxlen=200)   # last 200 events for replay-on-connect
_RECENT_LOCK = threading.Lock()


class Subscriber:
    """One connected SSE client."""
    def __init__(self, channels: Iterable[str], filters: Optional[Dict] = None):
        self.channels = set(channels) if channels else {"news", "scored", "alert", "backtest"}
        self.filters = filters or {}
        self.q: queue.Queue = queue.Queue(maxsize=500)
        self.connected_at = time.time()
        self.dropped = 0

    def matches(self, event: Dict) -> bool:
        if event.get("channel") not in self.channels:
            return False
        # Optional filters: ticker, min_alpha, forensic_band
        f = self.filters
        if "ticker" in f:
            tickers = event.get("tickers") or ([event["ticker"]] if event.get("ticker") else [])
            if f["ticker"] not in tickers:
                return False
        if "min_alpha" in f:
            try:
                if float(event.get("alpha_score", 0) or 0) < float(f["min_alpha"]):
                    return False
            except (TypeError, ValueError):
                return False
        if "forensic_band" in f:
            if event.get("forensic_band") != f["forensic_band"]:
                return False
        return True

    def push(self, event: Dict):
        try:
            self.q.put_nowait(event)
        except queue.Full:
            self.dropped += 1


def _broadcast(event: Dict, _from_remote: bool = False):
    """Fan out an event to matching subscribers + recent buffer.

    If `_from_remote` is False (the default), the event is ALSO published
    onto the Redis bridge so peer processes (e.g. the web container that
    actually has connected clients) can fan it out to their own subscribers.
    Events arriving from Redis pass `_from_remote=True` to break the loop.
    """
    with _RECENT_LOCK:
        _RECENT.append(event)
    with _SUBS_LOCK:
        dead = []
        for sub in _SUBSCRIBERS:
            try:
                if sub.matches(event):
                    sub.push(event)
            except Exception:
                dead.append(sub)
        for d in dead:
            try:
                _SUBSCRIBERS.remove(d)
            except ValueError:
                pass
    if not _from_remote:
        _publish_remote(event)


# ---- CROSS-PROCESS BRIDGE (REDIS PUB/SUB) -----------------------------------
# Why this matters:
#   The scraper runs in the `worker` container (RUN_SCHEDULER=true) but SSE/WS
#   subscribers connect to the `web` container. Without a cross-process bridge,
#   broadcasts in worker fan out to an empty subscriber list and never reach
#   the user. Redis is already in the stack (cache + rate-limit), so it's the
#   natural substrate. Falls back to local-only when REDIS_URL is unset.

_REDIS_CHANNEL = os.getenv("REALTIME_REDIS_CHANNEL", "tw:realtime:events")
_BROADCAST_ORIGIN = f"{os.getpid()}-{int(time.time() * 1000)}"
_REDIS_CLIENT: Any = None
_REDIS_CLIENT_LOCK = threading.Lock()
_REDIS_SUBSCRIBER_STARTED = False
_REDIS_SUBSCRIBER_LOCK = threading.Lock()


def _get_redis_client():
    """Lazy-init a redis-py client. Returns None when REDIS_URL is unset or
    redis-py / the server is unreachable. Cached per-process."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT if _REDIS_CLIENT is not False else None
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        with _REDIS_CLIENT_LOCK:
            _REDIS_CLIENT = False  # cache the "unavailable" verdict
        return None
    with _REDIS_CLIENT_LOCK:
        if _REDIS_CLIENT is not None:
            return _REDIS_CLIENT if _REDIS_CLIENT is not False else None
        try:
            import redis  # type: ignore
            client = redis.from_url(
                url, decode_responses=True, socket_timeout=2,
                socket_connect_timeout=2, health_check_interval=30,
            )
            client.ping()
            _REDIS_CLIENT = client
            logger.info("realtime: Redis bridge active on %s", _REDIS_CHANNEL)
            return client
        except Exception as e:
            logger.warning("realtime: Redis bridge unavailable, local-only fan-out: %s", e)
            _REDIS_CLIENT = False
            return None


def _publish_remote(event: Dict) -> None:
    """Publish an outbound event onto the Redis bridge channel.
    Tags with this process's origin so we can skip our own echoes."""
    client = _get_redis_client()
    if client is None:
        return
    try:
        payload = dict(event)
        payload["_origin"] = _BROADCAST_ORIGIN
        client.publish(_REDIS_CHANNEL, json.dumps(payload, default=str))
    except Exception as e:
        logger.debug("realtime: publish_remote failed: %s", e)


def _subscribe_loop() -> None:
    """Long-running daemon: forwards Redis-bridged events into the local
    pub/sub so SSE/WS clients on this process see them. Reconnects on error."""
    backoff = 1.0
    while True:
        client = _get_redis_client()
        if client is None:
            time.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2, 30.0)
            # Reset the cached "unavailable" verdict so we retry the connection
            global _REDIS_CLIENT
            with _REDIS_CLIENT_LOCK:
                if _REDIS_CLIENT is False:
                    _REDIS_CLIENT = None
            continue
        backoff = 1.0
        try:
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(_REDIS_CHANNEL)
            for message in pubsub.listen():
                if not isinstance(message, dict):
                    continue
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                # Skip our own echoes — _BROADCAST_ORIGIN is unique per process
                if payload.pop("_origin", None) == _BROADCAST_ORIGIN:
                    continue
                _broadcast(payload, _from_remote=True)
        except Exception as e:
            logger.warning("realtime: subscriber loop died, reconnecting: %s", e)
            with _REDIS_CLIENT_LOCK:
                _REDIS_CLIENT = None  # force reconnect on next iteration
            time.sleep(2.0)


def _start_subscriber_once() -> None:
    """Idempotent. Starts the Redis bridge subscriber as a daemon thread.
    Safe to call multiple times (e.g. when register() runs in dev with
    auto-reload). Disabled when DISABLE_REALTIME_BRIDGE=1 (handy for tests)."""
    global _REDIS_SUBSCRIBER_STARTED
    if _REDIS_SUBSCRIBER_STARTED:
        return
    if os.getenv("DISABLE_REALTIME_BRIDGE", "").strip() in ("1", "true", "yes"):
        logger.info("realtime: Redis bridge disabled via DISABLE_REALTIME_BRIDGE")
        return
    with _REDIS_SUBSCRIBER_LOCK:
        if _REDIS_SUBSCRIBER_STARTED:
            return
        _REDIS_SUBSCRIBER_STARTED = True
        t = threading.Thread(
            target=_subscribe_loop, daemon=True, name="realtime-redis-sub",
        )
        t.start()


# ---- PUBLIC API (called by scraper / api endpoints) ------------------------

def _attach_voice_phrase(payload: Dict) -> None:
    """Best-effort: add `cleaned_voice_phrase` for the audio-squawk UI.
    Never raises — a failed compaction simply omits the field."""
    if _compact_voice_phrase is None or "cleaned_voice_phrase" in payload:
        return
    try:
        phrase = _compact_voice_phrase(payload)
        if phrase:
            payload["cleaned_voice_phrase"] = phrase
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("voice_phrase failed: %s", e)


def _schedule_backtest(payload: Dict) -> None:
    """Fire-and-forget backtest lookup → emits a third-stage 'backtest' event.

    Skipped when: no event_backtest module, no ticker, no event_id, or the
    same ticker already has a backtest computing. The 24h TTL cache inside
    event_backtest.py prevents repeat network calls for known tickers.
    """
    pool = _get_backtest_pool()
    if pool is None:
        return
    ticker = payload.get("ticker")
    event_id = payload.get("event_id")
    if not ticker or not event_id:
        return
    event_date = (
        payload.get("published_at")
        or payload.get("timestamp")
        or payload.get("ts")
    )
    if event_date is None:
        return
    if isinstance(event_date, (int, float)):
        # SSE `ts` is a float epoch — convert to ISO date
        import datetime as _dt
        event_date = _dt.datetime.fromtimestamp(event_date, _dt.timezone.utc).date().isoformat()

    key = f"{str(ticker).upper()}|{str(event_date)[:10]}"
    with _BACKTEST_INFLIGHT_LOCK:
        if key in _BACKTEST_INFLIGHT:
            return
        _BACKTEST_INFLIGHT.add(key)

    event_type = payload.get("event_type")
    # Pull sentiment + magnitude off the payload so the historical-similar
    # match is "same OUTCOME" (bullish beat vs bearish miss) not just same
    # event_type. Falls back gracefully when either is missing — the lookup
    # silently skips that filter dimension.
    payload_sentiment = payload.get("sentiment") or payload.get("sentiment_label")
    if payload_sentiment is None:
        # Some payloads carry a numeric sentiment_score; map to directional label.
        try:
            s = float(payload.get("sentiment_score"))
            payload_sentiment = "bullish" if s > 0.15 else ("bearish" if s < -0.15 else "neutral")
        except (TypeError, ValueError):
            payload_sentiment = None
    try:
        payload_magnitude = float(payload.get("magnitude")) if payload.get("magnitude") is not None else None
    except (TypeError, ValueError):
        payload_magnitude = None

    def _run():
        # Try to widen the sample by pulling historically-similar event dates
        # from the events table — falls back gracefully to single-event mode.
        similar: Optional[List[Any]] = None
        if _find_similar_event_dates is not None and _GET_DB is not None and event_type:
            try:
                similar = _find_similar_event_dates(
                    _GET_DB(), ticker, event_type,
                    sentiment=payload_sentiment,
                    magnitude=payload_magnitude,
                    limit=20,
                )
            except Exception as e:  # pragma: no cover — defensive
                logger.debug("similar-event lookup failed: %s", e)
                similar = None
        try:
            summary = _backtest_event(ticker, event_date, similar_dates=similar)
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("backtest_event raised for %s @ %s: %s", ticker, event_date, e)
            return
        finally:
            with _BACKTEST_INFLIGHT_LOCK:
                _BACKTEST_INFLIGHT.discard(key)
        try:
            broadcast_backtest({
                "event_id": event_id,
                "ticker": ticker,
                "backtest_summary": summary,
            })
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("backtest broadcast failed for %s: %s", ticker, e)

    try:
        pool.submit(_run)
    except RuntimeError as e:
        # Pool was shut down — drop silently rather than crash the broadcaster
        logger.debug("backtest pool unavailable: %s", e)
        with _BACKTEST_INFLIGHT_LOCK:
            _BACKTEST_INFLIGHT.discard(key)


def broadcast_raw(event: Dict):
    """Stage 1: fire immediately on ingest. Card appears as 'raw' in UI."""
    payload = {
        "channel": "news",
        "stage": "raw",
        "ts": time.time(),
        **event,
    }
    _attach_voice_phrase(payload)
    _broadcast(payload)


def broadcast_scored(event: Dict):
    """Stage 2: fires after forensic + alpha scoring. UI upgrades same card.

    Also schedules an async event-window backtest; when complete, a third
    "backtest" channel event joins to this card by event_id.
    """
    payload = {
        "channel": "scored",
        "stage": "scored",
        "ts": time.time(),
        **event,
    }
    _attach_voice_phrase(payload)
    _broadcast(payload)
    _schedule_backtest(payload)


def broadcast_backtest(event: Dict):
    """Stage 3: event-window backtest result for a previously-broadcast card."""
    payload = {
        "channel": "backtest",
        "stage": "backtest",
        "ts": time.time(),
        **event,
    }
    _broadcast(payload)


def broadcast_alert(alert: Dict):
    """Smart-alert hits broadcast on the alert channel."""
    payload = {"channel": "alert", "ts": time.time(), **alert}
    _attach_voice_phrase(payload)
    _broadcast(payload)


def get_recent(channel: Optional[str] = None, limit: int = 50) -> List[Dict]:
    with _RECENT_LOCK:
        items = list(_RECENT)
    if channel:
        items = [e for e in items if e.get("channel") == channel]
    return items[-limit:]


def stats() -> Dict:
    with _SUBS_LOCK:
        subs = list(_SUBSCRIBERS)
    with _RECENT_LOCK:
        recent_count = len(_RECENT)
    bridge_state = "off"
    if os.getenv("REDIS_URL", "").strip():
        bridge_state = "active" if _REDIS_CLIENT and _REDIS_CLIENT is not False else "down"
    return {
        "subscribers": len(subs),
        "recent_buffer": recent_count,
        "channels_breakdown": _channel_breakdown(),
        "oldest_subscriber_age_secs": (
            int(time.time() - min((s.connected_at for s in subs), default=time.time()))
            if subs else 0
        ),
        "origin": _BROADCAST_ORIGIN,
        "bridge": {
            "redis": bridge_state,
            "channel": _REDIS_CHANNEL,
            "subscriber_running": _REDIS_SUBSCRIBER_STARTED,
        },
    }


def _channel_breakdown() -> Dict[str, int]:
    out: Dict[str, int] = {}
    with _RECENT_LOCK:
        for e in _RECENT:
            ch = e.get("channel", "?")
            out[ch] = out.get(ch, 0) + 1
    return out


# ---- FLASK BLUEPRINT --------------------------------------------------------

bp = Blueprint("realtime", __name__)


def _format_sse(event: Dict) -> str:
    data = json.dumps(event, default=str)
    return f"event: {event.get('channel', 'message')}\ndata: {data}\n\n"


@bp.route("/api/stream", methods=["GET"])
def stream():
    """SSE endpoint. Query params:
        channels=news,scored,alert,backtest  (default: all four)
        ticker=RELIANCE                      (optional filter)
        min_alpha=70                         (optional filter)
        forensic_band=clean                  (optional filter)
        replay=20                            (replay last N matching events on connect)
    """
    channels = (request.args.get("channels", "news,scored,alert,backtest") or "").split(",")
    channels = [c.strip() for c in channels if c.strip()]
    filters = {}
    if request.args.get("ticker"):
        filters["ticker"] = request.args["ticker"].upper()
    if request.args.get("min_alpha"):
        try:
            filters["min_alpha"] = float(request.args["min_alpha"])
        except ValueError:
            pass
    if request.args.get("forensic_band"):
        filters["forensic_band"] = request.args["forensic_band"]
    try:
        replay = max(0, min(100, int(request.args.get("replay", "0"))))
    except ValueError:
        replay = 0

    sub = Subscriber(channels, filters)
    with _SUBS_LOCK:
        _SUBSCRIBERS.append(sub)

    @stream_with_context
    def gen():
        try:
            # Replay recent events that match
            if replay:
                with _RECENT_LOCK:
                    recent = list(_RECENT)
                replayed = 0
                for ev in recent[-200:]:
                    if sub.matches(ev) and replayed < replay:
                        yield _format_sse({**ev, "_replay": True})
                        replayed += 1

            # Initial hello so client knows the stream is live
            yield _format_sse({
                "channel": "hello",
                "ts": time.time(),
                "filters": filters,
                "subscribed_to": sorted(sub.channels),
            })

            heartbeat_secs = 20.0
            last_beat = time.time()
            while True:
                try:
                    ev = sub.q.get(timeout=heartbeat_secs)
                    yield _format_sse(ev)
                    last_beat = time.time()
                except queue.Empty:
                    # Comment line keeps proxies/load balancers from closing the connection
                    yield ": heartbeat\n\n"
                    last_beat = time.time()
        finally:
            with _SUBS_LOCK:
                try:
                    _SUBSCRIBERS.remove(sub)
                except ValueError:
                    pass

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # disable nginx buffering
    resp.headers["Connection"] = "keep-alive"
    return resp


@bp.route("/api/stream/recent", methods=["GET"])
def stream_recent():
    """REST fallback: latest N events as JSON. For clients that can't open SSE."""
    from flask import jsonify
    channel = request.args.get("channel")
    try:
        limit = max(1, min(200, int(request.args.get("limit", "50"))))
    except ValueError:
        limit = 50
    return jsonify({"success": True, "data": get_recent(channel, limit)})


@bp.route("/api/stream/stats", methods=["GET"])
def stream_stats():
    from flask import jsonify
    return jsonify({"success": True, "data": stats()})


# ---- WEBSOCKET ENDPOINT -----------------------------------------------------
# The primary live-stream transport. Same payload shape as SSE — every frame
# is one JSON object with a `channel` key. Reuses the in-process pub/sub so
# WS and SSE listeners see identical events.

def _ws_handler(ws):
    """Driver for a single WebSocket connection.

    Query params accepted (same as /api/stream):
        channels=news,scored,alert,backtest
        ticker=RELIANCE
        min_alpha=70
        forensic_band=clean
        replay=20
    """
    # `request` is the Flask request bound to the upgrade
    channels = (request.args.get("channels", "news,scored,alert,backtest") or "").split(",")
    channels = [c.strip() for c in channels if c.strip()]
    filters: Dict[str, Any] = {}
    if request.args.get("ticker"):
        filters["ticker"] = request.args["ticker"].upper()
    if request.args.get("min_alpha"):
        try:
            filters["min_alpha"] = float(request.args["min_alpha"])
        except ValueError:
            pass
    if request.args.get("forensic_band"):
        filters["forensic_band"] = request.args["forensic_band"]
    try:
        replay = max(0, min(100, int(request.args.get("replay", "0"))))
    except ValueError:
        replay = 0

    sub = Subscriber(channels, filters)
    with _SUBS_LOCK:
        _SUBSCRIBERS.append(sub)

    try:
        # Replay matching recent events on connect
        if replay:
            with _RECENT_LOCK:
                recent = list(_RECENT)
            sent = 0
            for ev in recent[-200:]:
                if sub.matches(ev) and sent < replay:
                    try:
                        ws.send(json.dumps({**ev, "_replay": True}, default=str))
                    except Exception:
                        return
                    sent += 1

        # Hello frame
        try:
            ws.send(json.dumps({
                "channel": "hello",
                "ts": time.time(),
                "filters": filters,
                "subscribed_to": sorted(sub.channels),
                "transport": "ws",
            }, default=str))
        except Exception:
            return

        # Main loop — pump queue items as JSON frames, send ping every 20s
        # so idle proxies and stale clients don't drift undetected. simple-
        # websocket auto-replies to pongs; if the peer is dead the next send
        # raises and we tear down.
        heartbeat_secs = 20.0
        last_beat = time.time()
        while True:
            try:
                ev = sub.q.get(timeout=heartbeat_secs)
                try:
                    ws.send(json.dumps(ev, default=str))
                except Exception:
                    return
                last_beat = time.time()
            except queue.Empty:
                try:
                    ws.send(json.dumps({"channel": "heartbeat", "ts": time.time()}, default=str))
                except Exception:
                    return
                last_beat = time.time()
    finally:
        with _SUBS_LOCK:
            try:
                _SUBSCRIBERS.remove(sub)
            except ValueError:
                pass


def register(app, get_db: Optional[Callable] = None):
    """Mount the blueprint + WS endpoint onto the Flask app. Idempotent.

    The optional `get_db` callable is captured so the broadcast-path backtest
    can pull historically-similar event dates for richer aggregation.
    Also starts the Redis pub/sub bridge subscriber so events broadcast by
    other processes (e.g. the scraper worker) are forwarded to this process's
    SSE/WS clients.
    """
    if "realtime" not in app.blueprints:
        app.register_blueprint(bp)
    # Stash the db accessor so _schedule_backtest can find historically-similar events
    if get_db is not None:
        global _GET_DB
        _GET_DB = get_db
    # Mount the WebSocket route if flask-sock is available
    if _sock is not None:
        try:
            _sock.init_app(app)
            # Idempotent route registration — flask-sock raises on duplicate add
            existing = {r.rule for r in app.url_map.iter_rules()}
            if "/ws/stream" not in existing:
                _sock.route("/ws/stream")(_ws_handler)
                logger.info("WebSocket endpoint mounted at /ws/stream")
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("flask-sock init failed (continuing without WS): %s", e)
    # Start the cross-process bridge subscriber. No-op when REDIS_URL is unset.
    _start_subscriber_once()
