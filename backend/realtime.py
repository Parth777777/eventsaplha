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
import queue
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Iterable, List, Optional

from flask import Blueprint, Response, request, stream_with_context


# ---- IN-PROCESS PUB/SUB ----------------------------------------------------

_SUBSCRIBERS: List["Subscriber"] = []
_SUBS_LOCK = threading.Lock()
_RECENT: Deque[Dict] = deque(maxlen=200)   # last 200 events for replay-on-connect
_RECENT_LOCK = threading.Lock()


class Subscriber:
    """One connected SSE client."""
    def __init__(self, channels: Iterable[str], filters: Optional[Dict] = None):
        self.channels = set(channels) if channels else {"news", "scored", "alert"}
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


def _broadcast(event: Dict):
    """Fan out an event to matching subscribers + recent buffer."""
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


# ---- PUBLIC API (called by scraper / api endpoints) ------------------------

def broadcast_raw(event: Dict):
    """Stage 1: fire immediately on ingest. Card appears as 'raw' in UI."""
    payload = {
        "channel": "news",
        "stage": "raw",
        "ts": time.time(),
        **event,
    }
    _broadcast(payload)


def broadcast_scored(event: Dict):
    """Stage 2: fires after forensic + alpha scoring. UI upgrades same card."""
    payload = {
        "channel": "scored",
        "stage": "scored",
        "ts": time.time(),
        **event,
    }
    _broadcast(payload)


def broadcast_alert(alert: Dict):
    """Smart-alert hits broadcast on the alert channel."""
    payload = {"channel": "alert", "ts": time.time(), **alert}
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
    return {
        "subscribers": len(subs),
        "recent_buffer": recent_count,
        "channels_breakdown": _channel_breakdown(),
        "oldest_subscriber_age_secs": (
            int(time.time() - min((s.connected_at for s in subs), default=time.time()))
            if subs else 0
        ),
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
        channels=news,scored,alert  (default: all three)
        ticker=RELIANCE              (optional filter)
        min_alpha=70                 (optional filter)
        forensic_band=clean          (optional filter)
        replay=20                    (replay last N matching events on connect)
    """
    channels = (request.args.get("channels", "news,scored,alert") or "").split(",")
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


def register(app, get_db: Optional[Callable] = None):
    """Mount the blueprint onto the Flask app. Idempotent."""
    if "realtime" not in app.blueprints:
        app.register_blueprint(bp)
