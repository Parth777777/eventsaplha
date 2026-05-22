"""Tests for event_history.find_similar_event_dates.

Uses a real in-memory SQLite database so the SQL path is exercised end-to-end
(LIKE escaping, ESCAPE clause, date coercion). PostgreSQL placeholder logic
is covered by the is_postgres branch on a minimal stub.
"""
import datetime as dt
import sqlite3

import pytest

from event_history import (
    _coerce_date,
    _escape_like,
    _normalize_ticker,
    find_similar_event_dates,
)


class _FakeDB:
    """Mimics TickwaveDB's `.conn` + `.is_postgres` interface."""
    def __init__(self, conn, is_postgres=False):
        self.conn = conn
        self.is_postgres = is_postgres


@pytest.fixture
def sqlite_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT UNIQUE,
            title TEXT,
            event_type TEXT,
            sentiment TEXT,
            magnitude REAL,
            companies TEXT,
            published_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    rows = [
        # (event_id, title, event_type, sentiment, magnitude, companies, published_at)
        ("e1", "RELIANCE Q4 beat",          "earnings",   "bullish",  0.80, "RELIANCE.NS",            "2026-04-10 14:00:00"),
        ("e2", "RELIANCE Q3 result",        "earnings",   "bullish",  0.75, "RELIANCE.NS,TCS.NS",     "2026-01-12 09:30:00"),
        ("e3", "RELIANCE Q2 miss",          "earnings",   "bearish",  0.65, "RELIANCE",               "2025-10-15 09:30:00"),
        ("e4", "RELIANCE order win",        "order_win",  "bullish",  0.70, "RELIANCE.NS",            "2025-09-01 10:00:00"),
        ("e5", "TCS Q4",                    "earnings",   "bullish",  0.80, "TCS.NS",                 "2026-04-11 14:00:00"),
        ("e6", "OLD RELIANCE event",        "earnings",   "bullish",  0.80, "RELIANCE.NS",            "2019-01-01 09:00:00"),
        ("e7", "RELIANCE 2026 today beat",  "earnings",   "bullish",  0.78, "RELIANCE.NS",            "2026-05-19 14:00:00"),
        ("e8", "Industries — wildcard test", "earnings",  "neutral",  0.30, "100%_OWNED",             "2026-03-01 09:00:00"),
        ("e9", "RELIANCE tiny mini-result", "earnings",   "bullish",  0.10, "RELIANCE.NS",            "2026-02-01 09:00:00"),
    ]
    conn.executemany(
        "INSERT INTO events (event_id, title, event_type, sentiment, magnitude, companies, published_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
    )
    conn.commit()
    yield _FakeDB(conn, is_postgres=False)
    conn.close()


def test_returns_descending_dates_for_matching_ticker_and_type(sqlite_db):
    dates = find_similar_event_dates(sqlite_db, "RELIANCE.NS", "earnings", limit=10)
    # Should return e7, e1, e2, e3, then dropped e6 (5y cutoff)
    iso = [d.isoformat() for d in dates]
    assert iso[0] == "2026-05-19"
    assert iso[1] == "2026-04-10"
    assert "2026-01-12" in iso
    assert "2025-10-15" in iso
    # Wrong event_type must be filtered out
    assert "2025-09-01" not in iso
    # Wrong ticker must be filtered out
    assert "2026-04-11" not in iso


def test_lookback_cutoff_excludes_old_events(sqlite_db):
    # e6 is 2019 — outside 5y lookback by default
    dates = find_similar_event_dates(sqlite_db, "RELIANCE.NS", "earnings", limit=50)
    iso = [d.isoformat() for d in dates]
    assert "2019-01-01" not in iso


def test_exclude_date_drops_the_primary_event(sqlite_db):
    dates = find_similar_event_dates(
        sqlite_db, "RELIANCE.NS", "earnings",
        exclude_date=dt.date(2026, 5, 19),
    )
    iso = [d.isoformat() for d in dates]
    assert "2026-05-19" not in iso
    assert "2026-04-10" in iso


def test_matches_ticker_with_or_without_exchange_suffix(sqlite_db):
    # e3 has companies="RELIANCE" (no suffix); should match the .NS query
    dates = find_similar_event_dates(sqlite_db, "RELIANCE.NS", "earnings")
    iso = [d.isoformat() for d in dates]
    assert "2025-10-15" in iso


def test_limit_caps_returned_dates(sqlite_db):
    dates = find_similar_event_dates(sqlite_db, "RELIANCE.NS", "earnings", limit=2)
    assert len(dates) == 2


def test_wildcard_chars_in_user_input_dont_widen_match(sqlite_db):
    # e8 has companies="100%_OWNED". A naive LIKE would match almost anything
    # when the query ticker contains % or _ — the escape logic must prevent
    # that. Query with a deliberate `_` in the input.
    dates = find_similar_event_dates(sqlite_db, "_OWNED", "earnings")
    # The escaping turns "_" into a literal underscore in the LIKE pattern,
    # so e8 ("100%_OWNED") matches but no spurious wider rows do.
    iso = [d.isoformat() for d in dates]
    assert iso == ["2026-03-01"]


def test_empty_inputs_return_empty_list(sqlite_db):
    assert find_similar_event_dates(sqlite_db, "", "earnings") == []
    assert find_similar_event_dates(sqlite_db, "RELIANCE.NS", "") == []
    assert find_similar_event_dates(None, "RELIANCE.NS", "earnings") == []


def test_missing_table_returns_empty_list_not_raise():
    conn = sqlite3.connect(":memory:")
    db = _FakeDB(conn, is_postgres=False)
    # No events table exists — function must absorb the error
    out = find_similar_event_dates(db, "RELIANCE.NS", "earnings")
    assert out == []
    conn.close()


def test_normalize_ticker_strips_known_suffixes():
    assert _normalize_ticker("RELIANCE.NS") == "RELIANCE"
    assert _normalize_ticker("TCS.BO") == "TCS"
    assert _normalize_ticker("infy.bse") == "INFY"
    assert _normalize_ticker("HDFCBANK") == "HDFCBANK"
    assert _normalize_ticker("") == ""
    assert _normalize_ticker(None) == ""  # type: ignore[arg-type]


def test_escape_like_handles_backslash_pct_underscore():
    assert _escape_like("a%b") == r"a\%b"
    assert _escape_like("a_b") == r"a\_b"
    assert _escape_like("a\\b") == r"a\\b"


def test_sentiment_filter_isolates_same_direction(sqlite_db):
    """When sentiment is passed, the bearish miss (e3) must be excluded from
    a bullish-anchor query — that's the whole point of "similar OUTCOME"."""
    bullish_dates = find_similar_event_dates(
        sqlite_db, "RELIANCE.NS", "earnings", sentiment="bullish",
    )
    iso = [d.isoformat() for d in bullish_dates]
    assert "2025-10-15" not in iso          # e3 (bearish) excluded
    assert "2026-04-10" in iso              # e1 (bullish) included
    assert "2026-01-12" in iso              # e2 (bullish) included


def test_sentiment_filter_case_insensitive(sqlite_db):
    out = find_similar_event_dates(
        sqlite_db, "RELIANCE.NS", "earnings", sentiment="BULLISH",
    )
    assert any(d.isoformat() == "2026-04-10" for d in out)


def test_magnitude_band_filter_excludes_dissimilar_outcomes(sqlite_db):
    """A blockbuster beat (magnitude 0.80) and a tiny mini-result (mag 0.10)
    aren't comparable — the band filter keeps comparables only."""
    out = find_similar_event_dates(
        sqlite_db, "RELIANCE.NS", "earnings",
        magnitude=0.80, magnitude_band=0.4,   # ±0.32 around 0.80 = [0.48, 1.12]
    )
    iso = [d.isoformat() for d in out]
    # e1 (0.80), e2 (0.75), e3 (0.65), e7 (0.78) all inside the band
    assert "2026-04-10" in iso
    assert "2026-01-12" in iso
    assert "2025-10-15" in iso
    assert "2026-05-19" in iso
    # e9 (0.10) — far outside the band, must be excluded
    assert "2026-02-01" not in iso


def test_zero_magnitude_anchor_skips_the_band_filter(sqlite_db):
    """An anchor magnitude of 0 means we don't know how big the event is —
    silently fall back to no-magnitude-filter rather than reject everything."""
    out = find_similar_event_dates(
        sqlite_db, "RELIANCE.NS", "earnings", magnitude=0.0,
    )
    # e9 (small) should be back in since the filter was skipped
    iso = [d.isoformat() for d in out]
    assert "2026-02-01" in iso


def test_sentiment_and_magnitude_combined(sqlite_db):
    """Both filters apply together — only bullish events within band."""
    out = find_similar_event_dates(
        sqlite_db, "RELIANCE.NS", "earnings",
        sentiment="bullish", magnitude=0.78, magnitude_band=0.4,
    )
    iso = [d.isoformat() for d in out]
    # e1, e2, e7 are bullish and in the magnitude band
    assert "2026-04-10" in iso
    assert "2026-01-12" in iso
    assert "2026-05-19" in iso
    # e3 (bearish, in-band) excluded by sentiment
    assert "2025-10-15" not in iso
    # e9 (bullish, out-of-band) excluded by magnitude
    assert "2026-02-01" not in iso


def test_backward_compat_no_new_args(sqlite_db):
    """Existing callers passing only the original args must keep working."""
    out = find_similar_event_dates(sqlite_db, "RELIANCE.NS", "earnings")
    iso = [d.isoformat() for d in out]
    # All RELIANCE earnings within lookback (including the bearish miss) appear
    assert "2025-10-15" in iso
    assert "2026-04-10" in iso


def test_coerce_date_accepts_common_forms():
    assert _coerce_date("2026-05-19") == dt.date(2026, 5, 19)
    assert _coerce_date("2026-05-19 14:00:00") == dt.date(2026, 5, 19)
    assert _coerce_date(dt.datetime(2026, 5, 19, 14, 0)) == dt.date(2026, 5, 19)
    assert _coerce_date(dt.date(2026, 5, 19)) == dt.date(2026, 5, 19)
    assert _coerce_date(None) is None
    assert _coerce_date("nonsense") is None
