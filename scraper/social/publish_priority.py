"""
Publish-priority tracker: records first-seen timestamps per cluster across sources.

When a signal's event cluster was first seen on a social/non-mainstream source
before any RSS outlet, we mark `priority_boost=True` and record `edge_minutes`
(how many minutes ahead social was).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Sources we consider "mainstream" — seeing the same cluster from them
# first is baseline, not an edge.
MAINSTREAM_SOURCES = {
    "et_markets", "et_stocks", "mint_markets", "mint_companies",
    "moneycontrol_markets", "moneycontrol_stocks", "bs_markets",
    "rbi_press", "bse_announcements", "newsapi", "google_news",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


class PublishPriorityTracker:
    """Keeps a per-cluster first-seen record and decorates posts/events.

    State is persisted in `event_clusters` table via an EventAlphaDB instance.
    """

    def __init__(self, db):
        self.db = db

    def _placeholder(self) -> str:
        return "%s" if getattr(self.db, "is_postgres", False) else "?"

    def _upsert_cluster(self, cluster_hash: str, headline: str, ticker: Optional[str],
                        source: str, seen_at: datetime) -> Dict:
        """Insert cluster if new, update member list if existing. Returns current cluster row."""
        p = self._placeholder()
        cursor = self.db.conn.cursor()
        try:
            if self.db.is_postgres:
                cursor.execute(
                    """
                    INSERT INTO event_clusters (cluster_hash, canonical_headline, ticker,
                        first_seen_at, first_seen_source, member_count, sources)
                    VALUES (%s, %s, %s, %s, %s, 1, %s)
                    ON CONFLICT (cluster_hash) DO UPDATE SET
                        member_count = event_clusters.member_count + 1,
                        sources = event_clusters.sources || ',' || EXCLUDED.sources
                    """,
                    (cluster_hash, headline, ticker, seen_at, source, source),
                )
            else:
                cursor.execute(
                    f"SELECT id, first_seen_at, first_seen_source, sources, member_count FROM event_clusters WHERE cluster_hash = {p}",
                    (cluster_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        f"""INSERT INTO event_clusters (cluster_hash, canonical_headline, ticker,
                            first_seen_at, first_seen_source, member_count, sources)
                            VALUES ({p}, {p}, {p}, {p}, {p}, 1, {p})""",
                        (cluster_hash, headline, ticker, seen_at.isoformat(), source, source),
                    )
                else:
                    sources_now = (row["sources"] or "") + ("," + source)
                    cursor.execute(
                        f"UPDATE event_clusters SET member_count = member_count + 1, sources = {p} WHERE id = {p}",
                        (sources_now, row["id"]),
                    )
            self.db.conn.commit()

            cursor.execute(
                f"""SELECT cluster_hash, first_seen_at, first_seen_source, sources, member_count
                    FROM event_clusters WHERE cluster_hash = {p}""",
                (cluster_hash,),
            )
            row = cursor.fetchone()
            if row is None:
                return {}
            if self.db.is_postgres:
                return {
                    "cluster_hash": row[0],
                    "first_seen_at": row[1],
                    "first_seen_source": row[2],
                    "sources": row[3] or "",
                    "member_count": row[4] or 1,
                }
            else:
                return dict(row)
        except Exception as exc:
            logger.warning("cluster upsert failed: %s", exc)
            self.db.conn.rollback()
            return {}

    def record(self, cluster_hash: str, headline: str, ticker: Optional[str],
               source: str, seen_at: Optional[datetime] = None) -> Dict:
        """Record an observation. Returns decoration fields to attach to the event/signal."""
        seen_at = seen_at or _now()
        cluster = self._upsert_cluster(cluster_hash, headline[:300], ticker, source, seen_at)
        if not cluster:
            return {"first_seen_source": source, "priority_boost": False, "edge_minutes": None}

        first_seen = _parse_ts(cluster.get("first_seen_at")) or seen_at
        first_source = cluster.get("first_seen_source") or source
        sources_list = [s for s in (cluster.get("sources") or "").split(",") if s]

        priority_boost = False
        edge_minutes: Optional[float] = None

        # We have a boost only if the FIRST source is non-mainstream AND mainstream has now caught up
        if first_source not in MAINSTREAM_SOURCES and any(s in MAINSTREAM_SOURCES for s in sources_list):
            priority_boost = True
            # How many minutes did social beat mainstream by?
            # Rough: use delta between first_seen and now (this is when mainstream caught up)
            edge_minutes = max((seen_at - first_seen).total_seconds() / 60.0, 0.0)

        return {
            "first_seen_source": first_source,
            "priority_boost": priority_boost,
            "edge_minutes": round(edge_minutes, 2) if edge_minutes is not None else None,
            "sources_seen": sources_list,
        }
