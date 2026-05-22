"""Backfill subject_confidence + news_type on existing signals + events.

The entity_linker fix only helps signals scraped AFTER deployment. This script
re-scores everything already in the DB, marks low-confidence rows, and
populates news_type from the source URL.

Usage:
  python tools/backfill_subject_confidence.py             # dry-run, print stats
  python tools/backfill_subject_confidence.py --apply     # write back
  python tools/backfill_subject_confidence.py --apply --expire-below 0.20
        # additionally flip status='mismatch' for confidence < 0.20

Recommended first run: dry-run, look at distribution. Then --apply.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# Force UTF-8 stdout on Windows so currency/unicode in headlines doesn't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "scraper"))

import entity_linker
from config import STOCK_COMPANIES
from stock_universe import UNIVERSE_SHORT_NAMES


DB_PATH = ROOT / "data" / "tickwave.db"


def backfill(apply: bool, expire_below: float, limit: int) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1) Pull signals that don't yet have subject_confidence
    print(f"\n=== SIGNALS BACKFILL (limit={limit}) ===")
    sig_rows = cur.execute(
        """
        SELECT s.id, s.event_id, s.ticker, s.headline, s.source, s.status,
               COALESCE(e.title, s.headline) AS title,
               COALESCE(e.summary, '') AS summary,
               COALESCE(e.link, '') AS link
        FROM signals s
        LEFT JOIN events e ON e.event_id = s.event_id
        WHERE s.subject_confidence IS NULL
        ORDER BY s.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    print(f"  scanning {len(sig_rows)} signals without subject_confidence")

    conf_buckets = Counter()
    type_buckets = Counter()
    updates: list[tuple[float, str, str, int]] = []
    expire_ids: list[int] = []
    examples_low: list[dict] = []

    for r in sig_rows:
        title = r["title"] or r["headline"] or ""
        summary = r["summary"] or ""
        text = f"{title} {summary}".strip()
        ticker = (r["ticker"] or "").upper()
        if not ticker or not text:
            continue
        conf, ev = entity_linker.score(
            ticker, text, title=title,
            short_names_map=UNIVERSE_SHORT_NAMES,
            companies_map=STOCK_COMPANIES,
        )
        news_type = entity_linker.classify_news_type(r["source"] or "", r["link"] or "")
        # Bucket for visibility
        if conf >= 0.7:
            conf_buckets["strong"] += 1
        elif conf >= 0.4:
            conf_buckets["medium"] += 1
        elif conf > 0.0:
            conf_buckets["weak"] += 1
        else:
            conf_buckets["veto"] += 1
        type_buckets[news_type] += 1

        updates.append((conf, json.dumps(ev, default=str)[:1000], news_type, r["id"]))
        if expire_below and conf < expire_below and (r["status"] or "active") == "active":
            expire_ids.append(r["id"])
            if len(examples_low) < 10:
                examples_low.append({
                    "id": r["id"], "ticker": ticker, "conf": conf,
                    "title": title[:120], "reasons": ev.get("reasons"),
                })

    print(f"\n  confidence buckets: {dict(conf_buckets)}")
    print(f"  news_type buckets:  {dict(type_buckets)}")
    if expire_ids:
        print(f"\n  would expire {len(expire_ids)} signals with conf < {expire_below}")
        print("  examples of mis-tagged signals about to be expired:")
        for e in examples_low:
            print(f"    [{e['ticker']:10}] conf={e['conf']:.2f}  {e['title'][:90]}  reasons={e['reasons']}")

    # 2) Same for events
    print(f"\n=== EVENTS BACKFILL ===")
    # events.companies is JSON; for now backfill news_type only (per-ticker
    # confidence on multi-company events would require schema change).
    ev_rows = cur.execute(
        """
        SELECT id, source, link
        FROM events
        WHERE news_type IS NULL OR news_type = 'news_article'
        ORDER BY published_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    ev_type_buckets = Counter()
    ev_updates: list[tuple[str, int]] = []
    for r in ev_rows:
        nt = entity_linker.classify_news_type(r["source"] or "", r["link"] or "")
        ev_type_buckets[nt] += 1
        ev_updates.append((nt, r["id"]))
    print(f"  scanning {len(ev_rows)} events, new news_type distribution: {dict(ev_type_buckets)}")

    # 3) Apply if requested
    if not apply:
        print("\n  (dry-run; pass --apply to write back)")
        conn.close()
        return

    print("\n  writing back signals…")
    cur.executemany(
        "UPDATE signals SET subject_confidence=?, subject_evidence=?, news_type=? WHERE id=?",
        updates,
    )
    print(f"  updated {len(updates)} signals")

    if expire_ids:
        # Use existing 'expired' status (signals.expire_stale_signals uses
        # this) rather than introducing a new value the rest of the app
        # doesn't know how to display.
        q = "UPDATE signals SET status='expired' WHERE id IN (" + ",".join("?" * len(expire_ids)) + ")"
        cur.execute(q, expire_ids)
        print(f"  expired {cur.rowcount} mis-tagged signals (conf < {expire_below})")

    print("\n  writing back events…")
    cur.executemany("UPDATE events SET news_type=? WHERE id=?", ev_updates)
    print(f"  updated {len(ev_updates)} events")

    conn.commit()
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="actually write the changes (default: dry-run)")
    p.add_argument("--expire-below", type=float, default=0.0,
                   help="expire active signals whose subject_confidence is below this (use 0.20 for safe cleanup)")
    p.add_argument("--limit", type=int, default=20000,
                   help="max rows to backfill per table (default 20000)")
    args = p.parse_args()
    backfill(apply=args.apply, expire_below=args.expire_below, limit=args.limit)
