"""
source_credibility_backfill — compute per-outlet hit-rate from resolved
predictions, write into source_credibility.

Joins signals → predictions (already linked by event_id) → event_clusters
(by cluster_hash), then attributes each resolved prediction's hit/miss
to EVERY outlet that contributed to the cluster. After 90 days of data,
each outlet's hit_rate reflects whether stories it reported actually
moved their predicted direction.

Designed to run nightly via APScheduler. Zero-LLM cost.

Public API:
  recompute_source_credibility(db, days=90, min_sample=5) -> dict
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict

logger = logging.getLogger(__name__)


def _placeholder(db) -> str:
    return "%s" if getattr(db, "is_postgres", False) else "?"


def recompute_source_credibility(db, days: int = 90, min_sample: int = 5) -> Dict:
    """Re-aggregate per-source hit rates from the last `days` of resolved
    predictions and upsert into source_credibility. Returns stats dict.

    Postgres has LATERAL unnest available; SQLite doesn't, so we always
    use the cross-dialect Python aggregation path. Slower but uniform.
    """
    if not db:
        return {"updated": 0, "sources": 0, "error": "no db"}

    p = _placeholder(db)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    is_pg = getattr(db, "is_postgres", False)

    # Pull every (cluster sources, hit_target) tuple in the window
    sql = (
        "SELECT ec.sources, p.hit_target "
        "  FROM signals s "
        "  JOIN predictions p ON p.event_id = s.event_id "
        "  JOIN event_clusters ec ON ec.cluster_hash = s.cluster_hash "
        f" WHERE p.hit_target IS NOT NULL "
        f"   AND s.created_at >= {p}"
    )
    try:
        cur = db.conn.cursor()
        cur.execute(sql, (cutoff,))
        rows = cur.fetchall() or []
    except Exception as exc:
        logger.warning("source_credibility query failed: %s", exc)
        return {"updated": 0, "sources": 0, "error": str(exc)[:200]}

    # Aggregate in Python — dict[source_name] = [hits, total]
    per_source: Dict[str, list] = {}
    for row in rows:
        if isinstance(row, dict):
            sources_raw = row.get("sources") or ""
            hit = row.get("hit_target")
        else:
            sources_raw, hit = row[0] or "", row[1]
        try:
            hit_int = 1 if hit in (1, True, "1", "true") else 0
        except Exception:
            hit_int = 0
        # De-dup sources within the same cluster row so an outlet listed
        # twice doesn't double-count
        seen = set()
        for src in sources_raw.split(","):
            src = (src or "").strip()
            if not src or src in seen:
                continue
            seen.add(src)
            bucket = per_source.setdefault(src, [0, 0])
            bucket[0] += hit_int
            bucket[1] += 1

    updated = 0
    for outlet, (hits, total) in per_source.items():
        if total < min_sample:
            continue
        hit_rate = hits / total if total else 0.0
        # credibility_score is just hit_rate for now; can blend with
        # sample_size confidence later (e.g. Wilson lower-bound).
        cred = hit_rate
        try:
            cur = db.conn.cursor()
            if is_pg:
                cur.execute(
                    """INSERT INTO source_credibility
                           (outlet, author, hit_rate, sample_size, credibility_score)
                       VALUES (%s, NULL, %s, %s, %s)
                       ON CONFLICT (outlet, author) DO UPDATE SET
                           hit_rate = EXCLUDED.hit_rate,
                           sample_size = EXCLUDED.sample_size,
                           credibility_score = EXCLUDED.credibility_score,
                           last_computed_at = NOW()""",
                    (outlet, hit_rate, total, cred),
                )
            else:
                # SQLite: probe by (outlet, NULL) since author can be NULL
                cur.execute(
                    f"SELECT id FROM source_credibility WHERE outlet = {p} AND author IS NULL",
                    (outlet,),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        f"""INSERT INTO source_credibility
                                (outlet, hit_rate, sample_size, credibility_score)
                                VALUES ({p}, {p}, {p}, {p})""",
                        (outlet, hit_rate, total, cred),
                    )
                else:
                    row_id = row[0] if not isinstance(row, dict) else row["id"]
                    cur.execute(
                        f"""UPDATE source_credibility SET
                                hit_rate = {p}, sample_size = {p},
                                credibility_score = {p},
                                last_computed_at = CURRENT_TIMESTAMP
                            WHERE id = {p}""",
                        (hit_rate, total, cred, row_id),
                    )
            db.conn.commit()
            updated += 1
        except Exception as exc:
            logger.debug("upsert source_credibility outlet=%s err=%s", outlet, exc)
            try: db.conn.rollback()
            except Exception: pass

    out = {"updated": updated, "sources": len(per_source), "window_days": days}
    logger.info("source_credibility refreshed: %s", out)
    return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from database_schema import TickwaveDB
    logging.basicConfig(level=logging.INFO)
    db = TickwaveDB()
    print(recompute_source_credibility(db))
