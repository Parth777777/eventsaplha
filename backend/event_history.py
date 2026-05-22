"""Pull historically-similar event dates from the events table.

Used by the broadcast-path backtester to widen the aggregation sample so the
PRD `backtest_summary` payload (`sample_events_found`, `fade_probability_pct`,
averages) reflects N comparable events rather than just T+0.

The events table is created by scraper/database_schema.py and contains:
    event_id, title, summary, source, link, event_type, sentiment, magnitude,
    companies (comma-separated tickers), published_at, created_at

Defensive design:
    - Works against PostgreSQL (`%s` placeholders) AND SQLite (`?`) via the
      same `db.is_postgres` flag the rest of the codebase uses.
    - Never raises — any DB issue returns an empty list, keeping the backtest
      pipeline running in single-event mode.
    - Hardens the ticker LIKE-clause against SQL wildcards in user input
      (`%`, `_`, `\\`) so a malformed event payload can't widen the match.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


_DEFAULT_LIMIT = 20
# How far back to search. 5 years catches enough event-window samples for any
# reasonable corporate-action category without dragging multi-decade dataset.
_DEFAULT_LOOKBACK_DAYS = 365 * 5


# Tickers in the events table can appear as plain symbol ("RELIANCE") or with
# an exchange suffix ("RELIANCE.NS"). To match both with one LIKE we strip the
# suffix and search for the bare symbol — a false-positive on a partial-name
# collision (e.g. "TATA" matching "TATAMOTORS") is acceptable because the
# backtester then computes against the *queried* ticker's OHLC, so any
# accidental wider match just contributes neutral noise that the per-event
# math discards via no-data / no-T-1 paths.
_TICKER_SUFFIX = re.compile(r"\.(NS|BO|BSE|NSE)$", re.IGNORECASE)


def _normalize_ticker(ticker: str) -> str:
    if not ticker:
        return ""
    return _TICKER_SUFFIX.sub("", str(ticker).strip()).upper()


def _escape_like(s: str) -> str:
    """Escape SQL LIKE wildcards so user input can't widen the match."""
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _coerce_date(value: Any) -> Optional[dt.date]:
    """Turn whatever the DB hands back into a date, or None if unrecognizable."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # ISO date / datetime forms covered explicitly; rare formats fall
        # through to the pandas parser if available.
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return dt.datetime.strptime(s[: len(fmt) + 4], fmt).date()
            except ValueError:
                continue
        try:
            import pandas as pd
            return pd.to_datetime(s).date()
        except Exception:
            return None
    return None


def find_similar_event_dates(
    db: Any,
    ticker: str,
    event_type: str,
    *,
    sentiment: Optional[str] = None,
    magnitude: Optional[float] = None,
    magnitude_band: float = 0.4,
    limit: int = _DEFAULT_LIMIT,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    exclude_date: Optional[dt.date] = None,
) -> List[dt.date]:
    """Return up to `limit` historical event dates for (ticker, event_type),
    optionally narrowed to events with similar *outcome* — same sentiment
    direction and magnitude within ±`magnitude_band` (default ±40%) of the
    anchor magnitude. Sorted most-recent first, going back ≤ `lookback_days`.

    "Similar earnings results" needs more than same event_type — a beat and a
    miss both have event_type='earnings' but produce opposite stock reactions.
    Matching on sentiment + magnitude isolates the historical reactions to
    *comparable* outcomes. When sentiment / magnitude are None the filters are
    skipped, so the existing single-arg call site still works.

    `db` must expose `.conn` and `.is_postgres` (matches TickwaveDB shape).
    On any error returns an empty list — the backtester then runs in
    single-event mode.
    """
    if not ticker or not event_type or db is None:
        return []
    try:
        is_pg = bool(getattr(db, "is_postgres", False))
        conn = getattr(db, "conn", None)
        if conn is None:
            return []
        placeholder = "%s" if is_pg else "?"
        cutoff = dt.date.today() - dt.timedelta(days=max(1, int(lookback_days)))
        bare_ticker = _normalize_ticker(ticker)
        if not bare_ticker:
            return []
        like_token = "%" + _escape_like(bare_ticker) + "%"

        # Build the WHERE incrementally so the optional filters are append-only.
        where = [
            f"event_type = {placeholder}",
            f"companies LIKE {placeholder} ESCAPE '\\'",
            f"COALESCE(published_at, created_at) >= {placeholder}",
        ]
        params: List[Any] = [event_type, like_token, cutoff]

        # Sentiment filter — case-insensitive on the value passed in. Stored
        # values are lowercase tokens like "bullish" / "bearish" / "neutral".
        if sentiment:
            where.append(f"LOWER(COALESCE(sentiment, '')) = {placeholder}")
            params.append(str(sentiment).lower())

        # Magnitude band: ±`magnitude_band` * |magnitude| around the anchor.
        # Skip when anchor magnitude is 0 (no meaningful band — would filter
        # everything OR match everything depending on row data, neither useful).
        if magnitude is not None:
            try:
                m = float(magnitude)
            except (TypeError, ValueError):
                m = 0.0
            if abs(m) > 1e-9:
                band = abs(m) * max(0.0, float(magnitude_band))
                lo, hi = m - band, m + band
                where.append(f"magnitude BETWEEN {placeholder} AND {placeholder}")
                params.extend([lo, hi])

        params.append(int(limit))
        sql = (
            "SELECT published_at, created_at FROM events "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(published_at, created_at) DESC "
            f"LIMIT {placeholder}"
        )
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            rows = cur.fetchall()
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except Exception as e:
        logger.debug("find_similar_event_dates query failed: %s", e)
        return []

    out: List[dt.date] = []
    seen = set()
    for row in rows or []:
        # Row may be tuple OR mapping (psycopg2 RealDictCursor / sqlite Row).
        if hasattr(row, "keys"):
            pub = row.get("published_at") if hasattr(row, "get") else row["published_at"]
            cre = row.get("created_at") if hasattr(row, "get") else row["created_at"]
        else:
            pub, cre = (row[0], row[1])
        d = _coerce_date(pub) or _coerce_date(cre)
        if d is None or d in seen:
            continue
        if exclude_date and d == exclude_date:
            continue
        seen.add(d)
        out.append(d)
    return out
