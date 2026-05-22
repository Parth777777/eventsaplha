"""One-shot DB audit: row counts for key tables in data/tickwave.db."""
import os
import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "tickwave.db")

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
cur = db.cursor()

tables = [r[0] for r in cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
).fetchall()]
print(f"DB: {DB_PATH}  | tables: {len(tables)}")

key_tables = [
    "signals", "events", "predictions", "bulk_deals", "sebi_disclosures",
    "promoter_events", "promoter_holdings", "filings", "filing_numbers",
    "fo_unusual", "volume_anomalies", "manipulation_flags",
    "manipulation_scores", "social_items", "sebi_bans", "users",
    "subscriptions", "payments", "watchlist", "alerts", "notifications",
    "event_clusters",
]
print("\n--- ROW COUNTS ---")
for t in key_tables:
    if t in tables:
        try:
            n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:30s} {n:>10}")
        except Exception as e:
            print(f"  {t:30s} ERR: {e}")
    else:
        print(f"  {t:30s} (missing)")

print("\n--- FRESHNESS (7d) ---")
fresh_queries = {
    "signals": "SELECT COUNT(*) FROM signals WHERE created_at >= datetime('now','-7 days')",
    "events": "SELECT COUNT(*) FROM events WHERE published_at >= datetime('now','-7 days')",
    "bulk_deals": "SELECT COUNT(*) FROM bulk_deals WHERE deal_date >= date('now','-7 days')",
    "fo_unusual": "SELECT COUNT(*) FROM fo_unusual WHERE fetched_at >= datetime('now','-7 days')",
    "filings": "SELECT COUNT(*) FROM filings WHERE filed_at >= datetime('now','-7 days')",
    "promoter_events": "SELECT COUNT(*) FROM promoter_events WHERE event_date >= date('now','-7 days')",
    "sebi_disclosures": "SELECT COUNT(*) FROM sebi_disclosures WHERE transaction_date >= date('now','-7 days')",
    "social_items": "SELECT COUNT(*) FROM social_items WHERE posted_at >= datetime('now','-7 days')",
}
for label, q in fresh_queries.items():
    if label in tables:
        try:
            n = cur.execute(q).fetchone()[0]
            print(f"  {label:30s} {n:>10}")
        except Exception as e:
            print(f"  {label:30s} ERR: {e}")

print("\n--- RECENT SIGNALS (top 5) ---")
try:
    rows = cur.execute(
        "SELECT ticker, alpha_score, headline, created_at FROM signals "
        "ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    for r in rows:
        hl = (r["headline"] or "")[:80]
        print(f"  {r['ticker']:12} alpha={r['alpha_score']} {r['created_at']}  {hl}")
except Exception as e:
    print(f"  signals query failed: {e}")

db.close()
