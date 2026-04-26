"""
Backfill forensics on every existing signal.

Runs the full Phase 1.5 enrichment pipeline over signals that were generated
before the pipeline existed (or before it worked end-to-end). Writes intent,
fingerprint_flags, manipulation_score, pump_score, and fine_print fields back
to the DB so the UI has data to show immediately.

Usage:
    python scraper/backfill_forensics.py              # all signals
    python scraper/backfill_forensics.py --limit 500  # cap
    python scraper/backfill_forensics.py --since 7d   # only recent
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from database_schema import TickwaveDB

logger = logging.getLogger(__name__)


def _parse_since(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    m = re.match(r"(\d+)\s*([hd])", s.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(hours=n) if unit == "h" else timedelta(days=n)
    return datetime.utcnow() - delta


def fetch_signals_to_enrich(db: TickwaveDB, since: Optional[datetime] = None,
                              limit: Optional[int] = None,
                              only_missing: bool = True) -> List[Dict]:
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    q = """SELECT event_id, event_type, ticker, company, alpha_score, confidence,
                   regime, entry_price, sentiment, magnitude, impact_score,
                   source, headline, link, status, created_at,
                   intent, manipulation_score
              FROM signals
             WHERE 1=1"""
    args: list = []
    if only_missing:
        q += " AND (manipulation_score IS NULL OR intent IS NULL)"
    if since is not None:
        q += f" AND created_at >= {p}"
        args.append(since.isoformat() if not db.is_postgres else since)
    q += " ORDER BY created_at DESC"
    if limit:
        q += f" LIMIT {p}"
        args.append(limit)
    cursor = db.conn.cursor()
    cursor.execute(q, tuple(args))
    rows = cursor.fetchall()
    out: List[Dict] = []
    for r in rows:
        d = r if isinstance(r, dict) else dict(zip([c[0] for c in cursor.description], r))
        out.append(d)
    return out


def enrich_one(db: TickwaveDB, signal: Dict) -> bool:
    """Run the v2 forensics + rescoring + volume on a single signal."""
    try:
        from orchestrator_ext import enrich_signal_forensics_v2, enrich_signal_volume, persist_signal_extensions
    except Exception as exc:
        logger.error("orchestrator import failed: %s", exc)
        return False

    try:
        # Volume (needs yfinance — skip on failure)
        try:
            enrich_signal_volume(db, signal)
        except Exception as exc:
            logger.debug("volume enrich skipped: %s", exc)
        # Forensics v2 (fine_print + pump_dump + article + base fused)
        enrich_signal_forensics_v2(db, signal)
        # Optional rescore
        try:
            from rescoring import rescore_signal
            rescore_signal(db, signal, horizon="3D")
        except Exception:
            pass
        # Persist extended fields
        persist_signal_extensions(db, signal)
        return True
    except Exception as exc:
        logger.debug("enrich signal %s failed: %s", signal.get("event_id"), exc)
        return False


def run(since: Optional[str] = None, limit: Optional[int] = None,
        only_missing: bool = True, progress_every: int = 50,
        per_signal_sleep: float = 0.0) -> Dict:
    db = TickwaveDB()
    since_dt = _parse_since(since) if since else None
    signals = fetch_signals_to_enrich(db, since_dt, limit, only_missing)
    logger.info("backfill: %d signals to enrich (since=%s, limit=%s, only_missing=%s)",
                len(signals), since, limit, only_missing)

    enriched = 0
    failures = 0
    start = time.time()
    for i, sig in enumerate(signals, 1):
        ok = enrich_one(db, sig)
        if ok:
            enriched += 1
        else:
            failures += 1
        if i % progress_every == 0:
            elapsed = time.time() - start
            rate = i / max(elapsed, 1e-6)
            remaining = (len(signals) - i) / max(rate, 1e-6)
            logger.info("  progress %d/%d  enriched=%d  failed=%d  rate=%.1f/s  eta=%.0fs",
                        i, len(signals), enriched, failures, rate, remaining)
        if per_signal_sleep > 0:
            time.sleep(per_signal_sleep)

    summary = {
        "total": len(signals),
        "enriched": enriched,
        "failures": failures,
        "elapsed_seconds": round(time.time() - start, 1),
    }
    logger.info("backfill complete: %s", summary)
    return summary


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help='e.g. "24h" or "7d"')
    parser.add_argument("--limit", type=int)
    parser.add_argument("--all", action="store_true", help="Include already-enriched signals (re-run)")
    args = parser.parse_args()

    result = run(
        since=args.since,
        limit=args.limit,
        only_missing=not args.all,
        per_signal_sleep=0.0,
    )
    print(result)
