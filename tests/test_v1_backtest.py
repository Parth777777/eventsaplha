"""Tests for the /api/v1/backtest public endpoint.

Uses a real Flask app with the api_public blueprint registered, and
monkeypatches `event_backtest.backtest_event` so we don't actually hit
yfinance. The contract under test is the HTTP envelope, parameter parsing,
and error handling — the underlying math is covered in test_event_backtest.
"""
import datetime as dt
import json

import pytest
from flask import Flask


@pytest.fixture
def app(monkeypatch):
    """Build a minimal Flask app with api_public mounted and the backtest
    engine stubbed to deterministic output."""
    import api_public
    import event_backtest

    # Stub backtest_event to deterministic output keyed on inputs
    def _fake_backtest(ticker, event_date, *, similar_dates=None, drift_window=5, _df=None):
        return {
            "ticker": ticker.upper(),
            "sample_events_found": 1 + (len(list(similar_dates)) if similar_dates else 0),
            "avg_gap_up_pct": 1.25,
            "fade_probability_pct": 33.33,
            "avg_5day_drift_pct": 0.75,
            "events": [{
                "event_date": str(event_date),
                "resolved_trading_date": str(event_date),
                "gap_pct": 1.25,
                "intraday_pct": -0.50,
                "drift_5d_pct": 0.75,
            }],
            "stale": False,
            "as_of": "2026-05-19T14:00:00Z",
        }
    monkeypatch.setattr(event_backtest, "backtest_event", _fake_backtest)

    # Stub find_similar_event_dates to a small fixed list.
    # Accepts **kwargs so future filter additions don't re-break this test.
    import event_history
    monkeypatch.setattr(
        event_history, "find_similar_event_dates",
        lambda db, t, e, **kw: [dt.date(2025, 1, 15), dt.date(2024, 7, 20)],
    )

    app = Flask(__name__)
    app.config["TESTING"] = True

    # Provide a get_db that returns a truthy stub so the lookup path runs
    api_public.register(app, get_db=lambda: object())
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---- happy path ------------------------------------------------------------

def test_returns_200_and_standard_envelope(client):
    r = client.get("/api/v1/backtest?ticker=RELIANCE.NS&event_type=earnings")
    assert r.status_code == 200
    body = r.get_json()
    # Standard envelope
    assert "data" in body
    assert "as_of" in body
    # Endpoint-specific extras
    assert body["ticker"] == "RELIANCE.NS"
    assert body["event_type"] == "earnings"
    assert body["anchor_date"]  # ISO date
    assert body["similar_dates_found"] == 2
    # Payload shape mirrors backtest_summary
    d = body["data"]
    for k in ("ticker", "sample_events_found", "avg_gap_up_pct",
              "fade_probability_pct", "avg_5day_drift_pct", "events", "stale"):
        assert k in d
    # similar_dates widened the sample (1 anchor + 2 similar = 3)
    assert d["sample_events_found"] == 3


def test_accepts_explicit_date_parameter(client):
    r = client.get("/api/v1/backtest?ticker=INFY&event_type=order_win&date=2026-04-15")
    assert r.status_code == 200
    assert r.get_json()["anchor_date"] == "2026-04-15"


def test_defaults_anchor_to_today_when_no_date_given(client):
    r = client.get("/api/v1/backtest?ticker=INFY&event_type=order_win")
    assert r.status_code == 200
    today = dt.date.today().isoformat()
    assert r.get_json()["anchor_date"] == today


# ---- validation ------------------------------------------------------------

def test_missing_ticker_returns_400(client):
    r = client.get("/api/v1/backtest?event_type=earnings")
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"]["code"] == "missing_ticker"


def test_missing_event_type_returns_400(client):
    r = client.get("/api/v1/backtest?ticker=RELIANCE")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "missing_event_type"


def test_bad_date_format_returns_400(client):
    r = client.get("/api/v1/backtest?ticker=R&event_type=earnings&date=not-a-date")
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"]["code"] == "bad_date"
    assert "not-a-date" in body["error"]["message"]


def test_query_param_clamping(client):
    # lookback_days clamped to 30..3650
    r = client.get(
        "/api/v1/backtest?ticker=RELIANCE&event_type=earnings"
        "&lookback_days=99999&drift_window=999&limit=99999"
    )
    assert r.status_code == 200  # clamped silently rather than rejected


def test_index_lists_backtest_route(client):
    r = client.get("/api/v1/")
    assert r.status_code == 200
    paths = [e["path"] for e in r.get_json()["data"]["endpoints"]]
    assert "/api/v1/backtest" in paths


def test_sentiment_filter_propagates_to_response(client):
    r = client.get("/api/v1/backtest?ticker=RELIANCE&event_type=earnings&sentiment=bullish&magnitude=0.7")
    assert r.status_code == 200
    body = r.get_json()
    assert body["filters"]["sentiment"] == "bullish"
    assert body["filters"]["magnitude"] == 0.7


def test_bad_sentiment_returns_400(client):
    r = client.get("/api/v1/backtest?ticker=R&event_type=earnings&sentiment=neutral_pos")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "bad_sentiment"


def test_bad_magnitude_returns_400(client):
    r = client.get("/api/v1/backtest?ticker=R&event_type=earnings&magnitude=not-a-number")
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "bad_magnitude"


def test_engine_unavailable_returns_503(monkeypatch, client):
    """If event_backtest somehow fails to import, the endpoint must surface
    a 503 with a stable error code rather than a 500 traceback."""
    import sys
    # Force the import inside the handler to fail
    real_module = sys.modules.pop("event_backtest", None)
    sys.modules["event_backtest"] = None  # makes `from event_backtest import ...` raise
    try:
        r = client.get("/api/v1/backtest?ticker=RELIANCE&event_type=earnings")
        assert r.status_code == 503
        assert r.get_json()["error"]["code"] == "backtest_engine_unavailable"
    finally:
        # Restore so other tests still work
        if real_module is not None:
            sys.modules["event_backtest"] = real_module
        else:
            sys.modules.pop("event_backtest", None)
