"""
Demo seed for promoter_holdings / promoters / promoter_events / sebi_disclosures.

Seeds realistic-looking quarterly history for the 32 monitored tickers so the
promoter UI has something to visualize before the real BSE XBRL scrape runs.

Values are illustrative only — DO NOT use for trading decisions. Real data
populates via the quarterly cron once BSE credentials are configured.
"""

from __future__ import annotations

import logging
import random
from datetime import date, timedelta
from typing import Iterable, List, Tuple

logger = logging.getLogger(__name__)


# Seed baselines: approximate real promoter holdings (public data) ±noise
# (promoter_pct, pledge_pct, fii_pct, dii_pct) per ticker.
BASELINES = {
    "INFY":      (13.0, 0.0, 32.0, 16.0),
    "TCS":       (71.8, 0.0, 13.5, 5.0),
    "WIPRO":     (72.8, 0.0, 7.5, 6.0),
    "LT":        (0.0, 0.0, 23.2, 32.0),
    "RELIANCE":  (50.3, 0.0, 22.0, 14.0),
    "HDFCBANK":  (0.0, 0.0, 48.5, 23.0),
    "ICICIBANK": (0.0, 0.0, 45.0, 30.0),
    "SBIN":      (57.4, 0.0, 10.2, 22.0),
    "BAJAJFINSV":(60.9, 0.0, 6.8, 8.5),
    "MARUTI":    (58.2, 0.0, 25.0, 10.5),
    "TATASTEEL": (33.2, 0.0, 18.9, 22.0),
    "JSWSTEEL":  (44.9, 3.2, 21.8, 12.0),
    "ADANIGREEN":(60.5, 26.8, 15.0, 3.5),
    "ADANIPORTS":(66.0, 13.5, 13.2, 9.5),
    "SUNPHARMA": (54.5, 0.0, 17.2, 16.5),
    "DIVISLAB":  (52.0, 0.0, 16.5, 18.0),
    "HINDUNILVR":(61.9, 0.0, 12.6, 10.5),
    "ITC":       (0.0, 0.0, 13.2, 41.5),
    "NESTLEIND": (62.8, 0.0, 13.8, 8.5),
    "AXISBANK":  (8.1, 0.0, 51.0, 24.0),
    "KNRCON":    (49.2, 1.5, 3.6, 15.0),
    "TECHM":     (35.7, 0.0, 26.0, 20.5),
    "HCLTECH":   (60.2, 0.0, 19.5, 12.0),
    "BAJAJ-AUTO":(49.9, 0.0, 14.0, 10.0),
    "BHARTIARTL":(50.0, 0.0, 24.5, 20.5),
    "CIPLA":     (33.5, 0.0, 26.0, 22.0),
    "LUPIN":     (47.0, 0.0, 17.0, 23.0),
    "POWERGRID": (51.3, 0.0, 26.5, 13.0),
    "NTPC":      (51.1, 0.0, 16.5, 23.0),
    "COALINDIA": (63.1, 0.0, 10.0, 16.0),
    "IOC":       (51.5, 0.0, 5.6, 18.5),
    "TATAPOWER": (46.8, 0.0, 10.8, 18.5),
}


def _quarter_ends(n: int = 8) -> List[date]:
    """Return N most-recent quarter-end dates (oldest first)."""
    today = date.today()
    # Find the latest quarter end
    month_map = [12, 3, 6, 9]  # quarters end Dec/Mar/Jun/Sep
    qtrs: List[date] = []
    y, m = today.year, today.month
    for _ in range(n):
        qm = max([x for x in month_map if x < m] or [12])
        qy = y if qm < m else y - 1
        # last day of that month
        if qm in (3, 12):
            qe = date(qy, qm, 31)
        elif qm == 6:
            qe = date(qy, qm, 30)
        elif qm == 9:
            qe = date(qy, qm, 30)
        else:
            qe = date(qy, qm, 30)
        qtrs.append(qe)
        m, y = qm, qy
    qtrs.reverse()
    return qtrs


def _generate_series(baseline: Tuple[float, float, float, float], n: int = 8,
                     rng: random.Random = None) -> List[Tuple[float, float, float, float]]:
    """Generate a realistic noisy drift around the baseline."""
    rng = rng or random.Random()
    prom, pledge, fii, dii = baseline
    series: List[Tuple[float, float, float, float]] = []
    for _ in range(n):
        prom_j = prom + rng.uniform(-0.3, 0.3)
        pledge_j = max(0.0, pledge + rng.uniform(-0.5, 0.5))
        fii_j = fii + rng.uniform(-0.8, 0.8)
        dii_j = dii + rng.uniform(-0.8, 0.8)
        series.append((round(prom_j, 2), round(pledge_j, 2), round(fii_j, 2), round(dii_j, 2)))
    return series


def seed_ticker(db, ticker: str, seed: int = 42) -> int:
    baseline = BASELINES.get(ticker.upper())
    if baseline is None:
        return 0
    rng = random.Random(seed + hash(ticker) % 10000)
    qtrs = _quarter_ends(8)
    series = _generate_series(baseline, len(qtrs), rng)
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    inserted = 0
    for q_end, (prom, pledge, fii, dii) in zip(qtrs, series):
        public_pct = round(max(0.0, 100 - prom - fii - dii), 2)
        mf = round(dii * rng.uniform(0.55, 0.75), 2)
        insur = round(dii * rng.uniform(0.15, 0.35), 2)
        try:
            if db.is_postgres:
                db.conn.cursor().execute(
                    """INSERT INTO promoter_holdings (ticker, quarter_end, promoter_pct, promoter_pledge_pct,
                          fii_pct, dii_pct, public_pct, mutual_fund_pct, insurance_pct, raw_xbrl_url)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (ticker, quarter_end) DO NOTHING""",
                    (ticker.upper(), q_end, prom, pledge, fii, dii, public_pct, mf, insur, "seed://demo"),
                )
            else:
                db.conn.execute(
                    f"""INSERT OR IGNORE INTO promoter_holdings (ticker, quarter_end, promoter_pct,
                         promoter_pledge_pct, fii_pct, dii_pct, public_pct, mutual_fund_pct,
                         insurance_pct, raw_xbrl_url)
                         VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, 'seed://demo')""",
                    (ticker.upper(), q_end.isoformat(), prom, pledge, fii, dii, public_pct, mf, insur),
                )
            inserted += 1
        except Exception as exc:
            logger.debug("seed holding upsert failed: %s", exc)
            db.conn.rollback()
    # Seed a couple of key persons per ticker (plausible role names)
    demo_persons = [
        ("Promoter Group", "promoter_group", None),
        ("Managing Director", "md", None),
    ]
    for name, role, din in demo_persons:
        try:
            if db.is_postgres:
                db.conn.cursor().execute(
                    """INSERT INTO promoters (ticker, person_name, role, din, source)
                       VALUES (%s, %s, %s, %s, 'seed')
                       ON CONFLICT (ticker, person_name) DO NOTHING""",
                    (ticker.upper(), f"{ticker.upper()} {name}", role, din),
                )
            else:
                db.conn.execute(
                    f"""INSERT OR IGNORE INTO promoters (ticker, person_name, role, din, source)
                        VALUES ({p}, {p}, {p}, {p}, 'seed')""",
                    (ticker.upper(), f"{ticker.upper()} {name}", role, din),
                )
        except Exception:
            db.conn.rollback()
    # Occasionally seed a SAST event for realism on a few tickers
    if rng.random() < 0.35:
        evt_date = date.today() - timedelta(days=rng.randint(5, 60))
        kind = rng.choice(["pit_buy", "pit_sell", "sast_acquire"])
        try:
            if db.is_postgres:
                db.conn.cursor().execute(
                    """INSERT INTO promoter_events (ticker, event_date, event_type, person_name, reason, source)
                       VALUES (%s, %s, %s, %s, %s, 'seed')
                       ON CONFLICT (ticker, event_date, event_type, person_name) DO NOTHING""",
                    (ticker.upper(), evt_date, kind, f"{ticker.upper()} Promoter Group",
                     f"Demo {kind} event"),
                )
            else:
                db.conn.execute(
                    f"""INSERT OR IGNORE INTO promoter_events
                         (ticker, event_date, event_type, person_name, reason, source)
                         VALUES ({p}, {p}, {p}, {p}, {p}, 'seed')""",
                    (ticker.upper(), evt_date.isoformat(), kind,
                     f"{ticker.upper()} Promoter Group", f"Demo {kind} event"),
                )
        except Exception:
            db.conn.rollback()
    db.conn.commit()
    return inserted


def seed_all(db, tickers: Iterable[str] = None) -> int:
    tickers = list(tickers) if tickers else list(BASELINES.keys())
    total = 0
    for t in tickers:
        total += seed_ticker(db, t)
    logger.info("promoter demo seed complete: %d rows across %d tickers", total, len(tickers))
    return total
