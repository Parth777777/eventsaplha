"""Unified scraper runner with persistent job_runs logging.

Why this exists:
  Before this module, scheduled scrapers wrapped themselves in `try: ... except:
  logger.warning(...)` and called it a day. When a scraper silently returned
  zero rows, nobody knew. Premium features that depend on those tables
  (forensics, F&O, smart-money) shipped empty and embarrassed paying users.

What it does:
  - Wraps every scraper call in a span that writes to the `job_runs` table.
  - Captures start/end time, duration, status, rows-inserted, error string.
  - Exposes `run(job_name)` for both the scheduler AND admin endpoints.
  - Exposes `recent_runs()` so /api/admin/jobs can show status without grepping logs.

Add a new scraper by:
  1. Define a callable in JOBS that takes `db` and returns an int (rows
     inserted) OR a dict like {'inserted': N, 'detail': '...'}.
  2. That's it — scheduling + logging + admin trigger come for free.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
# Make scraper/ importable so `from bulk_deals import ...` works whether the
# caller already added it to sys.path or not.
for _p in (ROOT / "scraper", ROOT / "backend"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)


# --- Job registry --------------------------------------------------------

def _run_bulk_deals(db) -> Dict[str, Any]:
    from bulk_deals import fetch_nse_bulk_deals, fetch_nse_block_deals, fetch_sebi_bans
    a = fetch_nse_bulk_deals(db)
    b = fetch_nse_block_deals(db)
    c = fetch_sebi_bans(db)
    return {"inserted": int(a) + int(b) + int(c),
            "detail": {"bulk": a, "block": b, "sebi_bans": c}}


def _run_fo_unusual(db) -> Dict[str, Any]:
    from fo_signals import snapshot_and_persist
    universe = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN',
                'TATAMOTORS', 'AXISBANK', 'BHARTIARTL', 'LT', 'KOTAKBANK',
                'MARUTI', 'HCLTECH', 'WIPRO', 'ASIANPAINT', 'SUNPHARMA',
                'ITC', 'TITAN', 'BAJFINANCE', 'ULTRACEMCO']
    r = snapshot_and_persist(db, universe, is_index=False)
    return {"inserted": int(r.get("unusual") or 0) + int(r.get("snapshots") or 0),
            "detail": r}


def _run_fo_fii_dii(db) -> Dict[str, Any]:
    from fo_signals import fetch_fii_dii_derivatives, persist_fii_dii_derivatives
    row = fetch_fii_dii_derivatives()
    ok = bool(row) and persist_fii_dii_derivatives(db, row)
    return {"inserted": 1 if ok else 0, "detail": row or {"note": "no FAO report available"}}


def _run_manipulation(db) -> Dict[str, Any]:
    from forensics.manipulation_detectors import (
        detect_circular_pattern, detect_insider_exit_pattern,
    )
    cp = detect_circular_pattern(db)
    ie = detect_insider_exit_pattern(db)
    return {"inserted": len(cp) + len(ie),
            "detail": {"circular": len(cp), "insider_exit": len(ie)}}


def _run_promoter_events(db) -> Dict[str, Any]:
    from fundamentals.promoter_events import PromoterEventsFetcher
    try:
        from config import MONITORED_STOCKS
    except Exception:
        MONITORED_STOCKS = []
    r = PromoterEventsFetcher(db, MONITORED_STOCKS).refresh_all()
    total = sum(int(v) for v in (r.values() if isinstance(r, dict) else []))
    return {"inserted": total, "detail": {"tickers": len(MONITORED_STOCKS), "by_ticker": r}}


def _run_bse_filings(db) -> Dict[str, Any]:
    from forensics.bse_filing_fetcher import BSEFilingFetcher
    try:
        from config import MONITORED_STOCKS
    except Exception:
        MONITORED_STOCKS = []
    r = BSEFilingFetcher(db, MONITORED_STOCKS).refresh_all()
    total = sum(int(v) for v in (r.values() if isinstance(r, dict) else []))
    return {"inserted": total, "detail": {"tickers": len(MONITORED_STOCKS), "by_ticker_top10": dict(list(r.items())[:10]) if isinstance(r, dict) else None}}


def _run_nse_filings(db) -> Dict[str, Any]:
    from forensics.nse_filing_fetcher import NSEFilingFetcher
    try:
        from config import MONITORED_STOCKS
    except Exception:
        MONITORED_STOCKS = []
    r = NSEFilingFetcher(db, MONITORED_STOCKS).refresh_all()
    total = sum(int(v) for v in (r.values() if isinstance(r, dict) else []))
    return {"inserted": total, "detail": {"tickers": len(MONITORED_STOCKS), "by_ticker_top10": dict(list(r.items())[:10]) if isinstance(r, dict) else None}}


def _run_sebi_disclosures(db) -> Dict[str, Any]:
    from forensics.sebi_disclosures import SEBIDisclosuresFetcher
    try:
        from config import MONITORED_STOCKS
    except Exception:
        MONITORED_STOCKS = []
    r = SEBIDisclosuresFetcher(db, MONITORED_STOCKS).refresh_all()
    total = sum(int(v) for v in (r.values() if isinstance(r, dict) else []))
    return {"inserted": total, "detail": r if not isinstance(r, dict) or len(r) < 50 else "(truncated)"}


def _run_volume_anomalies(db) -> Dict[str, Any]:
    from volume_anomalies import compute_and_persist
    try:
        from config import MONITORED_STOCKS
    except Exception:
        MONITORED_STOCKS = []
    return compute_and_persist(db, MONITORED_STOCKS)


JOBS: Dict[str, Callable] = {
    "bulk_deals":       _run_bulk_deals,
    "fo_unusual":       _run_fo_unusual,
    "fo_fii_dii":       _run_fo_fii_dii,
    "manipulation":     _run_manipulation,
    "promoter_events":  _run_promoter_events,
    "bse_filings":      _run_bse_filings,
    "nse_filings":      _run_nse_filings,
    "sebi_disclosures": _run_sebi_disclosures,
    "volume_anomalies": _run_volume_anomalies,
}


# --- Logging to job_runs table -------------------------------------------

_ENSURED = False


def _ensure_job_runs(db) -> None:
    global _ENSURED
    if _ENSURED:
        return
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                duration_secs REAL,
                status TEXT NOT NULL,
                rows_in INTEGER DEFAULT 0,
                rows_inserted INTEGER DEFAULT 0,
                detail TEXT,
                triggered_by TEXT DEFAULT 'scheduler'
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobruns_name ON job_runs(job_name, started_at)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobruns_status ON job_runs(status, started_at)"
        )
        db.conn.commit()
        _ENSURED = True
    except Exception as exc:
        logger.warning("job_runs ensure failed: %s", exc)


def _log_run(db, *, job_name: str, started_at: datetime, finished_at: datetime,
             status: str, rows_inserted: int, detail: Any, triggered_by: str) -> None:
    try:
        _ensure_job_runs(db)
        cur = db.conn.cursor()
        duration = (finished_at - started_at).total_seconds()
        detail_str = json.dumps(detail, default=str) if not isinstance(detail, str) else detail
        cur.execute(
            """
            INSERT INTO job_runs
            (job_name, started_at, finished_at, duration_secs, status,
             rows_inserted, detail, triggered_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_name, started_at.isoformat(), finished_at.isoformat(),
             duration, status, int(rows_inserted), detail_str[:8000], triggered_by),
        )
        db.conn.commit()
    except Exception as exc:
        logger.warning("job_runs log failed for %s: %s", job_name, exc)


# --- Public API ----------------------------------------------------------

def run(job_name: str, db, *, triggered_by: str = "scheduler") -> Dict[str, Any]:
    """Run a single scraper job, log to job_runs, return summary dict.

    Never raises — returns {'ok': False, 'error': ...} on failure so callers
    (scheduler, admin endpoints) can stay simple.
    """
    fn = JOBS.get(job_name)
    if not fn:
        return {"ok": False, "error": f"unknown job: {job_name}",
                "known": sorted(JOBS.keys())}

    started = datetime.utcnow()
    t0 = time.time()
    try:
        result = fn(db)
        finished = datetime.utcnow()
        if isinstance(result, dict):
            inserted = int(result.get("inserted") or 0)
            detail = result.get("detail", {})
        elif isinstance(result, int):
            inserted = result
            detail = {}
        else:
            inserted = 0
            detail = {"result": str(result)[:500]}
        status = "ok" if inserted > 0 else "partial"
        _log_run(db, job_name=job_name, started_at=started, finished_at=finished,
                 status=status, rows_inserted=inserted, detail=detail,
                 triggered_by=triggered_by)
        return {"ok": True, "job": job_name, "inserted": inserted,
                "duration_secs": round(time.time() - t0, 2), "detail": detail}
    except Exception as exc:
        finished = datetime.utcnow()
        tb = traceback.format_exc(limit=4)
        logger.error("scrape_runner.%s failed: %s\n%s", job_name, exc, tb)
        _log_run(db, job_name=job_name, started_at=started, finished_at=finished,
                 status="error", rows_inserted=0,
                 detail={"error": str(exc), "trace": tb}, triggered_by=triggered_by)
        return {"ok": False, "job": job_name, "error": str(exc),
                "duration_secs": round(time.time() - t0, 2)}


def run_all(db, *, triggered_by: str = "cli") -> List[Dict[str, Any]]:
    """Run every registered job. Used by admin backfill + nightly health check."""
    return [run(name, db, triggered_by=triggered_by) for name in JOBS.keys()]


def recent_runs(db, *, limit: int = 100, job_name: Optional[str] = None,
                status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return recent rows from job_runs for admin dashboard."""
    _ensure_job_runs(db)
    cur = db.conn.cursor()
    sql = ("SELECT id, job_name, started_at, finished_at, duration_secs, "
           "status, rows_inserted, detail, triggered_by "
           "FROM job_runs WHERE 1=1 ")
    args: List[Any] = []
    if job_name:
        sql += "AND job_name = ? "
        args.append(job_name)
    if status:
        sql += "AND status = ? "
        args.append(status)
    sql += "ORDER BY started_at DESC LIMIT ?"
    args.append(int(limit))
    rows = cur.execute(sql, args).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        # rows can be sqlite3.Row OR plain tuple — handle both
        try:
            d = dict(r)
        except (TypeError, ValueError):
            d = {
                "id": r[0], "job_name": r[1], "started_at": r[2],
                "finished_at": r[3], "duration_secs": r[4], "status": r[5],
                "rows_inserted": r[6], "detail": r[7], "triggered_by": r[8],
            }
        try:
            d["detail"] = json.loads(d.get("detail") or "{}")
        except Exception:
            pass
        out.append(d)
    return out


def job_health(db) -> Dict[str, Any]:
    """One-line-per-job health summary for the admin dashboard.

    For each registered job: when did it last run, was it successful, how
    many rows? Used by /api/admin/jobs and the daily DQ check.
    """
    _ensure_job_runs(db)
    cur = db.conn.cursor()
    out: Dict[str, Any] = {}
    for name in JOBS.keys():
        cur.execute(
            "SELECT started_at, status, rows_inserted, duration_secs "
            "FROM job_runs WHERE job_name = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (name,),
        )
        r = cur.fetchone()
        if r is None:
            out[name] = {"status": "never_run"}
        else:
            try:
                d = dict(r)
            except (TypeError, ValueError):
                d = {"started_at": r[0], "status": r[1],
                     "rows_inserted": r[2], "duration_secs": r[3]}
            out[name] = d
    return out


# --- job_queue: async work queue for summarization, RAG indexing, etc. --
#
# Separate from `job_runs` (which logs scheduled scraper runs). This table
# holds discrete units of work pushed by the API or scrapers and drained
# by the worker container's `--daemon` loop.
#
# kinds we currently support:
#   summarize      payload = {"event_id": int}
#   explain_signal payload = {"signal_id": int}
#   index_chunks   payload = {"source": str, "source_id": str}
#   briefing       payload = {"user_id": int}

_QUEUE_ENSURED = False


def _ensure_job_queue(db) -> None:
    global _QUEUE_ENSURED
    if _QUEUE_ENSURED:
        return
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS job_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobqueue_pending "
            "ON job_queue(status, next_run_at)"
        )
        # Idempotency: never enqueue the same (kind, payload) twice while pending.
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobqueue_dedup "
            "ON job_queue(kind, payload_json) WHERE status IN ('pending','running')"
        )
        db.conn.commit()
        _QUEUE_ENSURED = True
    except Exception as exc:
        # WHERE-on-index isn't supported on older SQLite; fall back to non-unique
        try:
            cur = db.conn.cursor()
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobqueue_kind_payload "
                "ON job_queue(kind, payload_json)"
            )
            db.conn.commit()
            _QUEUE_ENSURED = True
        except Exception as exc2:
            logger.warning("job_queue ensure failed: %s / %s", exc, exc2)


def enqueue(db, kind: str, payload: Dict[str, Any]) -> Optional[int]:
    """Push a job. Returns row id, or None if a duplicate pending entry exists."""
    _ensure_job_queue(db)
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO job_queue (kind, payload_json, status, next_run_at) "
            "VALUES (?, ?, 'pending', CURRENT_TIMESTAMP)",
            (kind, json.dumps(payload, sort_keys=True, default=str)),
        )
        db.conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # duplicate — already pending
    except Exception as exc:
        logger.warning("job_queue enqueue(%s) failed: %s", kind, exc)
        return None


# Handlers map: kind → callable(db, payload) → dict result.
# Populated lazily so the import graph stays small for CLI use.
QUEUE_HANDLERS: Dict[str, Callable[[Any, Dict[str, Any]], Dict[str, Any]]] = {}


def register_queue_handler(kind: str, fn: Callable) -> None:
    QUEUE_HANDLERS[kind] = fn


def _drain_once(db, *, batch: int = 10) -> int:
    """Process up to `batch` pending jobs whose next_run_at is due.

    Exponential backoff on failure: next_run_at = now + min(900, 60*2**attempts).
    Dead-letter after 5 attempts (status='dead').
    """
    _ensure_job_queue(db)
    cur = db.conn.cursor()
    rows = cur.execute(
        "SELECT id, kind, payload_json, attempts FROM job_queue "
        "WHERE status = 'pending' AND next_run_at <= CURRENT_TIMESTAMP "
        "ORDER BY next_run_at ASC LIMIT ?", (int(batch),)
    ).fetchall()
    if not rows:
        return 0

    done = 0
    for r in rows:
        rid, kind, payload_json, attempts = r[0], r[1], r[2], r[3]
        handler = QUEUE_HANDLERS.get(kind)
        if not handler:
            # Unknown kind — skip indefinitely (mark dead so the queue clears)
            cur.execute(
                "UPDATE job_queue SET status='dead', last_error=?, finished_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (f"no handler registered for kind={kind}", rid)
            )
            db.conn.commit()
            continue

        # Mark running so a second drainer doesn't grab it
        cur.execute("UPDATE job_queue SET status='running' WHERE id=?", (rid,))
        db.conn.commit()
        try:
            payload = json.loads(payload_json)
            handler(db, payload)
            cur.execute(
                "UPDATE job_queue SET status='done', finished_at=CURRENT_TIMESTAMP "
                "WHERE id=?", (rid,)
            )
            db.conn.commit()
            done += 1
        except Exception as exc:
            attempts = (attempts or 0) + 1
            if attempts >= 5:
                cur.execute(
                    "UPDATE job_queue SET status='dead', attempts=?, last_error=?, "
                    "finished_at=CURRENT_TIMESTAMP WHERE id=?",
                    (attempts, str(exc)[:500], rid)
                )
            else:
                # Exponential backoff capped at 15 min
                backoff = min(900, 60 * (2 ** attempts))
                cur.execute(
                    "UPDATE job_queue SET status='pending', attempts=?, last_error=?, "
                    "next_run_at=datetime(CURRENT_TIMESTAMP, '+' || ? || ' seconds') "
                    "WHERE id=?",
                    (attempts, str(exc)[:500], backoff, rid)
                )
            db.conn.commit()
            logger.warning("job_queue handler %s failed (attempt %d): %s",
                           kind, attempts, exc)
    return done


def queue_depth(db) -> Dict[str, int]:
    """Return counts by status for the admin dashboard."""
    _ensure_job_queue(db)
    cur = db.conn.cursor()
    rows = cur.execute(
        "SELECT status, COUNT(*) FROM job_queue GROUP BY status"
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


# --- CLI for manual backfill ---------------------------------------------

def _cli_db():
    """Resolve a sqlite connection wrapper compatible with the scraper APIs."""
    db_path = ROOT / "data" / "tickwave.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    class _CLIDb:
        def __init__(self, c):
            self.conn = c
            self.is_postgres = False
    return _CLIDb(conn)


def _daemon_loop(db) -> None:
    """Long-running worker loop: starts APScheduler (via api.start_scheduler)
    and drains job_queue every 30 seconds. Blocks until SIGTERM."""
    import importlib
    import signal as _signal

    # Lazily import the api module to get the scheduler setup. Importing
    # backend.api builds the Flask app object but doesn't execute the
    # `if __name__ == '__main__'` block, so we control when the scheduler
    # actually starts.
    try:
        api_mod = importlib.import_module("backend.api")
    except ImportError:
        api_mod = importlib.import_module("api")

    logger.info("worker: starting APScheduler (RUN_SCHEDULER=%s)",
                os.getenv("RUN_SCHEDULER", "true"))
    try:
        api_mod.start_scheduler()
    except Exception as exc:
        logger.error("worker: start_scheduler failed: %s", exc, exc_info=True)

    # Register built-in queue handlers (lazy-imported on first use to keep
    # CLI startup fast and avoid heavy imports when not needed).
    def _ensure_handlers():
        if 'summarize' in QUEUE_HANDLERS:
            return
        try:
            from backend.ai_summarize import summarize_event_handler
            register_queue_handler('summarize', summarize_event_handler)
        except Exception as e:
            logger.debug("summarize handler not yet available: %s", e)
        try:
            from backend.ai_signal_explain import explain_signal_handler
            register_queue_handler('explain_signal', explain_signal_handler)
        except Exception as e:
            logger.debug("explain_signal handler not yet available: %s", e)
        try:
            from backend.rag import index_chunks_handler
            register_queue_handler('index_chunks', index_chunks_handler)
        except Exception as e:
            logger.debug("index_chunks handler not yet available: %s", e)
        try:
            from backend.daily_briefing import briefing_handler
            register_queue_handler('briefing', briefing_handler)
        except Exception as e:
            logger.debug("briefing handler not yet available: %s", e)
        # ── Phase 4 priority mail handlers ─────────────────────────
        try:
            from backend.priority_mail import (
                signal_handler as _pm_signal,
                forensic_handler as _pm_forensic,
                premarket_handler as _pm_premarket,
                earnings_handler as _pm_earnings,
            )
            register_queue_handler('priority_mail_signal',    _pm_signal)
            register_queue_handler('priority_mail_forensic',  _pm_forensic)
            register_queue_handler('priority_mail_premarket', _pm_premarket)
            register_queue_handler('priority_mail_earnings',  _pm_earnings)
        except Exception as e:
            logger.debug("priority_mail handlers not yet available: %s", e)

    _shutdown = {"flag": False}
    def _sigterm(signum, _frame):
        logger.info("worker: SIGTERM received, draining...")
        _shutdown["flag"] = True
    try:
        _signal.signal(_signal.SIGTERM, _sigterm)
        _signal.signal(_signal.SIGINT, _sigterm)
    except Exception:
        pass

    import os as _os
    drain_secs = int(_os.getenv("QUEUE_DRAIN_SECS", "30"))
    logger.info("worker: draining job_queue every %ds", drain_secs)
    while not _shutdown["flag"]:
        try:
            _ensure_handlers()
            n = _drain_once(db, batch=10)
            if n:
                logger.info("worker: drained %d jobs", n)
        except Exception as exc:
            logger.warning("worker: drain failed: %s", exc)
        time.sleep(drain_secs)
    logger.info("worker: shut down cleanly")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    args = sys.argv[1:]
    if args and args[0] == "--daemon":
        # Worker mode: scheduler + queue drainer, blocks until SIGTERM.
        db = _cli_db()
        _daemon_loop(db)
        sys.exit(0)

    db = _cli_db()
    targets = args or list(JOBS.keys())
    print(f"\nRunning {len(targets)} job(s): {targets}\n")
    results = [run(name, db, triggered_by="cli") for name in targets if name in JOBS]
    print("\n=== SUMMARY ===")
    for r in results:
        flag = "OK " if r.get("ok") and (r.get("inserted") or 0) > 0 else \
               "WARN" if r.get("ok") else "FAIL"
        line = f"  [{flag}] {r.get('job'):20s} inserted={r.get('inserted','-'):>6}"
        if not r.get("ok"):
            line += f"  err={r.get('error','')[:60]}"
        print(line)
    print()
