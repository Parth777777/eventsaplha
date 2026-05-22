"""
cluster_persister — write in-memory cluster_events() output to event_clusters.

Bridges the gap between source_tiering.cluster_events() (which returns
in-memory ClusteredEvent objects each scrape cycle) and the persistent
event_clusters table (which existed but was only populated by the social
scraper). Without this, RSS articles never accumulated cross-source
confirmation history — clusters vanished after each cycle.

Addendum 2026-05-18 (multi-source aggregation):
  - Same upsert pattern as scraper/social/publish_priority.py:53-113
  - De-duplicates sources within a single cluster on each upsert (the
    legacy social path appends blindly, double-counting outlets)
  - Returns {link_or_title: cluster_hash} so the caller can tag each
    signal row with its cluster_hash for downstream JOINs

Public API:
  persist_clusters(db, clustered_events, ticker_resolver=None) -> dict[str, str]

Zero-LLM cost — pure SQL upserts.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def _placeholder(db) -> str:
    return "%s" if getattr(db, "is_postgres", False) else "?"


def _ts_to_iso(ts) -> str:
    """ClusteredEvent timestamps are epoch floats; convert for portable storage."""
    try:
        return datetime.utcfromtimestamp(float(ts)).isoformat()
    except Exception:
        return datetime.utcnow().isoformat()


def _compute_cluster_hash(ticker: Optional[str], canonical_title: str) -> str:
    """Same hash function used by social/common.py — keep the namespace shared
    so a story breaking on Twitter and confirmed on Mint lands in ONE cluster."""
    try:
        from scraper.social.common import normalize_headline, cluster_hash as _ch
    except Exception:
        from social.common import normalize_headline, cluster_hash as _ch  # type: ignore
    return _ch(ticker, normalize_headline(canonical_title or ""))


def _existing_sources(db, cluster_hash: str) -> List[str]:
    """Pull the comma-separated sources list for an existing cluster.
    Returns [] if cluster doesn't exist yet."""
    p = _placeholder(db)
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"SELECT sources FROM event_clusters WHERE cluster_hash = {p}",
            (cluster_hash,),
        )
        row = cur.fetchone()
        if row is None:
            return []
        raw = row[0] if not isinstance(row, dict) else row.get("sources")
        return [s.strip() for s in (raw or "").split(",") if s.strip()]
    except Exception:
        return []


def _upsert_cluster(db, ch: str, headline: str, ticker: Optional[str],
                    new_sources: List[str], first_seen_iso: str) -> bool:
    """Insert or update one cluster row. Returns True on success."""
    p = _placeholder(db)
    is_pg = getattr(db, "is_postgres", False)
    if not new_sources:
        return False

    try:
        cursor = db.conn.cursor()
        # De-dup against existing sources to avoid double-counting
        existing = _existing_sources(db, ch)
        seen = set(existing)
        merged = list(existing)
        for s in new_sources:
            if s and s not in seen:
                seen.add(s)
                merged.append(s)
        merged_str = ",".join(merged)
        merged_count = len(merged)
        first_seen_source = (existing[0] if existing else new_sources[0])

        if is_pg:
            cursor.execute(
                """INSERT INTO event_clusters
                       (cluster_hash, canonical_headline, ticker,
                        first_seen_at, first_seen_source, member_count, sources)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (cluster_hash) DO UPDATE SET
                       member_count = EXCLUDED.member_count,
                       sources      = EXCLUDED.sources""",
                (ch, headline[:300], ticker, first_seen_iso,
                 first_seen_source, merged_count, merged_str),
            )
        else:
            # SQLite: probe then INSERT or UPDATE explicitly
            cursor.execute(
                f"SELECT id FROM event_clusters WHERE cluster_hash = {p}",
                (ch,),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    f"""INSERT INTO event_clusters
                            (cluster_hash, canonical_headline, ticker,
                             first_seen_at, first_seen_source, member_count, sources)
                            VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})""",
                    (ch, headline[:300], ticker, first_seen_iso,
                     first_seen_source, merged_count, merged_str),
                )
            else:
                cluster_id = row[0] if not isinstance(row, dict) else row["id"]
                cursor.execute(
                    f"""UPDATE event_clusters SET
                            member_count = {p},
                            sources      = {p}
                        WHERE id = {p}""",
                    (merged_count, merged_str, cluster_id),
                )
        db.conn.commit()
        return True
    except Exception as exc:
        logger.warning("cluster upsert failed hash=%s err=%s", ch[:8], exc)
        try:
            db.conn.rollback()
        except Exception:
            pass
        return False


def persist_clusters(
    db,
    clustered_events: Iterable,
    ticker_resolver: Optional[Callable[[object], Optional[str]]] = None,
) -> Dict[str, str]:
    """Upsert each clustered event into event_clusters. Returns a map of
    `{canonical_title -> cluster_hash}` and `{link -> cluster_hash}` so the
    caller can tag downstream signals/events with the right hash.

    Args:
        db: TickwaveDB instance.
        clustered_events: iterable of source_tiering.ClusteredEvent.
        ticker_resolver: optional callable(cluster) -> str|None. Used when
            ClusteredEvent doesn't carry an explicit ticker. Defaults to None.

    Returns:
        dict mapping {key -> cluster_hash}. Keys are both the canonical_title
        AND each source link in the cluster, so callers can do flexible lookup.
    """
    if not db or clustered_events is None:
        return {}

    out: Dict[str, str] = {}
    persisted = 0
    skipped = 0

    for cl in clustered_events:
        try:
            title = getattr(cl, "canonical_title", None) or ""
            sources = getattr(cl, "sources", []) or []
            if not title or not sources:
                skipped += 1
                continue

            ticker = None
            if ticker_resolver is not None:
                try:
                    ticker = ticker_resolver(cl)
                except Exception:
                    pass

            ch = _compute_cluster_hash(ticker, title)
            source_names = []
            for s in sources:
                if not isinstance(s, dict):
                    continue
                name = s.get("canonical") or s.get("name") or s.get("source")
                if name:
                    source_names.append(str(name))
            if not source_names:
                skipped += 1
                continue

            earliest = getattr(cl, "earliest_ts", 0) or 0
            ok = _upsert_cluster(
                db, ch, title, ticker, source_names, _ts_to_iso(earliest)
            )
            if not ok:
                skipped += 1
                continue

            # Index by both title and each source link for downstream lookup
            out[title] = ch
            for s in sources:
                if isinstance(s, dict) and s.get("link"):
                    out[s["link"]] = ch
            persisted += 1
        except Exception as exc:
            logger.debug("persist_clusters: cluster failed err=%s", exc)
            skipped += 1

    if persisted or skipped:
        logger.info("cluster_persister: %d persisted, %d skipped", persisted, skipped)
    return out
