"""API v2 routes: status/health, fast news priority feed, smart-alerts CRUD,
forensic-tagged event feed.

Mount via: api_v2.register(app, get_db, scraper_status_ref)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from flask import Blueprint, Response, g, jsonify, request

from cache_util import ttl_cache  # response caching for hot endpoints

logger = logging.getLogger(__name__)

bp = Blueprint("api_v2", __name__)

# Filled in by register()
_get_db: Optional[Callable] = None
_scraper_status: Optional[Dict] = None
_started_at: float = time.time()


# ---- /api/status ------------------------------------------------------------

@bp.route("/api/status", methods=["GET"])
def api_status():
    """Comprehensive product health page.

    Drives the frontend "is the data fresh?" banner and the public trust panel.
    Never raises — every probe wraps in try/except so a single bad subsystem
    can't 500 the status page itself.
    """
    db = _get_db() if _get_db else None
    out: Dict = {
        "ok": True,
        "uptime_secs": int(time.time() - _started_at),
        "now": datetime.utcnow().isoformat() + "Z",
        "version": os.getenv("APP_VERSION", "dev"),
    }

    # Scraper
    try:
        ss = _scraper_status or {}
        last = ss.get("last_run")
        age_min = None
        if last:
            try:
                age_min = (datetime.utcnow() - datetime.fromisoformat(last.replace("Z", "")).replace(tzinfo=None)).total_seconds() / 60.0
            except Exception:
                age_min = None
        out["scraper"] = {
            "running": bool(ss.get("running")),
            "last_run": last,
            "last_run_age_min": round(age_min, 2) if age_min is not None else None,
            "last_result": ss.get("last_result"),
            "last_error": ss.get("error"),
            "stale": (age_min is not None and age_min > 15),
        }
    except Exception as e:
        out["scraper"] = {"error": str(e)}

    # DB row counts (cheap if indexed)
    try:
        if db:
            cur = db.conn.cursor()
            counts: Dict[str, int] = {}
            for table in ("signals", "events", "predictions"):
                try:
                    cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                    counts[table] = int((cur.fetchone() or {"n": 0})["n"] or 0)
                except Exception:
                    counts[table] = -1
            try:
                cur.execute("SELECT COUNT(*) AS n FROM signals WHERE created_at >= datetime('now', '-24 hours')")
                counts["signals_24h"] = int((cur.fetchone() or {"n": 0})["n"] or 0)
            except Exception:
                counts["signals_24h"] = -1
            try:
                cur.execute("SELECT COUNT(*) AS n FROM events WHERE created_at >= datetime('now', '-1 hour')")
                counts["events_1h"] = int((cur.fetchone() or {"n": 0})["n"] or 0)
            except Exception:
                counts["events_1h"] = -1
            out["db"] = counts
    except Exception as e:
        out["db"] = {"error": str(e)}

    # Realtime stream stats
    try:
        from realtime import stats as _rt_stats
        out["stream"] = _rt_stats()
    except Exception as e:
        out["stream"] = {"error": str(e)}

    # Backtest hit-rate (for public trust panel)
    try:
        if db:
            cur = db.conn.cursor()
            cur.execute(
                """
                SELECT horizon,
                       COUNT(*) AS n,
                       SUM(CASE WHEN actual_return_pct IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                       SUM(CASE WHEN
                           (predicted_return_pct > 0 AND actual_return_pct > 0) OR
                           (predicted_return_pct < 0 AND actual_return_pct < 0)
                       THEN 1 ELSE 0 END) AS hits
                FROM predictions
                WHERE created_at >= datetime('now', '-90 days')
                GROUP BY horizon
                """
            )
            hr = {}
            for row in cur.fetchall():
                resolved = int(row["resolved"] or 0)
                hits = int(row["hits"] or 0)
                hr[row["horizon"]] = {
                    "resolved": resolved,
                    "hit_rate_pct": round((hits / resolved * 100), 2) if resolved else None,
                }
            out["hit_rate_90d"] = hr
    except Exception as e:
        out["hit_rate_90d"] = {"error": str(e)}

    # Mark unhealthy if scraper is wildly stale
    sc = out.get("scraper") or {}
    if sc.get("stale") or sc.get("last_error"):
        out["ok"] = False

    return jsonify({"success": True, "data": out})


# ---- /api/news/fast ---------------------------------------------------------

@bp.route("/api/news/fast", methods=["GET"])
@ttl_cache(seconds=30)
def news_fast():
    """Fastest path to user: combines streaming buffer + DB recent events,
    sorted by freshness, with forensic band + source tier surfaced.

    This is what the home page should call (not /api/events) when the user
    wants the latest. Doesn't re-fetch RSS — purely reads from buffer + DB.
    """
    try:
        limit = max(1, min(100, int(request.args.get("limit", "30"))))
    except ValueError:
        limit = 30
    ticker = (request.args.get("ticker") or "").upper().strip() or None
    min_alpha = request.args.get("min_alpha")
    forensic_band = request.args.get("forensic_band")
    # `?exclude_social=1` filters out reddit/telegram social posts so the
    # home page Live Wire can show curated news (RSS + Google News + filings)
    # without being drowned in social volume.
    exclude_social = (request.args.get("exclude_social") or "").lower() in ("1", "true", "yes")
    # `?kinds=earnings,policy,order_win` whitelists event types.
    kinds_filter = (request.args.get("kinds") or "").strip()
    kinds_set = {k.strip().lower() for k in kinds_filter.split(",") if k.strip()} if kinds_filter else None

    db = _get_db() if _get_db else None
    items: List[Dict] = []

    # 1) live buffer (last few minutes — fastest)
    try:
        from realtime import get_recent
        for ev in get_recent(channel=None, limit=limit * 2):
            if ev.get("channel") not in ("news", "scored"):
                continue
            if ticker and ticker not in (ev.get("tickers") or [ev.get("ticker")] if ev.get("ticker") else []):
                continue
            items.append({
                "source": "buffer",
                **ev,
            })
    except Exception:
        pass

    # 2) DB events table — widen window when filtering social out, since
    # curated news has lower volume and a 2h window often returns nothing.
    if db:
        try:
            cur = db.conn.cursor()
            # Window is generous so the feed never goes empty during low-volume
            # periods or when the scraper is paused — the UI sorts by recency,
            # so older items naturally fall to the bottom.
            window = "-14 days" if (exclude_social or kinds_set) else "-7 days"
            # NOTE: Order by `created_at` (always SQLite-native ISO) rather than
            # `published_at`, which is sometimes RFC822 ("Wed, 02 Jul GMT") for
            # Google News events. SQLite's datetime() returns NULL on RFC822,
            # which previously pushed all curated news to the bottom while
            # ISO-formatted social posts bubbled to the top.
            q = f"""
                SELECT event_id, title, summary, source, link, event_type, sentiment,
                       sentiment_confidence, magnitude, impact_score, companies,
                       published_at, created_at
                FROM events
                WHERE created_at >= datetime('now', '{window}')
                ORDER BY created_at DESC
                LIMIT ?
            """
            cur.execute(q, (limit * 4 if (exclude_social or kinds_set) else limit * 2,))
            for row in cur.fetchall():
                companies = row["companies"]
                if isinstance(companies, str):
                    try:
                        companies = json.loads(companies)
                    except Exception:
                        companies = [c.strip() for c in companies.split(",") if c.strip()]
                if ticker and ticker not in (companies or []):
                    continue
                items.append({
                    "source": "db",
                    "event_id": row["event_id"],
                    "title": row["title"],
                    "summary": row["summary"],
                    "feed": row["source"],
                    "link": row["link"],
                    "event_type": row["event_type"],
                    "sentiment": row["sentiment"],
                    "sentiment_confidence": row["sentiment_confidence"],
                    "magnitude": row["magnitude"],
                    "impact_score": row["impact_score"],
                    "tickers": companies,
                    "published_at": row["published_at"],
                    "created_at": row["created_at"],
                })
        except Exception as e:
            logger.warning(f"news_fast db read: {e}")

    # 3) annotate with source tier + freshness
    try:
        from source_tiering import classify_source, freshness_label, _parse_age_hours
        for it in items:
            feed = it.get("feed") or it.get("source_name") or it.get("source")
            link = it.get("link", "")
            meta = classify_source(str(feed or ""), link)
            it["tier"] = meta.tier
            it["tier_weight"] = meta.weight
            published = it.get("published_at") or it.get("published") or it.get("created_at")
            it["freshness"] = freshness_label(published)
            it["age_hours"] = round(_parse_age_hours(published), 2)
    except Exception as e:
        logger.warning(f"news_fast annotate: {e}")

    # 4) optional alpha / forensic filters using signals table join
    if (min_alpha or forensic_band) and db:
        try:
            event_ids = [it.get("event_id") for it in items if it.get("event_id")]
            if event_ids:
                cur = db.conn.cursor()
                placeholders = ",".join(["?"] * len(event_ids))
                cur.execute(
                    f"""SELECT s.event_id, s.alpha_score,
                               COALESCE(ms.band, 'clean') AS forensic_band
                        FROM signals s
                        LEFT JOIN manipulation_scores ms ON ms.event_id = s.event_id
                        WHERE s.event_id IN ({placeholders})""",
                    event_ids,
                )
                meta_map = {}
                for row in cur.fetchall():
                    meta_map[row["event_id"]] = {
                        "alpha_score": row["alpha_score"],
                        "forensic_band": row["forensic_band"],
                    }
                for it in items:
                    eid = it.get("event_id")
                    if eid and eid in meta_map:
                        it["alpha_score"] = meta_map[eid].get("alpha_score")
                        it["forensic_band"] = meta_map[eid].get("forensic_band")
                if min_alpha:
                    try:
                        thr = float(min_alpha)
                        items = [it for it in items if (it.get("alpha_score") or 0) >= thr]
                    except ValueError:
                        pass
                if forensic_band:
                    items = [it for it in items if it.get("forensic_band") == forensic_band]
        except Exception as e:
            logger.warning(f"news_fast filter: {e}")

    # 5) dedup by event_id, then by content hash
    try:
        from source_tiering import content_hash
        seen_eid: set = set()
        seen_hash: set = set()
        deduped: List[Dict] = []
        for it in items:
            eid = it.get("event_id")
            if eid and eid in seen_eid:
                continue
            ch = content_hash(it.get("title") or "", it.get("summary") or "")
            if ch in seen_hash:
                continue
            if eid:
                seen_eid.add(eid)
            seen_hash.add(ch)
            deduped.append(it)
        items = deduped
    except Exception:
        pass

    # 6) source/kind filters
    def _is_social(it: Dict) -> bool:
        if (it.get("event_type") or "").lower() == "social":
            return True
        feed = str(it.get("feed") or it.get("source") or "").lower()
        return feed.startswith("reddit") or feed.startswith("telegram") or "/r/" in feed
    if exclude_social:
        items = [it for it in items if not _is_social(it)]
    if kinds_set:
        items = [it for it in items if (it.get("event_type") or "").lower() in kinds_set]

    # 7) sort: prefer recent + high-tier. We multiply age in hours by a tier
    # penalty so a fresh ET/Mint headline beats a fresher reddit post.
    def _sort_key(x):
        age = x.get("age_hours") or 999.0
        tier = x.get("tier") or 4
        # Tier 1 (filings) = 1.0×, Tier 2 (press) = 1.4×, Tier 3 = 2.5×, Tier 4 = 5×
        penalty = {1: 1.0, 2: 1.4, 3: 2.5, 4: 5.0}.get(tier, 5.0)
        return age * penalty
    items.sort(key=_sort_key)

    return jsonify({
        "success": True,
        "count": len(items[:limit]),
        "data": items[:limit],
        "served_at": datetime.utcnow().isoformat() + "Z",
    })


# ---- /api/feed — UNIFIED NEWSROOM FEED -------------------------------------
# Single canonical entry-point for all timely items across the app: events,
# signals, social, geo, IPO milestones, bulk deals, F&O unusual flow, and
# forensic flags. Every page (Newsroom, global, policy, social, earnings,
# stock detail, index) reads from here so news lives in exactly one place.
#
# Shape per item:
#   {id, category, title, summary, source, source_tier, alpha_score,
#    sentiment, magnitude, impact_score, companies, ticker, link,
#    published_at, age_hours, freshness, forensic_band, event_type}
#
# Categories (derived from event_type + table-of-origin):
#   corporate, earnings, policy, ipo, geopolitical, social,
#   commodity, forensic, bulk_deal, fo
#
# A category-bucketed feed avoids the legacy fragmentation where each page
# called its own endpoint with its own filter logic and rendered with its
# own card markup.

# event_type -> category mapping for the events table
_EVENT_TYPE_CATEGORY = {
    "earnings": "earnings",
    "policy": "policy",
    "merger": "corporate",
    "buyback": "corporate",
    "dividend": "corporate",
    "split": "corporate",
    "bonus": "corporate",
    "rights": "corporate",
    "order_win": "corporate",
    "supply": "corporate",
    "insider": "corporate",
    "ipo": "ipo",
    "social": "social",
    "social_buzz": "social",
    "geo": "geopolitical",
    "geopolitical": "geopolitical",
    "macro": "geopolitical",
    "commodity": "commodity",
}

# Reverse map: category -> set of event_types to query against the events table
_CATEGORY_EVENT_TYPES = {
    "corporate": ("merger", "buyback", "dividend", "split", "bonus", "rights",
                  "order_win", "supply", "insider"),
    "earnings": ("earnings",),
    "policy": ("policy",),
    "ipo": ("ipo",),
    "geopolitical": ("geo", "geopolitical", "macro"),
    "social": ("social", "social_buzz"),
    "commodity": ("commodity",),
}

# Heuristic: keywords that flag a non-typed event as geopolitical so the
# geopolitical category isn't empty when the scraper hasn't tagged event_type.
_GEOPOLITICAL_KEYWORDS = (
    "fed ", "ecb", "boj", "bank of japan", "treasury yield", "nasdaq", "s&p",
    "shanghai", "evergrande", "opec", "geopolit", "tariff", "sanction",
    "russia", "ukraine", "china", "us-china", "trade war",
)
_COMMODITY_KEYWORDS = (
    "crude", "brent", "wti", "gold", "silver", "copper", "natural gas",
    "lng", "wheat", "corn", "sugar", "cotton", "palm oil", "coffee",
    "aluminium", "aluminum",
)


def _classify_category(event_type: str, title: str = "", source: str = "") -> str:
    """Derive a feed category for an event.
    Falls back to keyword sniffing for events without a strong event_type tag."""
    et = (event_type or "").lower()
    if et in _EVENT_TYPE_CATEGORY:
        return _EVENT_TYPE_CATEGORY[et]
    blob = (title + " " + source).lower()
    if any(k in blob for k in _GEOPOLITICAL_KEYWORDS):
        return "geopolitical"
    if any(k in blob for k in _COMMODITY_KEYWORDS):
        return "commodity"
    src = (source or "").lower()
    if src.startswith("reddit") or src.startswith("telegram") or "/r/" in src:
        return "social"
    return "corporate"  # safe default — most untyped news is corporate-action chatter


def _normalize_companies(raw):
    if isinstance(raw, list):
        return [str(c).strip() for c in raw if str(c).strip()]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(c).strip() for c in parsed if str(c).strip()]
        except Exception:
            pass
        return [c.strip() for c in raw.split(",") if c.strip()]
    return []


def _annotate_tier_freshness(items: List[Dict]) -> None:
    """In-place: attach source_tier + freshness + age_hours to every item."""
    try:
        from source_tiering import classify_source, freshness_label, _parse_age_hours
        for it in items:
            feed = it.get("source") or it.get("feed") or it.get("source_name")
            link = it.get("link") or ""
            try:
                meta = classify_source(str(feed or ""), link)
                it["source_tier"] = meta.tier
            except Exception:
                it["source_tier"] = it.get("source_tier") or 4
            published = it.get("published_at") or it.get("created_at")
            try:
                it["freshness"] = freshness_label(published)
                it["age_hours"] = round(_parse_age_hours(published), 2)
            except Exception:
                it["freshness"] = it.get("freshness") or ""
                it["age_hours"] = it.get("age_hours")
    except Exception:
        # Tiering optional — UI tolerates missing fields
        pass


def _attach_alpha(db, items: List[Dict]) -> None:
    """Join the signals table to attach alpha_score + forensic_band per event."""
    if not db or not items:
        return
    event_ids = [it["event_id"] for it in items if it.get("event_id")]
    if not event_ids:
        return
    try:
        cur = db.conn.cursor()
        placeholders = ",".join(["?"] * len(event_ids))
        cur.execute(
            f"""SELECT s.event_id, MAX(s.alpha_score) AS alpha_score,
                       COALESCE(MAX(ms.band), 'clean') AS forensic_band
                FROM signals s
                LEFT JOIN manipulation_scores ms ON ms.event_id = s.event_id
                WHERE s.event_id IN ({placeholders})
                GROUP BY s.event_id""",
            event_ids,
        )
        meta_map = {row["event_id"]: dict(row) for row in cur.fetchall()}
        for it in items:
            m = meta_map.get(it.get("event_id"))
            if m:
                it["alpha_score"] = m.get("alpha_score")
                it["forensic_band"] = m.get("forensic_band")
    except Exception as e:
        logger.debug(f"feed alpha-join failed: {e}")


def _read_events(db, hours: int, ticker: Optional[str], categories: Optional[set],
                 limit: int) -> List[Dict]:
    """Read from the canonical `events` table and project into the unified shape."""
    if not db:
        return []
    rows: List[Dict] = []
    try:
        cur = db.conn.cursor()
        # Build the SQL filter for the requested category. For
        # keyword-classified categories (commodity, geopolitical) the SQL
        # type-filter alone returns nothing — almost no events carry the
        # literal event_type='commodity'. So we union the literal type with
        # LIKE clauses over title for any keyword the classifier checks.
        type_filter = ""
        params: List = []
        if categories and len(categories) == 1:
            cat = next(iter(categories))
            types = _CATEGORY_EVENT_TYPES.get(cat)
            keyword_bag = (
                _COMMODITY_KEYWORDS if cat == "commodity" else
                _GEOPOLITICAL_KEYWORDS if cat == "geopolitical" else
                ()
            )
            type_clauses: List[str] = []
            if types:
                placeholders = ",".join(["?"] * len(types))
                type_clauses.append(f"event_type IN ({placeholders})")
                params.extend(types)
            if keyword_bag:
                like_parts = [f"LOWER(title) LIKE ?" for _ in keyword_bag]
                type_clauses.append("(" + " OR ".join(like_parts) + ")")
                params.extend(f"%{kw.lower()}%" for kw in keyword_bag)
            if type_clauses:
                type_filter = " AND (" + " OR ".join(type_clauses) + ")"
        sql = (
            "SELECT event_id, title, summary, source, link, event_type, sentiment, "
            "       sentiment_confidence, magnitude, impact_score, companies, "
            "       published_at, created_at "
            "FROM events "
            f"WHERE created_at >= datetime('now', '-{int(hours)} hours')"
            f"{type_filter} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(int(limit) * 4)
        cur.execute(sql, tuple(params))
        for r in cur.fetchall():
            companies = _normalize_companies(r["companies"])
            if ticker and ticker not in companies:
                continue
            cat = _classify_category(r["event_type"], r["title"] or "", r["source"] or "")
            # Final Python-side filter: even if the keyword-LIKE matched, only
            # keep rows whose final classification agrees with the request.
            if categories and cat not in categories:
                continue
            rows.append({
                "id": r["event_id"],
                "event_id": r["event_id"],
                "category": cat,
                "title": r["title"],
                "summary": r["summary"],
                "source": r["source"],
                "link": r["link"],
                "event_type": r["event_type"],
                "sentiment": r["sentiment"],
                "sentiment_confidence": r["sentiment_confidence"],
                "magnitude": r["magnitude"],
                "impact_score": r["impact_score"],
                "companies": companies,
                "published_at": r["published_at"],
                "created_at": r["created_at"],
                "origin": "events",
            })
    except Exception as e:
        logger.warning(f"feed events read: {e}")
    return rows


def _read_bulk_deals(db, hours: int, ticker: Optional[str], limit: int) -> List[Dict]:
    """Project bulk_deals rows into the unified shape so they appear in the
    bulk_deal category alongside news."""
    if not db:
        return []
    out: List[Dict] = []
    try:
        cur = db.conn.cursor()
        sql = ("SELECT deal_date, ticker, deal_value_cr, deal_price, "
               "       buyer_seller, exchange "
               "FROM bulk_deals "
               f"WHERE deal_date >= datetime('now', '-{int(hours)} hours')")
        params: List = []
        if ticker:
            sql += " AND ticker = ?"
            params.append(ticker)
        sql += " ORDER BY deal_date DESC LIMIT ?"
        params.append(int(limit))
        cur.execute(sql, tuple(params))
        for r in cur.fetchall():
            row = dict(r) if hasattr(r, "keys") else {}
            tk = row.get("ticker") or ""
            party = row.get("buyer_seller") or ""
            value = row.get("deal_value_cr") or 0
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = 0
            title = f"{tk}: ₹{value:.1f}Cr block deal" if value else f"{tk}: bulk deal"
            summary = f"{party} on {row.get('exchange') or 'NSE/BSE'}"
            out.append({
                "id": f"bd:{tk}:{row.get('deal_date')}",
                "event_id": None,
                "category": "bulk_deal",
                "title": title,
                "summary": summary,
                "source": row.get("exchange") or "NSE",
                "link": "",
                "event_type": "bulk_deal",
                "sentiment": "bullish" if "buy" in party.lower() else
                             ("bearish" if "sell" in party.lower() else "neutral"),
                "magnitude": min(10, value / 5) if value else 5,
                "impact_score": min(100, value * 2) if value else 40,
                "companies": [tk] if tk else [],
                "published_at": str(row.get("deal_date")),
                "created_at": str(row.get("deal_date")),
                "origin": "bulk_deals",
            })
    except Exception as e:
        logger.debug(f"feed bulk_deals read: {e}")
    return out


def _read_fo_unusual(db, hours: int, ticker: Optional[str], limit: int) -> List[Dict]:
    """Project fo_unusual rows (option-chain anomalies) into unified shape."""
    if not db:
        return []
    out: List[Dict] = []
    try:
        cur = db.conn.cursor()
        sql = ("SELECT ticker, signal_type, magnitude, detail, created_at "
               "FROM fo_unusual "
               f"WHERE created_at >= datetime('now', '-{int(hours)} hours')")
        params: List = []
        if ticker:
            sql += " AND ticker = ?"
            params.append(ticker)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        cur.execute(sql, tuple(params))
        for r in cur.fetchall():
            row = dict(r) if hasattr(r, "keys") else {}
            tk = row.get("ticker") or ""
            sig = row.get("signal_type") or "unusual_flow"
            mag = row.get("magnitude") or 5
            try:
                mag = float(mag)
            except (TypeError, ValueError):
                mag = 5
            out.append({
                "id": f"fo:{tk}:{row.get('created_at')}",
                "event_id": None,
                "category": "fo",
                "title": f"{tk}: unusual {sig.replace('_', ' ')}",
                "summary": row.get("detail") or "Option-chain anomaly detected",
                "source": "F&O monitor",
                "link": "",
                "event_type": "fo_unusual",
                "sentiment": "neutral",
                "magnitude": mag,
                "impact_score": min(100, mag * 10),
                "companies": [tk] if tk else [],
                "published_at": str(row.get("created_at")),
                "created_at": str(row.get("created_at")),
                "origin": "fo_unusual",
            })
    except Exception as e:
        logger.debug(f"feed fo_unusual read: {e}")
    return out


def _read_ipo_events(db, limit: int) -> List[Dict]:
    """Use the existing IPO event computation so listed/closing/opened/milestone
    states flow into the unified feed. We dispatch directly to the helper used
    by /api/ipos/events to avoid duplicating the state-machine logic."""
    if not db:
        return []
    try:
        # api_ipos_events reads request args; call it via the underlying logic.
        # Cheap path: just call the route function in a synthetic request context.
        from flask import current_app
        with current_app.test_request_context(f"/api/ipos/events?limit={int(limit)}"):
            resp = api_ipos_events()
        if hasattr(resp, "get_json"):
            payload = resp.get_json() or {}
        else:
            return []
        events = payload.get("data") or []
    except Exception as e:
        logger.debug(f"feed ipo passthrough failed: {e}")
        return []

    out: List[Dict] = []
    for ev in events:
        out.append({
            "id": f"ipo:{ev.get('symbol')}:{ev.get('kind')}:{ev.get('ts')}",
            "event_id": None,
            "category": "ipo",
            "title": ev.get("title") or "",
            "summary": ev.get("detail") or "",
            "source": "IPO pipeline",
            "link": "",
            "event_type": "ipo",
            "sentiment": ("bullish" if ev.get("tag") == "pop" else
                          "bearish" if ev.get("tag") == "flop" else "neutral"),
            "magnitude": 7 if ev.get("tag") in ("pop", "milestone") else 5,
            "impact_score": int(float(ev.get("alpha") or 0)),
            "alpha_score": float(ev.get("alpha") or 0) or None,
            "companies": [ev.get("ticker") or ev.get("symbol")] if (ev.get("ticker") or ev.get("symbol")) else [],
            "published_at": ev.get("ts"),
            "created_at": ev.get("ts"),
            "origin": "ipos",
            "ipo_tag": ev.get("tag"),
        })
    return out


@bp.route("/api/feed", methods=["GET"])
@ttl_cache(seconds=30)
def feed():
    """Unified news / alerts / events feed — the canonical read path.

    Query params:
        category:      corporate|earnings|policy|ipo|geopolitical|social|
                       commodity|forensic|bulk_deal|fo  (omit for "all")
        ticker:        filter by ticker (uppercase)
        hours:         lookback window 1-720 (default 24)
        min_alpha:     minimum alpha_score (after signals join)
        source_tier:   minimum tier — 1 (T1 verified) is strictest
        exclude_social: 1 = drop social posts
        limit:         1-500 (default 100)

    Every page that shows a feed of timely items should call this. Specialized
    pages just pin the category param.
    """
    try:
        limit = max(1, min(500, int(request.args.get("limit", "100"))))
    except ValueError:
        limit = 100
    try:
        hours = max(1, min(720, int(request.args.get("hours", "24"))))
    except ValueError:
        hours = 24
    ticker = (request.args.get("ticker") or "").upper().strip() or None
    category = (request.args.get("category") or "").lower().strip() or None
    exclude_social = (request.args.get("exclude_social") or "").lower() in ("1", "true", "yes")
    min_alpha_raw = request.args.get("min_alpha")
    try:
        min_alpha = float(min_alpha_raw) if min_alpha_raw not in (None, "") else None
    except ValueError:
        min_alpha = None
    try:
        max_tier = int(request.args.get("source_tier")) if request.args.get("source_tier") else None
    except ValueError:
        max_tier = None

    db = _get_db() if _get_db else None
    items: List[Dict] = []

    # Specialized-table categories pull from their own tables; everything else
    # falls back to the canonical events table with a category-derived filter.
    if category == "bulk_deal":
        items = _read_bulk_deals(db, hours, ticker, limit * 2)
    elif category == "fo":
        items = _read_fo_unusual(db, hours, ticker, limit * 2)
    elif category == "ipo":
        # Mix IPO state events with any 'ipo' rows in the events table
        items = _read_ipo_events(db, limit) + _read_events(db, hours, ticker, {"ipo"}, limit)
    else:
        cats = {category} if category else None
        items = _read_events(db, hours, ticker, cats, limit * 3)

    _annotate_tier_freshness(items)
    _attach_alpha(db, items)

    # Forensic category: keep only items whose forensic_band is non-clean
    if category == "forensic":
        items = [it for it in items if (it.get("forensic_band") or "clean") != "clean"]

    # Filters
    if exclude_social:
        items = [it for it in items if it.get("category") != "social"]
    if min_alpha is not None:
        items = [it for it in items if (it.get("alpha_score") or 0) >= min_alpha]
    if max_tier is not None:
        items = [it for it in items if (it.get("source_tier") or 99) <= max_tier]

    # Dedupe by event_id, then content hash
    try:
        from source_tiering import content_hash
        seen_eid: set = set()
        seen_hash: set = set()
        deduped: List[Dict] = []
        for it in items:
            eid = it.get("event_id") or it.get("id")
            if eid and eid in seen_eid:
                continue
            ch = content_hash(it.get("title") or "", it.get("summary") or "")
            if ch in seen_hash:
                continue
            if eid:
                seen_eid.add(eid)
            seen_hash.add(ch)
            deduped.append(it)
        items = deduped
    except Exception:
        pass

    # Sort: tier-weighted recency (same scheme as /api/news/fast)
    def _key(x):
        age = x.get("age_hours") or 999.0
        tier = x.get("source_tier") or 4
        penalty = {1: 1.0, 2: 1.4, 3: 2.5, 4: 5.0}.get(tier, 5.0)
        return age * penalty
    items.sort(key=_key)

    return jsonify({
        "success": True,
        "category": category or "all",
        "count": len(items[:limit]),
        "data": items[:limit],
        "served_at": datetime.utcnow().isoformat() + "Z",
    })


# ---- smart-alerts CRUD -----------------------------------------------------

def _user_id() -> str:
    return getattr(g, "user_id", None) or "legacy"


@bp.route("/api/alerts/smart", methods=["GET"])
def smart_alerts_list():
    db = _get_db()
    try:
        from smart_alerts import list_alerts
        active_only = request.args.get("active", "1") in ("1", "true", "True")
        return jsonify({"success": True, "data": list_alerts(db, _user_id(), active_only=active_only)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart", methods=["POST"])
def smart_alerts_create():
    db = _get_db()
    try:
        from smart_alerts import create_alert, SUPPORTED_KINDS
        data = request.get_json(force=True) or {}
        kind = data.get("kind")
        if kind not in SUPPORTED_KINDS:
            return jsonify({"success": False, "error": f"kind must be one of {SUPPORTED_KINDS}"}), 400
        ticker = (data.get("ticker") or "").upper() or None
        params = data.get("params") or {}
        cooldown = int(data.get("cooldown_minutes") or 60)
        new_id = create_alert(db, _user_id(), kind, ticker, params, cooldown_minutes=cooldown)
        try:
            from api import audit_log as _audit
            _audit('alert_create', f'alert:{new_id}',
                   {'kind': kind, 'ticker': ticker, 'params': params, 'cooldown': cooldown})
        except Exception:
            pass
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart/preview", methods=["POST"])
def smart_alerts_preview():
    """Replay a candidate alert rule against historical signals/events and
    return what would have fired in the last N days.

    Lets users dry-run a rule before saving it (and tunes parameter defaults).
    Body: { "kind": ..., "ticker": optional, "params": {...}, "days": 30 }
    Returns: { matches: [{ts, ticker, payload}, ...], count, days }
    """
    db = _get_db()
    try:
        from smart_alerts import SUPPORTED_KINDS  # noqa: F401  (validate kind)
        body = request.get_json(force=True) or {}
        kind = body.get("kind")
        if kind not in SUPPORTED_KINDS:
            return jsonify({"success": False, "error": f"kind must be one of {SUPPORTED_KINDS}"}), 400
        ticker = (body.get("ticker") or "").upper() or None
        params = body.get("params") or {}
        try:
            days = max(1, min(90, int(body.get("days", 30))))
        except (TypeError, ValueError):
            days = 30

        cur = db.conn.cursor()
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        matches: List[Dict] = []

        # Each kind has a different historical replay strategy. Keep this
        # narrow to avoid expensive table scans — preview is a UX nicety,
        # not a backtester.
        if kind == "signal_alpha":
            min_alpha = float(params.get("min_alpha", 70))
            sentiment = (params.get("sentiment") or "").strip().lower() or None
            sql = (f"SELECT created_at, ticker, alpha_score, sentiment, headline "
                   f"FROM signals WHERE alpha_score >= {p} "
                   f"AND created_at >= datetime('now', '-{days} days')")
            args = [min_alpha]
            if ticker:
                sql += f" AND ticker = {p}"; args.append(ticker)
            if sentiment:
                sql += f" AND sentiment = {p}"; args.append(sentiment)
            sql += " ORDER BY created_at DESC LIMIT 100"
            cur.execute(sql, tuple(args))
            for r in cur.fetchall():
                row = dict(r) if hasattr(r, "keys") else {
                    "created_at": r[0], "ticker": r[1], "alpha_score": r[2],
                    "sentiment": r[3], "headline": r[4],
                }
                matches.append({"ts": str(row.get("created_at")),
                                "ticker": row.get("ticker"),
                                "payload": {"alpha": row.get("alpha_score"),
                                            "sentiment": row.get("sentiment"),
                                            "headline": row.get("headline")}})
        elif kind == "bulk_deal":
            min_cr = float(params.get("min_value_cr", 5))
            try:
                sql = (f"SELECT deal_date, ticker, deal_value_cr, deal_price, buyer_seller "
                       f"FROM bulk_deals WHERE deal_value_cr >= {p} "
                       f"AND deal_date >= datetime('now', '-{days} days')")
                args = [min_cr]
                if ticker:
                    sql += f" AND ticker = {p}"; args.append(ticker)
                sql += " ORDER BY deal_date DESC LIMIT 100"
                cur.execute(sql, tuple(args))
                for r in cur.fetchall():
                    row = dict(r) if hasattr(r, "keys") else {
                        "deal_date": r[0], "ticker": r[1], "deal_value_cr": r[2],
                        "deal_price": r[3], "buyer_seller": r[4],
                    }
                    matches.append({"ts": str(row.get("deal_date")),
                                    "ticker": row.get("ticker"),
                                    "payload": {"value_cr": row.get("deal_value_cr"),
                                                "price": row.get("deal_price"),
                                                "party": row.get("buyer_seller")}})
            except Exception:
                pass  # bulk_deals table may not exist in dev DBs
        elif kind == "promoter_pledge":
            min_delta = float(params.get("min_delta_pct", 5))
            try:
                sql = (f"SELECT event_date, ticker, pct_before, pct_after, person_name "
                       f"FROM promoter_events WHERE event_type = 'pledge' "
                       f"AND ABS(COALESCE(pct_after,0) - COALESCE(pct_before,0)) >= {p} "
                       f"AND event_date >= datetime('now', '-{days} days')")
                args = [min_delta]
                if ticker:
                    sql += f" AND ticker = {p}"; args.append(ticker)
                sql += " ORDER BY event_date DESC LIMIT 100"
                cur.execute(sql, tuple(args))
                for r in cur.fetchall():
                    row = dict(r) if hasattr(r, "keys") else {
                        "event_date": r[0], "ticker": r[1], "pct_before": r[2],
                        "pct_after": r[3], "person_name": r[4],
                    }
                    matches.append({"ts": str(row.get("event_date")),
                                    "ticker": row.get("ticker"),
                                    "payload": {"before": row.get("pct_before"),
                                                "after": row.get("pct_after"),
                                                "person": row.get("person_name")}})
            except Exception:
                pass
        elif kind == "forensic_flip":
            to_band = params.get("to_band", "likely_manipulated")
            try:
                sql = (f"SELECT as_of, ticker, band, score FROM forensic_flags "
                       f"WHERE band = {p} AND as_of >= datetime('now', '-{days} days')")
                args = [to_band]
                if ticker:
                    sql += f" AND ticker = {p}"; args.append(ticker)
                sql += " ORDER BY as_of DESC LIMIT 100"
                cur.execute(sql, tuple(args))
                for r in cur.fetchall():
                    row = dict(r) if hasattr(r, "keys") else {
                        "as_of": r[0], "ticker": r[1], "band": r[2], "score": r[3],
                    }
                    matches.append({"ts": str(row.get("as_of")),
                                    "ticker": row.get("ticker"),
                                    "payload": {"band": row.get("band"), "score": row.get("score")}})
            except Exception:
                pass
        else:
            # volume_spike / news_velocity require live time-series the preview
            # doesn't reconstruct. Surface this honestly rather than fabricate.
            return jsonify({"success": True, "data": {
                "matches": [], "count": 0, "days": days,
                "note": f"Preview not available for '{kind}' (requires live time-series replay).",
            }})

        return jsonify({"success": True, "data": {
            "matches": matches[:50], "count": len(matches), "days": days,
        }})
    except Exception as e:
        logger.warning(f"alerts preview failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart/<int:alert_id>", methods=["DELETE"])
def smart_alerts_delete(alert_id: int):
    db = _get_db()
    try:
        from smart_alerts import delete_alert
        ok = delete_alert(db, _user_id(), alert_id)
        if ok:
            try:
                from api import audit_log as _audit
                _audit('alert_delete', f'alert:{alert_id}', None)
            except Exception:
                pass
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart/<int:alert_id>", methods=["PATCH"])
def smart_alerts_update(alert_id: int):
    db = _get_db()
    try:
        from smart_alerts import update_alert
        data = request.get_json(force=True) or {}
        ok = update_alert(db, _user_id(), alert_id, **data)
        if ok:
            try:
                from api import audit_log as _audit
                _audit('alert_update', f'alert:{alert_id}', data)
            except Exception:
                pass
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/alerts/smart/kinds", methods=["GET"])
def smart_alerts_kinds():
    """Surfaces alert kinds + their parameter shapes for the UI builder."""
    return jsonify({"success": True, "data": [
        {"kind": "volume_spike", "label": "Volume spike", "needs_ticker": True,
         "params": [{"name": "min_z", "type": "number", "default": 2.0,
                     "help": "Z-score vs 20-day average (2.0 = 2x normal)"}]},
        {"kind": "news_velocity", "label": "News velocity", "needs_ticker": False,
         "params": [{"name": "min_per_hour", "type": "number", "default": 3,
                     "help": "Articles per hour threshold"},
                    {"name": "window_hours", "type": "number", "default": 2}]},
        {"kind": "forensic_flip", "label": "Forensic band flip", "needs_ticker": False,
         "params": [{"name": "to_band", "type": "select", "default": "likely_manipulated",
                     "options": ["likely_manipulated", "unverified"]}]},
        {"kind": "signal_alpha", "label": "Alpha threshold cross", "needs_ticker": False,
         "params": [{"name": "min_alpha", "type": "number", "default": 70},
                    {"name": "sentiment", "type": "select", "default": "",
                     "options": ["", "bullish", "bearish"]}]},
        {"kind": "promoter_pledge", "label": "Promoter pledge change", "needs_ticker": True,
         "params": [{"name": "min_delta_pct", "type": "number", "default": 5}]},
        {"kind": "bulk_deal", "label": "Bulk deal", "needs_ticker": True,
         "params": [{"name": "min_value_cr", "type": "number", "default": 5}]},
    ]})


# ---- /api/global/markets and /api/commodities/prices fallback shims --------

@bp.route("/api/global/markets", methods=["GET"])
@ttl_cache(seconds=60)
def global_markets():
    """Live world indices via yfinance. Falls back to last known DB row if
    network call fails. Always returns honest 'as_of' so the UI can flag stale.
    """
    indices = [
        ("^GSPC", "S&P 500", "USA"),
        ("^DJI", "Dow Jones", "USA"),
        ("^IXIC", "NASDAQ", "USA"),
        ("^FTSE", "FTSE 100", "UK"),
        ("^N225", "Nikkei 225", "JPN"),
        ("^HSI", "Hang Seng", "HKG"),
        ("000001.SS", "Shanghai Comp", "CHN"),
        ("^GDAXI", "DAX", "DEU"),
        ("^NSEI", "Nifty 50", "IND"),
        ("^BSESN", "Sensex", "IND"),
    ]
    fx_pairs = [
        ("INR=X", "USD/INR"),
        ("EURINR=X", "EUR/INR"),
        ("GBPINR=X", "GBP/INR"),
        ("JPYINR=X", "JPY/INR"),
    ]
    commodities = [
        ("CL=F", "Crude Oil", "WTI"),
        ("BZ=F", "Brent", "OIL"),
        ("GC=F", "Gold", "USD/oz"),
        ("SI=F", "Silver", "USD/oz"),
        ("NG=F", "Natural Gas", "MMBtu"),
    ]

    out: Dict = {"indices": [], "fx": [], "commodities": [], "as_of": None, "stale": False}
    try:
        import yfinance as yf
        all_syms = [s for s, _, _ in indices] + [s for s, _ in fx_pairs] + [s for s, _, _ in commodities]
        data = yf.download(all_syms, period="2d", interval="1d", group_by="ticker",
                           progress=False, threads=True)

        def _last_two(sym: str):
            try:
                if len(all_syms) == 1:
                    closes = data["Close"].dropna()
                else:
                    closes = data[sym]["Close"].dropna()
                if len(closes) < 1:
                    return None, None
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
                return last, prev
            except Exception:
                return None, None

        for sym, name, region in indices:
            last, prev = _last_two(sym)
            if last is None:
                continue
            out["indices"].append({
                "symbol": sym, "name": name, "region": region,
                "price": round(last, 2),
                "change_pct": round((last - prev) / prev * 100, 2) if prev else 0,
            })
        for sym, name in fx_pairs:
            last, prev = _last_two(sym)
            if last is None:
                continue
            out["fx"].append({
                "symbol": sym, "name": name,
                "price": round(last, 4),
                "change_pct": round((last - prev) / prev * 100, 2) if prev else 0,
            })
        for sym, name, unit in commodities:
            last, prev = _last_two(sym)
            if last is None:
                continue
            out["commodities"].append({
                "symbol": sym, "name": name, "unit": unit,
                "price": round(last, 2),
                "change_pct": round((last - prev) / prev * 100, 2) if prev else 0,
            })
        out["as_of"] = datetime.utcnow().isoformat() + "Z"
        out["source"] = "yfinance"
    except Exception as e:
        out["error"] = str(e)
        out["stale"] = True

    if not out["indices"] and not out["fx"] and not out["commodities"]:
        return jsonify({"success": False, "error": "no live data; check network", "data": out}), 503

    return jsonify({"success": True, "data": out})


# Comprehensive commodity catalogue — keyword bag drives event matching.
# Each row: id (matches frontend), display name, category, yfinance symbol,
# unit, and search keywords used to count events / score alpha.
_COMMODITY_CATALOG = [
    # Energy
    ("CRUDEOIL",    "Crude Oil (WTI)",     "Energy", "CL=F", "USD/bbl",
     ("crude", "wti", "oil price", "barrel", "petroleum")),
    ("BRENT",       "Brent Crude",         "Energy", "BZ=F", "USD/bbl",
     ("brent", "north sea")),
    ("NATURALGAS",  "Natural Gas",         "Energy", "NG=F", "USD/MMBtu",
     ("natural gas", "henry hub", "lng")),
    ("HEATINGOIL",  "Heating Oil",         "Energy", "HO=F", "USD/gal",
     ("heating oil", "diesel")),
    ("RBOB",        "RBOB Gasoline",       "Energy", "RB=F", "USD/gal",
     ("gasoline", "rbob", "petrol")),
    # Precious metals
    ("GOLD",        "Gold",                "Metals", "GC=F", "USD/oz",
     ("gold price", "gold rallies", "comex gold", "bullion")),
    ("SILVER",      "Silver",              "Metals", "SI=F", "USD/oz",
     ("silver price", "comex silver")),
    ("PLATINUM",    "Platinum",            "Metals", "PL=F", "USD/oz",
     ("platinum",)),
    ("PALLADIUM",   "Palladium",           "Metals", "PA=F", "USD/oz",
     ("palladium",)),
    # Base / industrial metals
    ("COPPER",      "Copper",              "Metals", "HG=F", "USD/lb",
     ("copper", "lme copper")),
    ("ALUMINIUM",   "Aluminium",           "Metals", "ALI=F", "USD/lb",
     ("aluminium", "aluminum", "lme alumin")),
    # Agri — grains
    ("WHEAT",       "Wheat",               "Agri", "ZW=F", "USD/bu",
     ("wheat",)),
    ("CORN",        "Corn",                "Agri", "ZC=F", "USD/bu",
     ("corn", "maize")),
    ("SOYBEAN",     "Soybean",             "Agri", "ZS=F", "USD/bu",
     ("soybean", "soya")),
    ("RICE",        "Rough Rice",          "Agri", "ZR=F", "USD/cwt",
     ("rice",)),
    ("OATS",        "Oats",                "Agri", "ZO=F", "USD/bu",
     ("oats",)),
    # Agri — softs
    ("COTTON",      "Cotton",              "Softs", "CT=F", "USD/lb",
     ("cotton",)),
    ("SUGAR",       "Sugar",               "Softs", "SB=F", "USD/lb",
     ("sugar",)),
    ("COFFEE",      "Coffee",              "Softs", "KC=F", "USD/lb",
     ("coffee",)),
    ("COCOA",       "Cocoa",               "Softs", "CC=F", "USD/MT",
     ("cocoa",)),
    ("OJ",          "Orange Juice",        "Softs", "OJ=F", "USD/lb",
     ("orange juice",)),
    ("LUMBER",      "Lumber",              "Softs", "LBR=F", "USD/1000bf",
     ("lumber", "timber")),
    # Livestock
    ("CATTLE",      "Live Cattle",         "Livestock", "LE=F", "USD/lb",
     ("cattle",)),
    ("HOGS",        "Lean Hogs",           "Livestock", "HE=F", "USD/lb",
     ("lean hogs", "pork")),
    # Asia-specific
    ("PALMOIL",     "Palm Oil",            "Agri", "FCPO=F", "MYR/MT",
     ("palm oil",)),
]


def _commodity_alpha(db, keywords, hours: int = 168):
    """Aggregate event-derived signals into a commodity alpha score.
    Looks at events whose title mentions any keyword in the last `hours`
    window, then scores: count weighted by impact_score, with sentiment
    penalty for bearish skew.
    """
    if not db:
        return {"alpha": None, "events": 0, "bull": 0, "bear": 0}
    bull = bear = total = 0
    impact_sum = 0.0
    try:
        cur = db.conn.cursor()
        # Cheap LIKE OR over keywords; cap LIMIT so a noisy keyword doesn't
        # tank query time.
        like_clauses = " OR ".join(["LOWER(title) LIKE ?"] * len(keywords))
        params = [f"%{k.lower()}%" for k in keywords]
        cur.execute(
            f"""SELECT title, sentiment, magnitude, impact_score
                FROM events
                WHERE created_at >= datetime('now', ?)
                  AND ({like_clauses})
                ORDER BY created_at DESC LIMIT 80""",
            (f"-{int(hours)} hours", *params),
        )
        for r in cur.fetchall():
            row = dict(r)
            sent = (row.get("sentiment") or "").lower()
            if sent == "bullish":
                bull += 1
            elif sent == "bearish":
                bear += 1
            try:
                impact_sum += float(row.get("impact_score") or 0)
            except (TypeError, ValueError):
                pass
            total += 1
    except Exception as e:
        logger.debug(f"_commodity_alpha failed: {e}")
        return {"alpha": None, "events": 0, "bull": 0, "bear": 0}

    if total == 0:
        return {"alpha": None, "events": 0, "bull": 0, "bear": 0}
    avg_impact = impact_sum / total                         # 0..100 baseline
    # Sentiment skew: +1 fully bullish, -1 fully bearish
    skew = (bull - bear) / max(total, 1)
    # Volume confirmation — more events = more confidence, capped at +20
    vol_bonus = min(20, total * 1.5)
    alpha = max(0, min(100, round(avg_impact + skew * 12 + vol_bonus, 1)))
    return {"alpha": alpha, "events": total, "bull": bull, "bear": bear}


@bp.route("/api/commodities/all", methods=["GET"])
@ttl_cache(seconds=120)
def commodities_all():
    """Comprehensive commodities feed: live yfinance prices + alpha score
    derived from event mentions in the events table. Replaces both the
    hardcoded MOCK_COMMODITY_PRICES catalogue and the per-commodity event
    arrays previously baked into commodities.html.

    Query params:
        category: filter to Energy / Metals / Agri / Softs / Livestock
        hours:    event lookback window for alpha (default 168 = 7d)
    """
    try:
        hours = max(24, min(720, int(request.args.get("hours", "168"))))
    except (TypeError, ValueError):
        hours = 168
    cat_filter = (request.args.get("category") or "").lower().strip() or None

    items = list(_COMMODITY_CATALOG)
    if cat_filter:
        items = [c for c in items if c[2].lower() == cat_filter]

    # Bulk-fetch yfinance prices for everything in one shot.
    syms = [c[3] for c in items]
    price_map: Dict[str, Dict] = {}
    try:
        import yfinance as yf
        data = yf.download(syms, period="3d", interval="1d",
                           group_by="ticker", progress=False, threads=True)
        for sym in syms:
            try:
                closes = data[sym]["Close"].dropna() if len(syms) > 1 else data["Close"].dropna()
                if len(closes) < 1:
                    continue
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
                price_map[sym] = {
                    "price": round(last, 2),
                    "change": round(last - prev, 4),
                    "change_pct": round((last - prev) / prev * 100, 2) if prev else 0.0,
                }
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"commodities_all yfinance failed: {e}")

    # Pull alpha + event counts per commodity from the events table.
    db = _get_db() if _get_db else None
    rows = []
    for cid, name, category, sym, unit, keywords in items:
        price = price_map.get(sym, {})
        alpha = _commodity_alpha(db, keywords, hours=hours)
        net = alpha["bull"] - alpha["bear"]
        sentiment = ("bullish" if net > 0 and alpha["bull"] >= 2 else
                     "bearish" if net < 0 and alpha["bear"] >= 2 else
                     "neutral")
        rows.append({
            "id": cid,
            "name": name,
            "category": category,
            "symbol": sym,
            "unit": unit,
            "price": price.get("price"),
            "change_pct": price.get("change_pct"),
            "change": price.get("change"),
            "alpha_score": alpha["alpha"],
            "events_in_window": alpha["events"],
            "bull_count": alpha["bull"],
            "bear_count": alpha["bear"],
            "sentiment": sentiment,
            "keywords": list(keywords),
        })

    # Sort: alpha desc (None last), then by absolute price-change
    rows.sort(key=lambda r: (
        -(r.get("alpha_score") or -1),
        -abs(r.get("change_pct") or 0),
    ))

    return jsonify({
        "success": True,
        "data": rows,
        "count": len(rows),
        "lookback_hours": hours,
        "as_of": datetime.utcnow().isoformat() + "Z",
    })


@bp.route("/api/commodities/prices", methods=["GET"])
@ttl_cache(seconds=60)
def commodities_prices():
    """Live commodity prices keyed by the IDs that commodities.html expects.

    Replaces the hardcoded MOCK_COMMODITY_PRICES dict in the frontend.
    Returns {id: {price, change_pct, currency, as_of}}. Front-end keeps its
    catalog (descriptions, sectors affected) and only reads price/change here.
    """
    # Yahoo Finance symbol map for the commodities the UI knows about
    sym_map = {
        "crude_brent": ("BZ=F", "USD/bbl"),
        "crude_wti": ("CL=F", "USD/bbl"),
        "natural_gas": ("NG=F", "USD/MMBtu"),
        "gold": ("GC=F", "USD/oz"),
        "silver": ("SI=F", "USD/oz"),
        "copper": ("HG=F", "USD/lb"),
        "aluminum": ("ALI=F", "USD/lb"),
        "wheat": ("ZW=F", "USD/bu"),
        "corn": ("ZC=F", "USD/bu"),
        "soybean": ("ZS=F", "USD/bu"),
        "cotton": ("CT=F", "USD/lb"),
        "sugar": ("SB=F", "USD/lb"),
        "coffee": ("KC=F", "USD/lb"),
        "palm_oil": ("FCPO=F", "MYR/MT"),
    }
    out: Dict[str, Dict] = {}
    stale = False
    try:
        import yfinance as yf
        syms = [v[0] for v in sym_map.values()]
        data = yf.download(syms, period="3d", interval="1d", group_by="ticker",
                           progress=False, threads=True)

        for cid, (sym, unit) in sym_map.items():
            try:
                closes = data[sym]["Close"].dropna() if len(syms) > 1 else data["Close"].dropna()
                if len(closes) < 1:
                    continue
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
                out[cid] = {
                    "price": round(last, 2),
                    "change_pct": round((last - prev) / prev * 100, 2) if prev else 0,
                    "currency": unit,
                    "as_of": datetime.utcnow().isoformat() + "Z",
                    "source": "yfinance",
                }
            except Exception:
                continue
    except Exception as e:
        stale = True
        logger.warning(f"commodities_prices yfinance failed: {e}")

    if not out:
        return jsonify({"success": False, "error": "no live commodity data; UI should show stale state",
                        "stale": True}), 503
    return jsonify({"success": True, "data": out, "stale": stale,
                    "as_of": datetime.utcnow().isoformat() + "Z"})


# ---- /api/global/history ----------------------------------------------------

_HISTORY_CACHE: Dict[str, Dict] = {}
_HISTORY_TTL_SECS = 600  # 10 min — yfinance is slow and the data is daily


@bp.route("/api/global/correlations", methods=["GET"])
@ttl_cache(seconds=600)
def global_correlations():
    """Pearson correlations + linear-regression beta of each global index
    versus Nifty 50 daily returns. Computed from a yfinance pull —
    no hardcoded values.

    Query params:
        days: 20..180 trading-day window (default 60)
    Response: {data: [{id, label, symbol, corr, beta, sample_n}, ...]}
    """
    try:
        days = max(20, min(180, int(request.args.get("days", 60))))
    except (TypeError, ValueError):
        days = 60

    indices = [
        ("^IXIC", "nasdaq", "NASDAQ"),
        ("^GSPC", "sp500", "S&P 500"),
        ("^DJI", "dow", "Dow Jones"),
        ("^GDAXI", "dax", "DAX"),
        ("^FTSE", "ftse", "FTSE 100"),
        ("^N225", "nikkei", "Nikkei 225"),
        ("^HSI", "hangseng", "Hang Seng"),
        ("000001.SS", "shanghai", "Shanghai"),
        ("^NSEI", "nifty", "Nifty 50"),
    ]
    out = []
    try:
        import yfinance as yf
        period_days = int(days * 1.6) + 10
        syms = [s for s, _, _ in indices]
        data = yf.download(syms, period=f"{period_days}d", interval="1d",
                           group_by="ticker", progress=False, threads=True)

        returns = {}
        for sym, key, _label in indices:
            try:
                closes = data[sym]["Close"].dropna() if len(syms) > 1 else data["Close"].dropna()
                tail = closes.tail(days + 1)
                if len(tail) < 8:
                    continue
                rets = tail.pct_change().dropna().tolist()
                returns[key] = rets
            except Exception:
                continue

        nifty_rets = returns.get("nifty")
        if not nifty_rets:
            return jsonify({"success": False, "error": "Nifty returns unavailable"}), 503

        for sym, key, label in indices:
            if key == "nifty":
                continue
            rets = returns.get(key)
            if not rets:
                continue
            n = min(len(rets), len(nifty_rets))
            x = rets[-n:]
            y = nifty_rets[-n:]
            if n < 8:
                continue
            mean_x = sum(x) / n
            mean_y = sum(y) / n
            cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n)) / n
            var_x = sum((v - mean_x) ** 2 for v in x) / n
            var_y = sum((v - mean_y) ** 2 for v in y) / n
            denom = (var_x * var_y) ** 0.5
            corr = (cov / denom) if denom > 0 else 0.0
            beta = (cov / var_x) if var_x > 0 else 0.0
            out.append({
                "id": key, "label": label, "symbol": sym,
                "corr": round(corr, 3),
                "beta": round(beta, 3),
                "sample_n": n,
            })
        out.sort(key=lambda r: abs(r["corr"]), reverse=True)
    except Exception as exc:
        logger.warning(f"global_correlations failed: {exc}")
        return jsonify({"success": False, "error": str(exc), "data": []}), 503

    return jsonify({"success": True, "data": out, "days": days,
                    "as_of": datetime.utcnow().isoformat() + "Z"})


@bp.route("/api/global/calendar", methods=["GET"])
@ttl_cache(seconds=300)
def global_calendar():
    """Upcoming policy + earnings + IPO + M&A catalysts.
    Replaces the hardcoded WEEK_EVENTS array on global.html.
    """
    db = _get_db() if _get_db else None
    if not db:
        return jsonify({"success": False, "error": "db unavailable"}), 503

    try:
        days = max(1, min(14, int(request.args.get("days", 7))))
    except (TypeError, ValueError):
        days = 7

    out = []
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT title, summary, event_type, source, link,
                      published_at, created_at, magnitude
               FROM events
               WHERE event_type IN ('policy','earnings','ipo','merger')
                 AND created_at >= datetime('now', '-3 days')
               ORDER BY created_at DESC LIMIT 60""",
        )
        from datetime import datetime as _dt
        seen_titles = set()
        for r in cur.fetchall():
            row = dict(r)
            ttl = (row.get("title") or "").strip()
            if not ttl or ttl in seen_titles:
                continue
            seen_titles.add(ttl)
            ts = row.get("published_at") or row.get("created_at")
            try:
                t = _dt.fromisoformat(str(ts).replace("Z", "").replace(" ", "T")[:19])
                day_lbl = t.strftime("%a")
            except Exception:
                day_lbl = ""
            mag = float(row.get("magnitude") or 0)
            impact = "high" if mag >= 7 else ("med" if mag >= 4 else "low")
            blob = (ttl + " " + (row.get("source") or "")).lower()
            region = ("US" if any(k in blob for k in ("fed", "us ", "wall street", "fomc")) else
                      "EU" if any(k in blob for k in ("ecb", "european", "bank of england", "lagarde")) else
                      "CN" if any(k in blob for k in ("china", "pboc", "shanghai")) else
                      "JP" if any(k in blob for k in ("japan", "boj", "tokyo")) else
                      "IN")
            out.append({
                "day": day_lbl,
                "label": ttl[:120],
                "event_type": row.get("event_type"),
                "region": region,
                "impact": impact,
                "ts": str(ts),
                "link": row.get("link"),
            })
            if len(out) >= 12:
                break
    except Exception as e:
        logger.warning(f"global_calendar query failed: {e}")

    return jsonify({"success": True, "data": out, "days": days,
                    "as_of": datetime.utcnow().isoformat() + "Z"})


@bp.route("/api/global/history", methods=["GET"])
def global_history():
    """Daily closes for the headline world indices over the last N trading days.

    Powers the 30-day normalized-returns line chart and the overnight-impact
    bar chart on the Global page. Cached for 10 minutes — yfinance bulk
    downloads are slow (~3-6s) and the underlying data is daily.

    Query params:
        days: int, default 30, capped at 90.
    """
    try:
        days = max(5, min(90, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30

    cache_key = f"days={days}"
    cached = _HISTORY_CACHE.get(cache_key)
    if cached and (time.time() - cached["_ts"]) < _HISTORY_TTL_SECS:
        return jsonify({"success": True, "data": cached["payload"], "cached": True})

    indices = [
        ("^IXIC", "nasdaq", "NASDAQ"),
        ("^GSPC", "sp500", "S&P 500"),
        ("^DJI", "dow", "Dow Jones"),
        ("^GDAXI", "dax", "DAX"),
        ("^FTSE", "ftse", "FTSE 100"),
        ("^N225", "nikkei", "Nikkei 225"),
        ("^HSI", "hangseng", "Hang Seng"),
        ("000001.SS", "shanghai", "Shanghai"),
        ("^NSEI", "nifty", "Nifty 50"),
    ]

    payload: Dict = {"indices": {}, "as_of": None, "days": days, "stale": False}
    try:
        import yfinance as yf
        # Pull a few extra calendar days to cover weekends/holidays
        period_days = int(days * 1.6) + 10
        syms = [s for s, _, _ in indices]
        data = yf.download(syms, period=f"{period_days}d", interval="1d",
                           group_by="ticker", progress=False, threads=True)

        for sym, key, label in indices:
            try:
                closes = data[sym]["Close"].dropna() if len(syms) > 1 else data["Close"].dropna()
                if len(closes) < 2:
                    continue
                # Take last `days` trading days only
                tail = closes.tail(days)
                series = [
                    {"date": idx.strftime("%Y-%m-%d"), "close": round(float(val), 2)}
                    for idx, val in tail.items()
                ]
                payload["indices"][key] = {
                    "label": label, "symbol": sym, "series": series,
                }
            except Exception as e:
                logger.debug(f"global_history: skipped {sym}: {e}")
                continue

        payload["as_of"] = datetime.utcnow().isoformat() + "Z"
    except Exception as exc:
        payload["stale"] = True
        payload["error"] = str(exc)
        logger.warning(f"global_history yfinance failed: {exc}")

    if not payload["indices"]:
        return jsonify({"success": False, "error": "no historical data available",
                        "data": payload}), 503

    _HISTORY_CACHE[cache_key] = {"_ts": time.time(), "payload": payload}
    return jsonify({"success": True, "data": payload, "cached": False})


# ---- /api/movers/spike — universe-wide intraday price-spike scanner ---------
# Catches Mobikwik-like ±10% moves that have NO matching news event yet.
# Without this scanner the front page only shows what news ingestion captured;
# fast-moving small/mid caps slip through silently.

_MOVERS_CACHE: Dict = {"_ts": 0.0, "payload": []}
_MOVERS_TTL_SECS = 300  # 5 min — yfinance bulk pull is slow


def _broad_universe() -> List[str]:
    """Build a broad NSE coverage list. Pulls from scraper config (large/mid
    rotation) plus an explicit small-cap recent-listings list so names like
    MOBIKWIK, ZAGGLE, etc. aren't invisible just because they're new."""
    universe: set = set()
    try:
        # Lazy import — avoids blowing up if the scraper package is missing
        import sys, os as _os
        sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "scraper"))
        from config import (
            MONITORED_STOCKS, TICKER_NEWS_ROTATION, LARGE_CAP, MID_CAP, SMALL_CAP,
        )
        universe.update(MONITORED_STOCKS or [])
        universe.update(TICKER_NEWS_ROTATION or [])
        universe.update(LARGE_CAP or [])
        universe.update(MID_CAP or [])
        universe.update(SMALL_CAP or [])
    except Exception as exc:
        logger.debug(f"movers: scraper config not available: {exc}")
    # Recent listings + niche small/mid caps the scraper rotation may not cover
    extras = [
        # Fintech / payments / new-economy (Mobikwik et al.)
        'MOBIKWIK', 'ZAGGLE', 'CARTRADE', 'PAYTM', 'POLICYBZR', 'CAMS', 'RAILTEL',
        'NYKAA', 'EASEMYTRIP', 'IXIGO', 'DELHIVERY', 'LICI', 'BLACKBOX',
        # Other niche small caps with frequent moves
        'NEULAND', 'BLUESTARCO', 'ELECON', 'ELECTCAST', 'CAPLIPOINT', 'ROUTE',
        'INTELLECT', 'ZENSARTECH', 'SUBROS', 'CARBORUNIV', 'TIMETECHNO',
        # Defence / capex spikes
        'BHARATELE', 'AVANTIFEED', 'KIRLOSBROS', 'GUFICBIO', 'SBICARD',
        # Recently active midcaps
        'IDEAFORGE', 'HAPPSTMNDS', 'TBOTEK', 'SIGNATURE', 'YATHARTH', 'SENCO',
    ]
    universe.update(extras)
    return sorted(universe)


def _ticker_sector_map() -> Dict[str, str]:
    """Return cached TICKER -> SECTOR map from scraper.stock_universe.
    Empty dict if scraper package isn't importable."""
    cache = getattr(_ticker_sector_map, "_cache", None)
    if cache is not None:
        return cache
    try:
        import sys, os as _os
        sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "scraper"))
        from stock_universe import TICKER_SECTOR_OVERRIDE  # type: ignore
        cache = dict(TICKER_SECTOR_OVERRIDE)
    except Exception as exc:
        logger.debug(f"sector map unavailable: {exc}")
        cache = {}
    _ticker_sector_map._cache = cache  # type: ignore[attr-defined]
    return cache


def _recent_ticker_news(tickers: List[str], hours: int = 24) -> Dict[str, Dict]:
    """For each ticker, return the freshest matching event row from the DB.
    Output: {TICKER: {title, link, source, published_at}}.

    Used to attach a corroborating headline to mover items so the UI can
    explain why a stock is spiking instead of just "X jumps Y%".
    """
    out: Dict[str, Dict] = {}
    if not tickers:
        return out
    db = _get_db() if _get_db else None
    if not db:
        return out
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT title, link, source, published_at, created_at, companies, sentiment
                FROM events
                WHERE created_at >= datetime('now', '-{int(hours)} hours')
                ORDER BY created_at DESC
                LIMIT 600""")
        wanted = {t.upper() for t in tickers}
        for row in cur.fetchall():
            companies = row["companies"]
            if isinstance(companies, str):
                try:
                    companies = json.loads(companies)
                except Exception:
                    companies = [c.strip() for c in companies.split(",") if c.strip()]
            for c in (companies or []):
                cu = (c or "").upper()
                if cu in wanted and cu not in out:
                    out[cu] = {
                        "title": row["title"],
                        "link": row["link"],
                        "source": row["source"],
                        "published_at": row["published_at"] or row["created_at"],
                        "sentiment": row["sentiment"],
                    }
            if len(out) == len(wanted):
                break
    except Exception as e:
        logger.debug(f"recent_ticker_news lookup failed: {e}")
    return out


def _enrich_movers(raw: List[Dict]) -> List[Dict]:
    """Attach sector, alpha_score, reasoning bullets, and sympathy peers to
    each mover row. Mutates and returns the list.

    Alpha score (0-100) =
        magnitude (40) + volume confirmation (30) + news corroboration (20)
        + sector momentum (10).
    """
    if not raw:
        return raw
    sec_map = _ticker_sector_map()
    tickers = [r.get("ticker") for r in raw if r.get("ticker")]
    news_map = _recent_ticker_news(tickers, hours=24)

    # Sector aggregates: how many movers in each sector and net direction
    sector_movers: Dict[str, List[Dict]] = {}
    for r in raw:
        s = sec_map.get((r.get("ticker") or "").upper())
        r["sector"] = s
        if s:
            sector_movers.setdefault(s, []).append(r)

    for r in raw:
        pct = float(r.get("pct_change") or 0)
        vol_z = r.get("vol_multiple")
        sector = r.get("sector")
        ticker = r.get("ticker")
        direction = "up" if pct >= 0 else "down"

        # ---- alpha score components ----
        s_move = min(40.0, abs(pct) * 4.0)         # 10% → 40
        s_vol = 0.0
        if isinstance(vol_z, (int, float)) and vol_z > 0:
            s_vol = min(30.0, max(0.0, (vol_z - 1.0) * 15.0))  # 3× → 30
        news = news_map.get((ticker or "").upper())
        s_news = 20.0 if news else 0.0
        # Sector momentum: at least 2 OTHER movers in same direction in this sector
        sector_peers_same_dir: List[Dict] = []
        if sector:
            for other in sector_movers.get(sector, []):
                if other is r:
                    continue
                opct = float(other.get("pct_change") or 0)
                if (opct >= 0) == (pct >= 0):
                    sector_peers_same_dir.append(other)
        s_sector = 10.0 if len(sector_peers_same_dir) >= 2 else (5.0 if len(sector_peers_same_dir) == 1 else 0.0)

        alpha = round(s_move + s_vol + s_news + s_sector, 1)
        r["alpha_score"] = alpha
        r["alpha_components"] = {
            "magnitude": round(s_move, 1),
            "volume": round(s_vol, 1),
            "news": round(s_news, 1),
            "sector": round(s_sector, 1),
        }
        r["confidence"] = "high" if alpha >= 65 else ("medium" if alpha >= 40 else "low")

        # ---- reasoning bullets ----
        bullets: List[Dict] = []
        sign = "+" if pct >= 0 else ""
        bullets.append({
            "kind": "move",
            "label": f"Spot move {sign}{pct:.2f}% intraday (last close ₹{r.get('price')})",
        })
        if isinstance(vol_z, (int, float)) and vol_z > 0:
            if vol_z >= 2.5:
                bullets.append({"kind": "volume", "label": f"Volume {vol_z:.1f}× the recent average — strongly confirms the move"})
            elif vol_z >= 1.5:
                bullets.append({"kind": "volume", "label": f"Volume {vol_z:.1f}× average — moderately confirmed"})
            else:
                bullets.append({"kind": "volume", "label": f"Volume only {vol_z:.1f}× average — thin participation, weaker signal"})
        if news:
            headline = (news.get("title") or "")[:140]
            src = news.get("source") or "news"
            bullets.append({
                "kind": "news",
                "label": f"Headline ({src}): {headline}",
                "link": news.get("link"),
            })
        else:
            bullets.append({
                "kind": "no_news",
                "label": "No corroborating headline in last 24h — likely technical/momentum or unannounced flow. Trade with tighter stops.",
            })
        if sector and sector_peers_same_dir:
            peer_names = ", ".join(p.get("ticker") for p in sector_peers_same_dir[:3])
            bullets.append({
                "kind": "sector",
                "label": f"Sector context: {sector} sees {len(sector_peers_same_dir)+1} names moving {direction} today (incl. {peer_names})",
            })
        elif sector:
            bullets.append({
                "kind": "sector",
                "label": f"Idiosyncratic — only {ticker} moving in {sector} today, not a sector trade",
            })
        r["reasoning"] = bullets

        # ---- sympathy / suggested stocks ----
        # Prefer same-direction peers from the live scan; fall back to sector names
        suggested: List[Dict] = []
        for p in sector_peers_same_dir[:4]:
            ppct = float(p.get("pct_change") or 0)
            psign = "+" if ppct >= 0 else ""
            suggested.append({
                "ticker": p.get("ticker"),
                "pct_change": ppct,
                "reason": f"Same sector, also {psign}{ppct:.1f}% today",
            })
        if not suggested and sector:
            # cold sector — pick a couple of well-known peers from the same sector
            sector_universe = [t for t, s in sec_map.items() if s == sector and t != ticker]
            for t in sector_universe[:3]:
                suggested.append({
                    "ticker": t,
                    "pct_change": None,
                    "reason": f"{sector} peer — watch for sympathy",
                })
        r["suggested"] = suggested

    return raw


@bp.route("/api/movers/spike", methods=["GET"])
def movers_spike():
    """Return stocks across the broad NSE universe with abnormal intraday moves.

    A "mover" is any stock whose latest close vs prior close pct change exceeds
    `min_pct` (default 5%). This catches small/mid-cap surges (e.g. Mobikwik
    +15%) that the news pipeline missed because no source indexed them.

    Each row is enriched with: sector, alpha_score (0-100), reasoning bullets,
    and suggested sympathy stocks — so the UI can explain *why* a stock is
    spiking and *what to watch next*, instead of just "X jumps Y%".

    Cached 5 min — yfinance bulk download for 300+ tickers is slow (~6-12s).

    Query params:
        min_pct: float, default 5.0
        limit: int, default 25 (cap to avoid flooding the UI)
    """
    try:
        min_pct = float(request.args.get("min_pct", 5.0))
    except (TypeError, ValueError):
        min_pct = 5.0
    try:
        limit = max(5, min(100, int(request.args.get("limit", 25))))
    except (TypeError, ValueError):
        limit = 25

    now = time.time()
    if (now - _MOVERS_CACHE["_ts"]) < _MOVERS_TTL_SECS and _MOVERS_CACHE.get("payload"):
        cached = [m for m in _MOVERS_CACHE["payload"] if abs(m.get("pct_change", 0)) >= min_pct]
        return jsonify({"success": True, "data": cached[:limit], "cached": True})

    out: List[Dict] = []
    try:
        import yfinance as yf
        universe = _broad_universe()
        # yfinance NSE suffix
        symbols = [f"{t}.NS" for t in universe]
        # Bulk download in 2-day window so we have prev-close + latest
        data = yf.download(symbols, period="3d", interval="1d",
                           group_by="ticker", progress=False, threads=True,
                           auto_adjust=False)

        for sym, ticker in zip(symbols, universe):
            try:
                df = data[sym] if len(symbols) > 1 else data
                if df is None or df.empty:
                    continue
                closes = df["Close"].dropna()
                vols = df["Volume"].dropna() if "Volume" in df.columns else None
                if len(closes) < 2:
                    continue
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])
                if not prev:
                    continue
                pct = (last - prev) / prev * 100.0
                if abs(pct) < min_pct:
                    continue
                # Pull avg-volume for a z-score-ish signal alongside the move
                vol_z = None
                if vols is not None and len(vols) >= 2:
                    try:
                        latest_vol = float(vols.iloc[-1])
                        avg_vol = float(vols.iloc[:-1].mean()) or 1.0
                        vol_z = round(latest_vol / avg_vol, 2)
                    except Exception:
                        pass
                out.append({
                    "ticker": ticker,
                    "price": round(last, 2),
                    "pct_change": round(pct, 2),
                    "vol_multiple": vol_z,
                    "as_of": datetime.utcnow().isoformat() + "Z",
                })
            except Exception:
                continue
    except Exception as exc:
        logger.warning(f"movers/spike scan failed: {exc}")
        return jsonify({"success": False, "error": str(exc), "data": []}), 503

    # Sort by absolute move desc — biggest signals first
    out.sort(key=lambda r: abs(r.get("pct_change", 0)), reverse=True)
    # Enrich BEFORE caching — alpha_score then becomes the primary sort key so
    # confirmed (high-vol + news + sector-wide) moves bubble above noise.
    try:
        _enrich_movers(out)
        out.sort(key=lambda r: r.get("alpha_score", 0), reverse=True)
    except Exception as exc:
        logger.warning(f"movers/spike enrichment failed: {exc}")
    _MOVERS_CACHE["_ts"] = now
    _MOVERS_CACHE["payload"] = out
    filtered = [m for m in out if abs(m.get("pct_change", 0)) >= min_pct]
    return jsonify({"success": True, "data": filtered[:limit], "cached": False,
                    "scanned": len(out), "as_of": datetime.utcnow().isoformat() + "Z"})


# ---- IPO endpoints ----------------------------------------------------------

@bp.route("/api/ipos", methods=["GET"])
@ttl_cache(seconds=120)
def api_ipos_list():
    """List IPOs filtered by status/sector. Returns alpha-ranked rows.

    Query params:
        status: upcoming | open | allotment | listed | all  (default: all)
        sector: sector name or 'all'  (default: all)
        limit:  max rows (default 100)
    """
    db = _get_db() if _get_db else None
    if not db:
        return jsonify({"success": False, "error": "db unavailable"}), 503
    status = (request.args.get("status") or "all").strip().lower()
    sector = (request.args.get("sector") or "all").strip()
    try:
        limit = max(1, min(500, int(request.args.get("limit", "100"))))
    except ValueError:
        limit = 100
    try:
        rows = db.get_ipos(status=status, sector=sector, limit=limit) or []
        # Parse factors_json for clients that want the breakdown
        for r in rows:
            blob = r.get("ipo_factors_json")
            if blob:
                try:
                    r["factors"] = json.loads(blob)
                except Exception:
                    r["factors"] = None
            # ISO-format dates so JSON is stable
            for k in ("open_date", "close_date", "allotment_date", "listing_date",
                     "created_at", "updated_at"):
                v = r.get(k)
                if v and not isinstance(v, str):
                    r[k] = str(v)
        return jsonify({"success": True, "data": rows, "count": len(rows)})
    except Exception as e:
        logger.exception("api_ipos_list failed")
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/ipos/<symbol>", methods=["GET"])
def api_ipo_detail(symbol):
    """Detailed view of a single IPO including its factor breakdown."""
    db = _get_db() if _get_db else None
    if not db:
        return jsonify({"success": False, "error": "db unavailable"}), 503
    try:
        row = db.get_ipo_by_symbol(symbol.upper().strip())
        if not row:
            return jsonify({"success": False, "error": "IPO not found"}), 404
        blob = row.get("ipo_factors_json")
        if blob:
            try:
                row["factors"] = json.loads(blob)
            except Exception:
                row["factors"] = None
        for k in ("open_date", "close_date", "allotment_date", "listing_date",
                 "created_at", "updated_at"):
            v = row.get(k)
            if v and not isinstance(v, str):
                row[k] = str(v)
        return jsonify({"success": True, "data": row})
    except Exception as e:
        logger.exception("api_ipo_detail failed")
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/ipos/stats", methods=["GET"])
def api_ipos_stats():
    """Summary stats for the IPO dashboard pill."""
    db = _get_db() if _get_db else None
    if not db:
        return jsonify({"success": False, "error": "db unavailable"}), 503
    try:
        return jsonify({"success": True, "data": db.get_ipo_stats()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/ipos/events", methods=["GET"])
@ttl_cache(seconds=120)
def api_ipos_events():
    """Derive event-style entries from IPO state for an activity feed.

    Events emitted (chronologically, newest first):
      - listed:           IPO listed in the past N days (with gain/loss)
      - subscription_close: IPO close_date is today/yesterday
      - subscription_open: IPO opened in last 2 days
      - milestone:        Sub > 10x or QIB > 30x
      - filing:           IPO created in DB recently (upcoming, no other tag)

    Events are computed live from the ipos table — no separate event log
    needed. This avoids stale state and keeps the API stateless.
    """
    db = _get_db() if _get_db else None
    if not db:
        return jsonify({"success": False, "error": "db unavailable"}), 503
    try:
        limit = max(1, min(100, int(request.args.get("limit", "30"))))
    except ValueError:
        limit = 30

    today = datetime.utcnow().date()
    events = []

    try:
        rows = db.get_ipos(limit=400) or []
        for r in rows:
            symbol = r.get("symbol")
            name = r.get("company_name") or symbol
            sector = r.get("sector")
            alpha = float(r.get("ipo_alpha_score") or 0)
            sub_total = float(r.get("sub_total") or 0)
            sub_qib = float(r.get("sub_qib") or 0)
            status = r.get("status")
            sub_hni = float(r.get("sub_hni") or 0)
            issue_low = r.get("issue_price_low")
            issue_high = r.get("issue_price_high")

            # Helper to format dates
            def parse_date(s):
                if not s:
                    return None
                if isinstance(s, str):
                    try:
                        return datetime.fromisoformat(s.split('T')[0]).date()
                    except (ValueError, AttributeError):
                        return None
                try:
                    return s.date()
                except AttributeError:
                    return s

            # Build event(s) per IPO based on its state
            listing_date = parse_date(r.get("listing_date"))
            close_date = parse_date(r.get("close_date"))
            open_date = parse_date(r.get("open_date"))
            updated_at = parse_date(r.get("updated_at"))

            # 1. Listed event (most recent state)
            if status == "listed" and listing_date:
                days_since = (today - listing_date).days
                if 0 <= days_since <= 21:
                    gain = r.get("listing_gain_pct")
                    listing_price = r.get("listing_price")
                    is_pop = gain is not None and gain >= 5
                    is_flop = gain is not None and gain <= -5
                    tag = "pop" if is_pop else ("flop" if is_flop else "listed")
                    detail = (
                        f"Listed at ₹{listing_price:,.2f}" if listing_price else "Listed"
                    )
                    if gain is not None:
                        detail += f" · {gain:+.1f}% vs issue"
                    events.append({
                        "kind": "listed",
                        "tag": tag,
                        "tag_label": "Listed " + ("Pop" if is_pop else ("Flop" if is_flop else "")),
                        "ts": listing_date.isoformat(),
                        "ts_display": _ago_display(listing_date, today),
                        "title": f"<strong>{name}</strong> listed",
                        "detail": detail,
                        "symbol": symbol,
                        "ticker": r.get("ticker"),
                        "sector": sector,
                        "alpha": alpha,
                        "auto_added": bool(r.get("promoted_to_signal")),
                    })
                    continue  # don't emit other events for listed IPOs

            # 2. Subscription closing today/yesterday
            if status == "open" and close_date:
                days_to_close = (close_date - today).days
                if -1 <= days_to_close <= 1:
                    sub_evidence = []
                    if sub_qib > 0: sub_evidence.append(f"QIB {sub_qib:.1f}x")
                    if sub_hni > 0: sub_evidence.append(f"HNI {sub_hni:.1f}x")
                    if sub_total > 0: sub_evidence.append(f"Total {sub_total:.1f}x")
                    when = "today" if days_to_close == 0 else ("tomorrow" if days_to_close == 1 else "yesterday")
                    events.append({
                        "kind": "closing",
                        "tag": "close",
                        "tag_label": "Closing " + when.capitalize(),
                        "ts": close_date.isoformat(),
                        "ts_display": _ago_display(close_date, today),
                        "title": f"<strong>{name}</strong> subscription closes {when}",
                        "detail": " · ".join(sub_evidence) if sub_evidence else "Final day to apply",
                        "symbol": symbol,
                        "sector": sector,
                        "alpha": alpha,
                        "issue_band": _format_band(issue_low, issue_high),
                    })
                    continue

            # 3. Just opened
            if status == "open" and open_date:
                days_since_open = (today - open_date).days
                if 0 <= days_since_open <= 3:
                    detail_parts = []
                    if sub_total > 0:
                        detail_parts.append(f"{sub_total:.1f}x already booked")
                    if sub_qib >= 5:
                        detail_parts.append(f"QIB at {sub_qib:.1f}x")
                    band = _format_band(issue_low, issue_high)
                    if band:
                        detail_parts.append(f"Band {band}")
                    events.append({
                        "kind": "opened",
                        "tag": "open",
                        "tag_label": "Open Now",
                        "ts": open_date.isoformat(),
                        "ts_display": _ago_display(open_date, today),
                        "title": f"<strong>{name}</strong> opened for subscription",
                        "detail": " · ".join(detail_parts) if detail_parts else "Subscription window open",
                        "symbol": symbol,
                        "sector": sector,
                        "alpha": alpha,
                    })
                    continue

            # 4. Subscription milestone (oversubscription)
            if status in ("open", "allotment") and (sub_total >= 10 or sub_qib >= 30):
                ts = updated_at or open_date or today
                events.append({
                    "kind": "milestone",
                    "tag": "milestone",
                    "tag_label": "Heavy Demand",
                    "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    "ts_display": _ago_display(ts, today) if hasattr(ts, "isoformat") else "recent",
                    "title": f"<strong>{name}</strong> oversubscribed",
                    "detail": f"QIB {sub_qib:.1f}x · HNI {sub_hni:.1f}x · Total {sub_total:.1f}x",
                    "symbol": symbol,
                    "sector": sector,
                    "alpha": alpha,
                })
                continue

            # 5. Upcoming / filing event for newly-tracked IPOs
            if status == "upcoming" and open_date:
                days_to_open = (open_date - today).days
                if 0 <= days_to_open <= 14:
                    detail_parts = []
                    band = _format_band(issue_low, issue_high)
                    if band:
                        detail_parts.append(f"Band {band}")
                    if r.get("issue_size_cr"):
                        detail_parts.append(f"Issue size ₹{float(r['issue_size_cr']):.0f}Cr")
                    when = (
                        "today" if days_to_open == 0
                        else "tomorrow" if days_to_open == 1
                        else f"in {days_to_open}d"
                    )
                    events.append({
                        "kind": "upcoming",
                        "tag": "filing",
                        "tag_label": "Upcoming",
                        "ts": open_date.isoformat(),
                        "ts_display": when,
                        "title": f"<strong>{name}</strong> opens {when}",
                        "detail": " · ".join(detail_parts) if detail_parts else "Awaiting subscription",
                        "symbol": symbol,
                        "sector": sector,
                        "alpha": alpha,
                    })
                    continue

        # Sort newest first by ts (descending)
        events.sort(key=lambda e: e["ts"], reverse=True)
        events = events[:limit]
        return jsonify({"success": True, "data": events, "count": len(events)})
    except Exception as e:
        logger.exception("api_ipos_events failed")
        return jsonify({"success": False, "error": str(e)}), 500


def _ago_display(d, today):
    """Format a date relative to today: 'today', 'yesterday', '3d ago', etc."""
    if not d:
        return ""
    try:
        if hasattr(d, "date"):
            d = d.date()
    except Exception:
        return str(d)
    delta = (today - d).days
    if delta == 0:    return "today"
    if delta == 1:    return "yesterday"
    if delta == -1:   return "tomorrow"
    if delta < 0:     return f"in {abs(delta)}d"
    if delta <= 7:    return f"{delta}d ago"
    if delta <= 30:   return f"{delta // 7}w ago"
    return d.strftime("%b %d")


def _format_band(low, high):
    if not low and not high:
        return ""
    if low and high and low != high:
        return f"₹{float(low):.0f}–₹{float(high):.0f}"
    v = low or high
    return f"₹{float(v):.0f}" if v else ""


@bp.route("/api/ipos/refresh", methods=["POST"])
def api_ipos_refresh():
    """Trigger an IPO data refresh. Guarded by ADMIN_TOKEN to avoid abuse.

    Pass X-Admin-Token header to authorize. Skips news lookups when ?fast=1.
    """
    expected = os.getenv("ADMIN_TOKEN") or os.getenv("ADMIN_SECRET")
    given = request.headers.get("X-Admin-Token") or request.headers.get("X-Admin-Secret")
    if not expected or expected in ("changeme", "admin", ""):
        return jsonify({"success": False, "error": "admin token not configured"}), 403
    if not given or given != expected:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    db = _get_db() if _get_db else None
    if not db:
        return jsonify({"success": False, "error": "db unavailable"}), 503
    fast = request.args.get("fast", "0") == "1"
    try:
        # Lazy import so api_v2 doesn't break if scraper deps are missing
        from ipo_scraper import refresh_ipos
        result = refresh_ipos(db, fetch_news=not fast)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.exception("api_ipos_refresh failed")
        return jsonify({"success": False, "error": str(e)}), 500


# ---- M&A (mergers & acquisitions) -------------------------------------------

@bp.route("/api/ma", methods=["GET"])
@ttl_cache(120)
def api_ma_list():
    """Paginated list of M&A deals. Filters: status, sector, min_value_cr, days, limit."""
    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": False, "error": "db not ready"}), 503
    try:
        from ma_scraper import list_deals
        status = request.args.get("status") or None
        if status == "all":
            status = None
        sector = request.args.get("sector") or None
        try:
            min_value = float(request.args.get("min_value_cr") or 0) or None
        except ValueError:
            min_value = None
        days = max(1, min(int(request.args.get("days", "365")), 1825))
        limit = max(1, min(int(request.args.get("limit", "100")), 500))
        rows = list_deals(db, status=status, sector=sector, min_value_cr=min_value,
                          days=days, limit=limit)
        return jsonify({"success": True, "data": rows, "count": len(rows)})
    except Exception as e:
        logger.exception("api_ma_list failed")
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/ma/stats", methods=["GET"])
@ttl_cache(300)
def api_ma_stats():
    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": False, "error": "db not ready"}), 503
    try:
        from ma_scraper import ma_stats
        days = max(1, min(int(request.args.get("days", "90")), 1825))
        return jsonify({"success": True, "data": ma_stats(db, days=days)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/ma/deal/<deal_id>", methods=["GET"])
@ttl_cache(120)
def api_ma_deal(deal_id: str):
    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": False, "error": "db not ready"}), 503
    try:
        from ma_scraper import deal_by_id
        deal = deal_by_id(db, deal_id)
        if not deal:
            return jsonify({"success": False, "error": "deal not found"}), 404
        return jsonify({"success": True, "data": deal})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/ma/ticker/<symbol>", methods=["GET"])
@ttl_cache(120)
def api_ma_ticker(symbol: str):
    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": False, "error": "db not ready"}), 503
    try:
        from ma_scraper import deals_by_ticker
        return jsonify({"success": True, "data": deals_by_ticker(db, symbol)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/ma/refresh", methods=["POST"])
def api_ma_refresh():
    """Manual M&A scrape trigger. Body optional: {"days": 7}."""
    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": False, "error": "db not ready"}), 503
    try:
        from ma_scraper import refresh_ma_deals
        body = request.get_json(silent=True) or {}
        days = max(1, min(int(body.get("days") or 7), 30))
        result = refresh_ma_deals(db, days=days)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.exception("api_ma_refresh failed")
        return jsonify({"success": False, "error": str(e)}), 500


# ---- company logos ---------------------------------------------------------

@bp.route("/api/logo/<ticker>", methods=["GET"])
def api_logo(ticker: str):
    """Serve a company logo by ticker. Tries cache → parqet (NS/BO) → SVG initials.

    Long browser cache (30d) since logos rarely change; bytes are also cached
    on disk so repeat misses don't re-hit external sources.
    """
    try:
        from logo_service import get_logo
        body, mime, real = get_logo(ticker)
        from flask import Response
        resp = Response(body, mimetype=mime)
        # 30d browser cache for real logos, 1h for SVG fallbacks (in case a real
        # logo becomes available later).
        max_age = 2592000 if real else 3600
        resp.headers["Cache-Control"] = f"public, max-age={max_age}"
        return resp
    except Exception as e:
        logger.exception(f"api_logo {ticker}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ---- registration -----------------------------------------------------------

def register(app, get_db: Callable, scraper_status_ref: Optional[Dict] = None):
    """Mount onto a Flask app. Idempotent."""
    global _get_db, _scraper_status, _started_at
    _get_db = get_db
    _scraper_status = scraper_status_ref or {}
    _started_at = time.time()
    if "api_v2" not in app.blueprints:
        app.register_blueprint(bp)
