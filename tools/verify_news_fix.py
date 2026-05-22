"""Verify the news-tagging fix: confidence distribution + before/after stats."""
import os
import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
conn = sqlite3.connect(str(ROOT / "data" / "tickwave.db"))
conn.row_factory = sqlite3.Row
c = conn.cursor()

print("\n=== SIGNAL STATUS DISTRIBUTION ===")
for r in c.execute(
    "SELECT status, COUNT(*) AS n FROM signals GROUP BY status ORDER BY n DESC"
).fetchall():
    print(f"  {r['status']:15s} {r['n']:>6}")

print("\n=== SUBJECT_CONFIDENCE BUCKETS ===")
buckets = [
    ("VETO (0.00)",      "subject_confidence = 0"),
    ("weak  (0-0.40)",   "subject_confidence > 0 AND subject_confidence < 0.40"),
    ("medium (0.40-0.70)","subject_confidence >= 0.40 AND subject_confidence < 0.70"),
    ("strong (>= 0.70)", "subject_confidence >= 0.70"),
    ("NULL (not scored)","subject_confidence IS NULL"),
]
for label, where in buckets:
    n = c.execute(f"SELECT COUNT(*) FROM signals WHERE {where}").fetchone()[0]
    print(f"  {label:25s} {n:>6}")

print("\n=== ACTIVE signals — confidence bucket breakdown ===")
for label, where in buckets:
    n = c.execute(
        f"SELECT COUNT(*) FROM signals WHERE status='active' AND {where}"
    ).fetchone()[0]
    print(f"  {label:25s} {n:>6}")

print("\n=== news_type DISTRIBUTION (all signals) ===")
for r in c.execute(
    "SELECT news_type, COUNT(*) AS n FROM signals GROUP BY news_type ORDER BY n DESC"
).fetchall():
    print(f"  {(r['news_type'] or '(null)'):20s} {r['n']:>6}")

print("\n=== EXAMPLE: 5 highest-confidence active signals ===")
for r in c.execute(
    """SELECT ticker, subject_confidence AS sc, headline, news_type
       FROM signals
       WHERE status='active' AND subject_confidence IS NOT NULL
       ORDER BY subject_confidence DESC, alpha_score DESC
       LIMIT 5"""
).fetchall():
    hl = (r["headline"] or "")[:90]
    print(f"  {r['ticker']:10s} conf={r['sc']:.2f}  type={r['news_type']:15s}  {hl}")

print("\n=== EXAMPLE: 5 EXPIRED signals (the mis-tagged ones) ===")
for r in c.execute(
    """SELECT ticker, subject_confidence AS sc, headline, subject_evidence AS ev
       FROM signals
       WHERE status='expired' AND subject_confidence = 0
       LIMIT 5"""
).fetchall():
    hl = (r["headline"] or "")[:90]
    print(f"  {r['ticker']:10s} conf={r['sc']:.2f}  {hl}")

print("\n=== DEDUCTION ===")
total = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
expired_low = c.execute(
    "SELECT COUNT(*) FROM signals WHERE status='expired' AND subject_confidence < 0.20"
).fetchone()[0]
active = c.execute("SELECT COUNT(*) FROM signals WHERE status='active'").fetchone()[0]
print(f"  Total signals:                     {total}")
print(f"  Active signals:                    {active}")
print(f"  Expired (mis-tag, conf<0.20):      {expired_low}")
print(f"  Mis-tag rate fixed:                {100*expired_low/max(total,1):.1f}%")

conn.close()
