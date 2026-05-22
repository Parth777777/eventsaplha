"""Test the broadcast-path enrichments: voice_phrase auto-attach and the
schedule-backtest fire-and-forget hook.

We bypass the SSE machinery and just inspect the buffered _RECENT deque,
which receives every event that's broadcast.
"""
import time

import realtime


def setup_function(_):
    """Reset shared state so tests are independent."""
    with realtime._RECENT_LOCK:
        realtime._RECENT.clear()
    with realtime._BACKTEST_INFLIGHT_LOCK:
        realtime._BACKTEST_INFLIGHT.clear()


def _latest():
    with realtime._RECENT_LOCK:
        return list(realtime._RECENT)[-1]


def test_broadcast_raw_attaches_voice_phrase():
    realtime.broadcast_raw({
        "event_id": "evt_test_1",
        "title": "Tata Power Company Limited bags order worth Rs 5 crore",
        "tickers": ["TATAPOWER.NS"],
        "event_type": "order_win",
    })
    ev = _latest()
    assert "cleaned_voice_phrase" in ev
    assert "Tata Power" in ev["cleaned_voice_phrase"]
    assert "Order Win" in ev["cleaned_voice_phrase"]
    assert "Limited" not in ev["cleaned_voice_phrase"]
    assert ev["channel"] == "news"
    assert ev["stage"] == "raw"


def test_broadcast_scored_attaches_voice_phrase():
    realtime.broadcast_scored({
        "event_id": "evt_test_2",
        "ticker": "RELIANCE.NS",
        "company": "Reliance Industries Limited",
        "headline": "Reliance announces strategic green hydrogen expansion",
        "event_type": "capacity_expansion",
        "alpha_score": 78,
    })
    ev = _latest()
    assert "cleaned_voice_phrase" in ev
    assert "Reliance" in ev["cleaned_voice_phrase"]
    assert "Capacity Expansion" in ev["cleaned_voice_phrase"]
    assert ev["channel"] == "scored"


def test_voice_phrase_preserved_if_caller_already_set_it():
    realtime.broadcast_raw({
        "event_id": "evt_test_3",
        "title": "anything",
        "cleaned_voice_phrase": "Alert. Manual override.",
    })
    ev = _latest()
    assert ev["cleaned_voice_phrase"] == "Alert. Manual override."


def test_schedule_backtest_dedupes_same_ticker_same_day(monkeypatch):
    """Two scored events for the same ticker+day must only schedule one
    backtest job (the second is dropped by the inflight guard)."""
    submitted = []

    class _FakePool:
        def submit(self, fn, *a, **kw):
            submitted.append(fn)
            # don't actually run — we just want to count submissions
            class _F:
                def result(self_inner, *a, **kw): return None
            return _F()

    monkeypatch.setattr(realtime, "_get_backtest_pool", lambda: _FakePool())

    realtime.broadcast_scored({
        "event_id": "a",
        "ticker": "RELIANCE.NS",
        "headline": "first",
        "published_at": "2026-05-19T10:00:00",
    })
    realtime.broadcast_scored({
        "event_id": "b",
        "ticker": "RELIANCE.NS",
        "headline": "second",
        "published_at": "2026-05-19T11:00:00",
    })
    assert len(submitted) == 1  # second was deduped


def test_schedule_backtest_runs_and_broadcasts_third_stage(monkeypatch):
    """End-to-end: scored → backtest job runs → backtest channel event lands."""
    fake_summary = {"sample_events_found": 3, "avg_gap_up_pct": 2.5, "stale": False}
    monkeypatch.setattr(realtime, "_backtest_event", lambda *a, **kw: fake_summary)

    # Run the worker inline so the test is deterministic
    class _InlinePool:
        def submit(self, fn, *a, **kw):
            fn()
            class _F:
                def result(self_inner): return None
            return _F()

    monkeypatch.setattr(realtime, "_get_backtest_pool", lambda: _InlinePool())

    realtime.broadcast_scored({
        "event_id": "evt_async_1",
        "ticker": "INFY.NS",
        "headline": "Q4 results beat",
        "event_type": "earnings",
        "published_at": "2026-05-19T15:00:00",
    })

    time.sleep(0.05)  # give the inline pool a moment

    with realtime._RECENT_LOCK:
        events = list(realtime._RECENT)
    backtest_events = [e for e in events if e.get("channel") == "backtest"]
    assert len(backtest_events) == 1
    bt = backtest_events[0]
    assert bt["event_id"] == "evt_async_1"
    assert bt["backtest_summary"] == fake_summary
