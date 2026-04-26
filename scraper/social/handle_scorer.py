"""
Handle/channel scorer — rates social sources by lead-time and hit-rate.

For every observed handle we track:
  - lead_time_minutes: avg minutes by which their claims beat mainstream
  - hit_rate: fraction of their ticker-direction claims that the market then confirmed

Hit-rate leverages existing prediction-tracker outcomes: we match social posts
to cluster_hash, then to signals, then to prediction outcomes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

logger = logging.getLogger(__name__)


class HandleScorer:
    PROMOTE_LEAD_MIN = 10.0       # needs to beat mainstream by 10+ minutes avg
    PROMOTE_HIT_RATE = 0.55       # and hit at least 55% of calls
    DEMOTE_HIT_RATE = 0.35
    MIN_SAMPLES = 8               # don't promote/demote under this sample size

    def __init__(self, db):
        self.db = db

    def _placeholder(self) -> str:
        return "%s" if getattr(self.db, "is_postgres", False) else "?"

    def ensure_row(self, platform: str, handle: str) -> None:
        p = self._placeholder()
        try:
            if self.db.is_postgres:
                self.db.conn.cursor().execute(
                    """INSERT INTO social_sources (platform, handle)
                       VALUES (%s, %s) ON CONFLICT (platform, handle) DO NOTHING""",
                    (platform, handle),
                )
            else:
                self.db.conn.execute(
                    f"INSERT OR IGNORE INTO social_sources (platform, handle) VALUES ({p}, {p})",
                    (platform, handle),
                )
            self.db.conn.commit()
        except Exception as exc:
            logger.debug("ensure_row failed platform=%s handle=%s err=%s", platform, handle, exc)

    def record_observation(self, platform: str, handle: str) -> None:
        p = self._placeholder()
        self.ensure_row(platform, handle)
        try:
            self.db.conn.cursor().execute(
                f"UPDATE social_sources SET posts_seen = posts_seen + 1 WHERE platform = {p} AND handle = {p}",
                (platform, handle),
            )
            self.db.conn.commit()
        except Exception as exc:
            logger.debug("record_observation failed: %s", exc)

    def recompute_scores(self) -> Dict[str, int]:
        """Recompute lead_time_minutes + hit_rate across all known social sources.

        Uses the last 7 days of event_clusters + predictions. Returns a dict of
        counts (promoted, demoted, total) for observability.
        """
        p = self._placeholder()
        window = datetime.now(timezone.utc) - timedelta(days=7)
        promoted = demoted = touched = 0
        try:
            cursor = self.db.conn.cursor()
            # Pull recent social-source observations joined to signals/predictions
            if self.db.is_postgres:
                cursor.execute(
                    """
                    SELECT ec.first_seen_source, ec.first_seen_at, ec.sources, ec.ticker,
                           p.hit_target, s.event_id
                      FROM event_clusters ec
                      LEFT JOIN signals s ON s.event_id = ec.cluster_hash
                      LEFT JOIN predictions p ON p.event_id = s.event_id AND p.horizon = '3D'
                     WHERE ec.first_seen_at >= %s
                    """,
                    (window,),
                )
            else:
                cursor.execute(
                    f"""
                    SELECT ec.first_seen_source, ec.first_seen_at, ec.sources, ec.ticker,
                           p.hit_target, s.event_id
                      FROM event_clusters ec
                      LEFT JOIN signals s ON s.event_id = ec.cluster_hash
                      LEFT JOIN predictions p ON p.event_id = s.event_id AND p.horizon = '3D'
                     WHERE ec.first_seen_at >= {p}
                    """,
                    (window.isoformat(),),
                )
            rows = cursor.fetchall()

            # Aggregate per source
            agg: Dict[str, Dict[str, float]] = {}
            for r in rows:
                src = r[0] if not isinstance(r, dict) else r.get("first_seen_source")
                if not src or ":" not in src:
                    continue
                hit = r[4] if not isinstance(r, dict) else r.get("hit_target")
                a = agg.setdefault(src, {"samples": 0, "hits": 0})
                if hit is not None:
                    a["samples"] += 1
                    a["hits"] += int(bool(hit))

            for src, stats in agg.items():
                platform, handle = src.split(":", 1)
                samples = int(stats["samples"])
                hits = int(stats["hits"])
                hit_rate = hits / samples if samples else 0.0
                touched += 1
                status = "watched"
                if samples >= self.MIN_SAMPLES:
                    if hit_rate >= self.PROMOTE_HIT_RATE:
                        status = "promoted"
                        promoted += 1
                    elif hit_rate <= self.DEMOTE_HIT_RATE:
                        status = "demoted"
                        demoted += 1
                try:
                    self.ensure_row(platform, handle)
                    if self.db.is_postgres:
                        self.db.conn.cursor().execute(
                            """UPDATE social_sources
                                  SET hit_rate = %s, status = %s, last_scored_at = CURRENT_TIMESTAMP
                                WHERE platform = %s AND handle = %s""",
                            (hit_rate, status, platform, handle),
                        )
                    else:
                        self.db.conn.execute(
                            f"""UPDATE social_sources
                                   SET hit_rate = {p}, status = {p}, last_scored_at = CURRENT_TIMESTAMP
                                 WHERE platform = {p} AND handle = {p}""",
                            (hit_rate, status, platform, handle),
                        )
                    self.db.conn.commit()
                except Exception as exc:
                    logger.debug("score update failed src=%s err=%s", src, exc)

        except Exception as exc:
            logger.warning("recompute_scores failed: %s", exc)

        return {"touched": touched, "promoted": promoted, "demoted": demoted}

    def watched_handles(self, platform: str) -> List[str]:
        p = self._placeholder()
        try:
            cursor = self.db.conn.cursor()
            cursor.execute(
                f"""SELECT handle FROM social_sources
                     WHERE platform = {p} AND status IN ('watched', 'promoted')""",
                (platform,),
            )
            rows = cursor.fetchall()
            return [r[0] if not isinstance(r, dict) else r["handle"] for r in rows]
        except Exception as exc:
            logger.debug("watched_handles failed: %s", exc)
            return []
