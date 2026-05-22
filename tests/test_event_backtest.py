"""Tests for the event-window backtester.

The backtester is monkeypatch-friendly: pass `_df` to bypass yfinance and run
the math against a hand-built DataFrame. All formulas are validated to two
decimal places against manual calculations.
"""
import datetime as dt

import pandas as pd
import pytest

from event_backtest import (
    backtest_event,
    cache_clear,
    cache_stats,
    _as_date,
    _resolve_t_position,
)


def _frame(rows):
    """Build a yfinance-shaped DataFrame from (date, open, close) tuples.

    High/Low/Volume/Adj Close are filled with safe values; the backtester
    only consumes Open and Close.
    """
    idx = pd.DatetimeIndex([dt.date.fromisoformat(d) for d, _, _ in rows])
    return pd.DataFrame(
        {
            "Open":      [o for _, o, _ in rows],
            "High":      [max(o, c) * 1.01 for _, o, c in rows],
            "Low":       [min(o, c) * 0.99 for _, o, c in rows],
            "Close":     [c for _, _, c in rows],
            "Adj Close": [c for _, _, c in rows],
            "Volume":    [1_000_000 for _ in rows],
        },
        index=idx,
    )


# ---- date parsing -----------------------------------------------------------

def test_as_date_accepts_common_formats():
    assert _as_date("2026-05-19") == dt.date(2026, 5, 19)
    assert _as_date("2026-05-19T14:15:22") == dt.date(2026, 5, 19)
    assert _as_date("2026-05-19 14:15:22Z") == dt.date(2026, 5, 19)
    assert _as_date(dt.date(2026, 5, 19)) == dt.date(2026, 5, 19)
    assert _as_date(dt.datetime(2026, 5, 19, 9, 30)) == dt.date(2026, 5, 19)


# ---- math correctness -------------------------------------------------------

def test_single_event_three_formulas_correct():
    # T-1 close = 100, T open = 102 (gap +2%), T close = 99 (intraday -2.94%),
    # T+5 close = 105 (drift +6.06%). T at pos 1, so T+5 is pos 6.
    df = _frame([
        ("2026-05-12", 99, 100),   # pos 0: T-1
        ("2026-05-13", 102, 99),   # pos 1: T
        ("2026-05-14", 99, 100),   # pos 2
        ("2026-05-15", 100, 101),  # pos 3
        ("2026-05-18", 101, 102),  # pos 4
        ("2026-05-19", 102, 104),  # pos 5
        ("2026-05-20", 104, 105),  # pos 6: T+5
    ])
    out = backtest_event("ANY.NS", "2026-05-13", _df=df)
    assert out["sample_events_found"] == 1
    assert out["stale"] is False
    assert out["avg_gap_up_pct"] == pytest.approx((102 - 100) / 100 * 100, abs=0.01)
    assert out["avg_5day_drift_pct"] == pytest.approx((105 - 99) / 99 * 100, abs=0.01)
    # intraday is negative — fade
    e = out["events"][0]
    assert e["intraday_pct"] == pytest.approx((99 - 102) / 102 * 100, abs=0.01)
    assert out["fade_probability_pct"] == 100.0  # 1/1 events faded


def test_holiday_forward_skip_saturday_to_monday():
    # Event date 2026-05-16 is a Saturday — no row in df. T resolves to Monday.
    df = _frame([
        ("2026-05-13", 100, 100),  # pos 0: Wed
        ("2026-05-14", 100, 100),  # pos 1: Thu
        ("2026-05-15", 100, 100),  # pos 2: Fri (becomes T-1)
        ("2026-05-18", 105, 110),  # pos 3: Mon (resolved T)
        ("2026-05-19", 110, 111),  # pos 4
        ("2026-05-20", 111, 112),  # pos 5
        ("2026-05-21", 112, 113),  # pos 6
        ("2026-05-22", 113, 114),  # pos 7
        ("2026-05-25", 114, 115),  # pos 8: T+5
    ])
    out = backtest_event("ANY.NS", "2026-05-16", _df=df)  # Saturday
    assert out["sample_events_found"] == 1
    ev = out["events"][0]
    assert ev["resolved_trading_date"] == "2026-05-18"
    # Gap = (105 - 100) / 100 * 100 = 5.0
    assert ev["gap_pct"] == pytest.approx(5.0, abs=0.01)


def test_holiday_skip_bails_when_gap_too_large():
    # Event on 2026-01-01 but next row is 2026-01-15 (14 days later).
    # Backtester should refuse rather than misalign T-1.
    df = _frame([
        ("2025-12-20", 100, 100),
        ("2026-01-15", 100, 100),
        ("2026-01-16", 100, 100),
        ("2026-01-19", 100, 100),
        ("2026-01-20", 100, 100),
        ("2026-01-21", 100, 100),
        ("2026-01-22", 100, 100),
    ])
    out = backtest_event("ANY.NS", "2026-01-01", _df=df)
    assert out["sample_events_found"] == 0
    assert out["stale"] is True


def test_no_t_minus_one_baseline_returns_stale():
    # Event date is the very first row — no T-1 close to compute gap from.
    df = _frame([
        ("2026-05-19", 100, 100),  # T (no prior row)
        ("2026-05-20", 100, 100),
        ("2026-05-21", 100, 100),
        ("2026-05-22", 100, 100),
        ("2026-05-25", 100, 100),
        ("2026-05-26", 100, 100),
    ])
    out = backtest_event("RECENT_IPO.NS", "2026-05-19", _df=df)
    assert out["sample_events_found"] == 0
    assert out["reason"] == "no_processable_events"


def test_insufficient_forward_data_returns_stale():
    # Only 3 trading sessions after T — drift window of 5 won't fit.
    df = _frame([
        ("2026-05-12", 100, 100),
        ("2026-05-13", 100, 100),  # T
        ("2026-05-14", 100, 100),
        ("2026-05-15", 100, 100),
        ("2026-05-18", 100, 100),  # only 3 forward sessions
    ])
    out = backtest_event("ANY.NS", "2026-05-13", _df=df)
    assert out["sample_events_found"] == 0


def test_aggregates_across_multiple_similar_dates():
    # Two events: one with positive intraday (spike-and-hold),
    # one with negative intraday (spike-and-fade). Fade prob = 50%.
    df = _frame([
        ("2026-04-01", 100, 100),  # T-1
        ("2026-04-02", 105, 110),  # T1: gap +5%, intraday +4.76%
        ("2026-04-03", 110, 112),
        ("2026-04-06", 112, 114),
        ("2026-04-07", 114, 115),
        ("2026-04-08", 115, 118),  # T1+5
        ("2026-04-09", 118, 120),
        # T-1 for event 2
        ("2026-05-11", 200, 200),
        ("2026-05-12", 210, 195),  # T2: gap +5%, intraday -7.14% (fade)
        ("2026-05-13", 195, 196),
        ("2026-05-14", 196, 198),
        ("2026-05-15", 198, 199),
        ("2026-05-18", 199, 201),  # T2+5
        ("2026-05-19", 201, 202),
    ])
    out = backtest_event(
        "ANY.NS",
        "2026-04-02",
        similar_dates=["2026-05-12"],
        _df=df,
    )
    assert out["sample_events_found"] == 2
    # avg gap = (5 + 5) / 2 = 5
    assert out["avg_gap_up_pct"] == pytest.approx(5.0, abs=0.01)
    # fade probability: 1 of 2 events had negative intraday
    assert out["fade_probability_pct"] == pytest.approx(50.0, abs=0.01)


def test_empty_dataframe_returns_stale_no_crash():
    out = backtest_event("ANY.NS", "2026-05-19", _df=pd.DataFrame())
    assert out["stale"] is True
    assert out["sample_events_found"] == 0
    assert "as_of" in out


def test_invalid_ticker_returns_stable_shape():
    out = backtest_event("", "2026-05-19")
    assert out["stale"] is True
    assert out["sample_events_found"] == 0
    assert out["events"] == []


def test_invalid_date_returns_stable_shape():
    out = backtest_event("ANY.NS", "not-a-date", _df=pd.DataFrame())
    assert out["stale"] is True
    assert out["reason"].startswith("bad_date")


def test_resolve_t_position_searchsort_correctness():
    df = _frame([
        ("2026-05-12", 100, 100),
        ("2026-05-15", 100, 100),
        ("2026-05-18", 100, 100),
    ])
    assert _resolve_t_position(df.index, dt.date(2026, 5, 15)) == 1
    # Saturday — resolves to Monday at position 2 (3 days forward)
    assert _resolve_t_position(df.index, dt.date(2026, 5, 16)) == 2
    # After last row → None
    assert _resolve_t_position(df.index, dt.date(2026, 5, 30)) is None
    # Empty index
    assert _resolve_t_position(pd.DatetimeIndex([]), dt.date(2026, 5, 19)) is None


def test_response_payload_shape_matches_prd_contract():
    df = _frame([
        ("2026-05-12", 99, 100),
        ("2026-05-13", 102, 99),
        ("2026-05-14", 99, 100),
        ("2026-05-15", 100, 101),
        ("2026-05-18", 101, 102),
        ("2026-05-19", 102, 104),
        ("2026-05-20", 104, 105),
    ])
    out = backtest_event("RELIANCE.NS", "2026-05-13", _df=df)
    for key in (
        "ticker", "sample_events_found", "avg_gap_up_pct",
        "fade_probability_pct", "avg_5day_drift_pct", "events", "stale", "as_of",
    ):
        assert key in out, f"missing PRD key: {key}"
    assert out["ticker"] == "RELIANCE.NS"


def test_cache_stats_and_clear():
    cache_clear()
    s = cache_stats()
    assert s["size"] == 0
    assert s["ttl_seconds"] == 24 * 3600
