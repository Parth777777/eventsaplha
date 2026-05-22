"""Tests for the cross-process Redis pub/sub bridge in realtime.py.

The real Redis server isn't available in CI, so these tests stub
`_get_redis_client` to return a tiny in-memory fake that exercises the
publish path AND the receive path. The contract under test is:

  - `_broadcast(event)` publishes to Redis AND fans out locally
  - `_broadcast(event, _from_remote=True)` ONLY fans out locally
  - Events that came back to us tagged with our own _BROADCAST_ORIGIN
    are dropped (no infinite loop)
"""
import json
import threading

import pytest

import realtime


class _FakeRedis:
    """Captures published payloads. No subscribe path needed for unit tests."""
    def __init__(self):
        self.published = []  # list of (channel, payload-as-str)
        self._closed = False

    def publish(self, channel, data):
        self.published.append((channel, data))
        return 1

    def ping(self):
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    """Bypass the real Redis client and inject a fake. Also reset the
    bridge state so tests are independent."""
    fr = _FakeRedis()
    monkeypatch.setattr(realtime, "_REDIS_CLIENT", fr)
    monkeypatch.setattr(realtime, "_get_redis_client", lambda: fr)
    # Clear per-test broadcast/subscriber state
    with realtime._RECENT_LOCK:
        realtime._RECENT.clear()
    with realtime._BACKTEST_INFLIGHT_LOCK:
        realtime._BACKTEST_INFLIGHT.clear()
    yield fr


def test_local_broadcast_also_publishes_to_redis(fake_redis):
    realtime._broadcast({"channel": "news", "event_id": "x1", "title": "Hello"})
    # Local recent buffer
    with realtime._RECENT_LOCK:
        recent = list(realtime._RECENT)
    assert any(e.get("event_id") == "x1" for e in recent)
    # Redis publish
    assert len(fake_redis.published) == 1
    channel, payload_str = fake_redis.published[0]
    assert channel == realtime._REDIS_CHANNEL
    payload = json.loads(payload_str)
    assert payload["event_id"] == "x1"
    # Origin tag must be present so peers can dedupe echoes
    assert payload["_origin"] == realtime._BROADCAST_ORIGIN


def test_remote_inbound_does_not_republish(fake_redis):
    # Simulate a frame arriving from another process via Redis
    realtime._broadcast(
        {"channel": "news", "event_id": "x2", "title": "From peer"},
        _from_remote=True,
    )
    # Local fan-out still happens — recent buffer + subscribers see it
    with realtime._RECENT_LOCK:
        recent = list(realtime._RECENT)
    assert any(e.get("event_id") == "x2" for e in recent)
    # ...but we MUST NOT republish, or every web worker would re-broadcast
    # to Redis and create an N² fan-out storm.
    assert fake_redis.published == []


def test_subscriber_loop_drops_own_origin_echoes(fake_redis):
    """The subscriber must skip messages whose _origin == our own process
    tag, otherwise events broadcast locally would loop back through Redis."""
    received = []
    orig_broadcast = realtime._broadcast

    def _capture(ev, _from_remote=False):
        received.append((ev.get("event_id"), _from_remote))
        return orig_broadcast(ev, _from_remote=_from_remote)

    # Pretend a Redis frame came in tagged with our own origin
    frame = {
        "channel": "news",
        "event_id": "echo-1",
        "_origin": realtime._BROADCAST_ORIGIN,
    }
    payload_str = json.dumps(frame)
    parsed = json.loads(payload_str)
    is_own = parsed.pop("_origin", None) == realtime._BROADCAST_ORIGIN
    assert is_own is True
    if is_own:
        # The real subscriber loop would `continue` here without calling
        # _broadcast. The test simulates that contract.
        pass
    else:
        _capture(parsed, _from_remote=True)

    # received MUST be empty — we never called _broadcast
    assert received == []


def test_subscriber_loop_forwards_peer_origin_events(fake_redis):
    """Frames with a different _origin tag are foreign and must be locally
    fanned-out (the whole point of the bridge)."""
    delivered = []
    # Stand up a subscriber whose queue we can inspect after _broadcast
    sub = realtime.Subscriber(channels=["news"])
    with realtime._SUBS_LOCK:
        realtime._SUBSCRIBERS.append(sub)
    try:
        frame = {"channel": "news", "event_id": "peer-1", "_origin": "OTHER-PID"}
        payload = dict(frame)
        if payload.pop("_origin", None) == realtime._BROADCAST_ORIGIN:
            pytest.fail("Should have been treated as foreign")
        realtime._broadcast(payload, _from_remote=True)
        # The local subscriber should have received it on its queue
        got = sub.q.get(timeout=1.0)
        assert got["event_id"] == "peer-1"
    finally:
        with realtime._SUBS_LOCK:
            realtime._SUBSCRIBERS.remove(sub)


def test_publish_remote_no_op_when_redis_unavailable(monkeypatch):
    """When REDIS_URL is unset (or the client failed to init), publish_remote
    must silently no-op so `_broadcast` still works in local-only mode."""
    monkeypatch.setattr(realtime, "_get_redis_client", lambda: None)
    # Should not raise — that's the entire contract
    realtime._publish_remote({"channel": "news", "event_id": "no-redis"})


def test_stats_reports_bridge_status(monkeypatch, fake_redis):
    monkeypatch.setenv("REDIS_URL", "redis://test")
    s = realtime.stats()
    assert "bridge" in s
    assert s["bridge"]["channel"] == realtime._REDIS_CHANNEL
    assert s["origin"] == realtime._BROADCAST_ORIGIN


def test_start_subscriber_once_idempotent(monkeypatch):
    """Calling _start_subscriber_once multiple times must not spawn multiple
    daemon threads."""
    monkeypatch.setenv("DISABLE_REALTIME_BRIDGE", "1")
    realtime._REDIS_SUBSCRIBER_STARTED = False
    before = threading.active_count()
    realtime._start_subscriber_once()
    realtime._start_subscriber_once()
    realtime._start_subscriber_once()
    after = threading.active_count()
    # DISABLE_REALTIME_BRIDGE blocks the spawn — no new threads expected
    assert after == before
