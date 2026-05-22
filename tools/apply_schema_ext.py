"""Apply schema_ext migrations to the live SQLite DB at data/tickwave.db.

Fixes errors like: `table filings has no column named exchange`.
Safe to run repeatedly — every statement is IF NOT EXISTS / probe-and-skip.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "scraper"))
sys.path.insert(0, str(ROOT / "backend"))

import sqlite3

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "data" / "tickwave.db"

import schema_ext  # noqa: E402

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row


# schema_ext.apply takes a raw DB connection (it calls conn.cursor() directly)
schema_ext.apply(conn)

# Also create the volume_anomalies table the audit flagged as missing
cur = conn.cursor()
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS volume_anomalies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        as_of_date TEXT NOT NULL,
        z_score REAL NOT NULL,
        volume INTEGER,
        avg_volume_20d INTEGER,
        close REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(ticker, as_of_date)
    )
    """
)
cur.execute(
    "CREATE INDEX IF NOT EXISTS idx_volanom_tkr_date "
    "ON volume_anomalies(ticker, as_of_date)"
)
cur.execute(
    "CREATE INDEX IF NOT EXISTS idx_volanom_zscore "
    "ON volume_anomalies(z_score)"
)

# job_runs: persistent log of every scraper run so failures aren't silent
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS job_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name TEXT NOT NULL,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP,
        duration_secs REAL,
        status TEXT NOT NULL,       -- 'ok' | 'error' | 'partial'
        rows_in INTEGER DEFAULT 0,
        rows_inserted INTEGER DEFAULT 0,
        detail TEXT,                -- JSON summary or error string
        triggered_by TEXT DEFAULT 'scheduler'   -- 'scheduler' | 'admin' | 'cli'
    )
    """
)
cur.execute("CREATE INDEX IF NOT EXISTS idx_jobruns_name ON job_runs(job_name, started_at)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_jobruns_status ON job_runs(status, started_at)")
conn.commit()

print("\n--- POST-MIGRATION COLUMN CHECK ---")
for tbl in ("filings", "events", "signals"):
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({tbl})").fetchall()]
    has_exchange = "exchange" in cols
    has_summary = "summary" in cols
    print(f"  {tbl}: exchange={has_exchange} summary={has_summary} (cols={len(cols)})")

print("\n--- NEW TABLES CHECK ---")
for tbl in ("volume_anomalies", "job_runs"):
    n = cur.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (tbl,),
    ).fetchone()[0]
    print(f"  {tbl}: {'OK' if n else 'MISSING'}")

conn.close()
print("\nschema_ext applied to data/tickwave.db")
