"""geo_map.py — Geopolitical intelligence map endpoints.

Three GET endpoints under /api/geo/map/*:
  - hotspots           : world hotspots with severity, recency, exposed sectors
  - flows              : commodity flow lanes (Hormuz, Suez, Bab-el-Mandeb, ...)
  - exposure/<region>  : Indian sectors + tickers tied to a region, alpha-graded

Hotspots are seeded from a curated list in this file (they don't change daily;
new entries get a PR) but each one is augmented at request time with:
  - latest related events from the `events` / `signals` tables
  - dynamic severity overlay (count of fresh high-alpha signals matching
    region keywords) so a quiet region drops and a flaring one rises
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

bp = Blueprint("geo_map", __name__)

_get_db: Optional[Callable] = None


def init_app(app, get_db: Callable):
    global _get_db
    _get_db = get_db
    app.register_blueprint(bp)


# ─── Static seed data ──────────────────────────────────────────────────────
#
# Each hotspot has: id, label, lat/lng, kind, base_severity, keywords for
# matching news, sectors affected with direction (+1 = tailwind / -1 = headwind),
# and a short trader-facing thesis line.
#
# Kinds: 'chokepoint' / 'conflict' / 'central_bank' / 'commodity_hub' /
#        'tech_hub' / 'political'

SEED_HOTSPOTS: List[Dict] = [
    # ── Maritime chokepoints ─────────────────────────────────────────────
    {
        "id": "hormuz", "label": "Strait of Hormuz", "lat": 26.5667, "lng": 56.25,
        "kind": "chokepoint", "base_severity": 60,
        "keywords": ["hormuz", "iran", "tanker", "persian gulf", "iranian"],
        "sectors": {"ENERGY": +1, "OIL": +1, "AVIATION": -1, "FMCG": -0.5},
        "tickers_exposed": ["ONGC", "RELIANCE", "IOC", "BPCL", "HPCL", "GAIL", "OIL"],
        "thesis": "20% of global seaborne oil transits here. Closure → Brent +15-25%, "
                  "Indian energy upstream rerates, refiners get squeezed on margin.",
        "flow_volume_mbpd": 20.5,
    },
    {
        "id": "suez", "label": "Suez Canal", "lat": 30.5852, "lng": 32.2654,
        "kind": "chokepoint", "base_severity": 35,
        "keywords": ["suez", "egypt", "ever given"],
        "sectors": {"SHIPPING": -1, "LOGISTICS": -1, "ENERGY": +0.5, "TEXTILES": -0.5},
        "tickers_exposed": ["GESHIP", "SCI", "ADANIPORTS", "GPPL", "JSWINFRA"],
        "thesis": "Asia-Europe trade route. Disruption forces Cape diversion → "
                  "+10d transit time, container surcharges spike, freight stocks rally.",
        "flow_volume_mbpd": 9.0,
    },
    {
        "id": "bab_el_mandeb", "label": "Bab-el-Mandeb / Red Sea", "lat": 12.5833, "lng": 43.3333,
        "kind": "chokepoint", "base_severity": 70,
        "keywords": ["red sea", "houthi", "yemen", "bab el mandeb", "bab-el-mandeb"],
        "sectors": {"SHIPPING": +1, "ENERGY": +0.5, "INSURANCE": +0.5, "TEXTILES": -0.5},
        "tickers_exposed": ["GESHIP", "SCI", "ADANIPORTS", "GPPL", "RELIANCE"],
        "thesis": "Houthi attacks → Cape rerouting → +20d Asia-EU transit. "
                  "War-risk premium up 5x. Shipping rates 3-5x normal.",
        "flow_volume_mbpd": 8.8,
    },
    {
        "id": "malacca", "label": "Strait of Malacca", "lat": 2.5, "lng": 101.0,
        "kind": "chokepoint", "base_severity": 25,
        "keywords": ["malacca", "singapore", "south china sea"],
        "sectors": {"ENERGY": +0.5, "SHIPPING": -0.5},
        "tickers_exposed": ["RELIANCE", "ONGC", "ADANIPORTS"],
        "thesis": "30% of global trade transits. Indian imports from East Asia "
                  "flow here. Geopolitical heat zone.",
        "flow_volume_mbpd": 16.0,
    },
    {
        "id": "panama", "label": "Panama Canal", "lat": 9.08, "lng": -79.68,
        "kind": "chokepoint", "base_severity": 30,
        "keywords": ["panama canal", "drought panama"],
        "sectors": {"SHIPPING": -0.5, "LOGISTICS": -0.5},
        "tickers_exposed": ["GESHIP", "ADANIPORTS"],
        "thesis": "Drought-led toll spikes. Less direct India impact but signals "
                  "global shipping cost regime.",
        "flow_volume_mbpd": 4.0,
    },
    # ── Active conflict / tension ────────────────────────────────────────
    {
        "id": "israel_iran", "label": "Israel-Iran corridor", "lat": 32.0, "lng": 49.0,
        "kind": "conflict", "base_severity": 75,
        "keywords": ["israel", "iran", "hezbollah", "lebanon", "gaza", "tehran"],
        "sectors": {"ENERGY": +1, "DEFENCE": +1, "GOLD": +1, "AVIATION": -1, "FMCG": -0.5},
        "tickers_exposed": ["HAL", "BEL", "BDL", "MAZDOCK", "GRSE", "ONGC", "RELIANCE"],
        "thesis": "Direct conflict → oil +5-15%, defence stocks bid, Indian "
                  "MEA hedges with crude oil reserve releases.",
    },
    {
        "id": "russia_ukraine", "label": "Russia-Ukraine front", "lat": 49.0, "lng": 36.0,
        "kind": "conflict", "base_severity": 50,
        "keywords": ["russia", "ukraine", "putin", "moscow", "kyiv", "kremlin",
                     "sanctions russia"],
        "sectors": {"ENERGY": +0.5, "METALS": +1, "FERTILIZER": +1,
                    "DEFENCE": +1, "PHARMA": -0.5},
        "tickers_exposed": ["COALINDIA", "TATASTEEL", "JSWSTEEL", "NMDC", "VEDL",
                            "CHAMBLFERT", "GNFC", "RCF", "GSFC"],
        "thesis": "Russia is India's #4 crude supplier (discounted Urals). "
                  "Metals supply disrupted → JSWSTEEL / TATASTEEL benefit. "
                  "Fertilizer prices spike → urea cos catch bid.",
    },
    {
        "id": "taiwan", "label": "Taiwan Strait", "lat": 24.0, "lng": 120.0,
        "kind": "conflict", "base_severity": 40,
        "keywords": ["taiwan", "tsmc", "semiconductor", "chip"],
        "sectors": {"IT": -0.5, "AUTO": -1, "ELECTRONICS": -1, "DEFENCE": +0.5},
        "tickers_exposed": ["DIXON", "AMBER", "TATAELXSI", "POLYCAB", "MOTHERSON"],
        "thesis": "TSMC fab disruption → global semi shock. Indian auto + "
                  "electronics import chain breaks. Govt PLI scheme winners benefit.",
    },
    {
        "id": "north_korea", "label": "Korean Peninsula", "lat": 38.0, "lng": 127.0,
        "kind": "conflict", "base_severity": 25,
        "keywords": ["north korea", "kim jong", "missile test"],
        "sectors": {"DEFENCE": +0.5, "GOLD": +0.5},
        "tickers_exposed": ["HAL", "BEL"],
        "thesis": "Tail-risk pin. Rarely market-moving for India unless escalation.",
    },
    # ── Central banks ─────────────────────────────────────────────────────
    {
        "id": "fed", "label": "US Federal Reserve", "lat": 38.8921, "lng": -77.0467,
        "kind": "central_bank", "base_severity": 50,
        "keywords": ["fed", "fomc", "powell", "us inflation", "us cpi", "us jobs"],
        "sectors": {"IT": +0.5, "PHARMA": +0.5, "BFSI": +0.5, "METALS": -0.5},
        "tickers_exposed": ["INFY", "TCS", "WIPRO", "HDFCBANK", "ICICIBANK"],
        "thesis": "Pivot dovish → IT exports + Indian financials rally. "
                  "Higher-for-longer → dollar strong, metals + emerging mkt drag.",
    },
    {
        "id": "ecb", "label": "ECB Frankfurt", "lat": 50.1109, "lng": 8.6821,
        "kind": "central_bank", "base_severity": 30,
        "keywords": ["ecb", "lagarde", "eurozone", "european central bank"],
        "sectors": {"IT": +0.3, "PHARMA": +0.3, "TEXTILES": +0.3},
        "tickers_exposed": ["INFY", "WIPRO", "CIPLA", "DRREDDY", "SUNPHARMA"],
        "thesis": "EU recession risk → IT services export pressure. EUR strength "
                  "boosts pharma exports.",
    },
    {
        "id": "opec", "label": "OPEC HQ Vienna", "lat": 48.2082, "lng": 16.3738,
        "kind": "commodity_hub", "base_severity": 45,
        "keywords": ["opec", "opec+", "saudi", "production cut", "barrels"],
        "sectors": {"ENERGY": +1, "AVIATION": -1, "FMCG": -0.3, "AUTO": -0.3},
        "tickers_exposed": ["ONGC", "OIL", "RELIANCE", "GAIL", "PETRONET"],
        "thesis": "Production cut → ONGC / OIL upstream beat estimates. "
                  "Refiners pinched. Aviation costs spike.",
    },
    {
        "id": "rbi", "label": "RBI Mumbai", "lat": 18.9322, "lng": 72.8347,
        "kind": "central_bank", "base_severity": 55,
        "keywords": ["rbi", "mpc", "shaktikanta", "repo rate", "rbi governor"],
        "sectors": {"REALESTATE": +1, "NBFC": +1, "AUTO": +0.5, "BFSI": -0.3},
        "tickers_exposed": ["DLF", "GODREJPROP", "BAJFINANCE", "CHOLAFIN", "M&M"],
        "thesis": "Domestic rate-set day. Cuts → realty + NBFC rally; banks "
                  "see NIM compression. Hikes → opposite.",
    },
    {
        "id": "boj", "label": "Bank of Japan", "lat": 35.6824, "lng": 139.7585,
        "kind": "central_bank", "base_severity": 35,
        "keywords": ["boj", "yen", "japan", "ueda"],
        "sectors": {"AUTO": -0.3, "METALS": +0.3},
        "tickers_exposed": ["MARUTI", "TATAMOTORS"],
        "thesis": "Yen carry unwind risk. Global liquidity event for EM equities.",
    },
    # ── Tech / supply-chain hubs ─────────────────────────────────────────
    {
        "id": "china_hub", "label": "China supply-chain hub", "lat": 30.5728, "lng": 114.2842,
        "kind": "tech_hub", "base_severity": 45,
        "keywords": ["china", "beijing", "xi jinping", "pboc", "shanghai", "yuan"],
        "sectors": {"PHARMA": +0.5, "METALS": -0.5, "TEXTILES": +0.5,
                    "ELECTRONICS": -0.5, "CHEMICALS": +0.5},
        "tickers_exposed": ["DIVISLAB", "AUROPHARMA", "TATASTEEL", "JSWSTEEL",
                            "DIXON", "PIDILITIND", "ATUL"],
        "thesis": "China+1 narrative for pharma APIs, chemicals, electronics. "
                  "Indian substitute beneficiaries on supply disruption.",
    },
    {
        "id": "uk_london", "label": "London / LME", "lat": 51.5074, "lng": -0.1278,
        "kind": "commodity_hub", "base_severity": 30,
        "keywords": ["lme", "london metal", "uk", "boe"],
        "sectors": {"METALS": +0.5},
        "tickers_exposed": ["VEDL", "HINDCOPPER", "NATIONALUM", "HINDALCO"],
        "thesis": "LME copper/aluminium settlement venue. Metal price discovery.",
    },
    # ── Indian domestic catalysts (anchor visibility) ────────────────────
    {
        "id": "delhi_parliament", "label": "Indian Parliament / PMO", "lat": 28.6139, "lng": 77.2090,
        "kind": "political", "base_severity": 45,
        "keywords": ["lok sabha", "rajya sabha", "modi", "parliament", "pmo",
                     "budget", "policy"],
        "sectors": {"DEFENCE": +0.5, "INFRA": +1, "PSU": +0.5},
        "tickers_exposed": ["LT", "HAL", "BEL", "BHEL", "IRFC", "RVNL"],
        "thesis": "Budget / PLI scheme / cabinet decisions. Defence + PSU + "
                  "infra rotate on policy headlines.",
    },
]


# ─── Flow lanes ────────────────────────────────────────────────────────────
# Each lane is a polyline with status + Indian sector impact.

SEED_FLOWS: List[Dict] = [
    {
        "id": "asia_eu_via_suez",
        "label": "Asia → Europe (Suez)",
        "kind": "shipping",
        "points": [[1.29, 103.85], [12.58, 43.33], [30.58, 32.27], [36.0, 14.5], [51.5, -0.13]],
        "status_normal": True,
        "flow_volume_mbpd": 9.0,
        "indian_impact": "Container shipping, IT exports, FMCG imports",
    },
    {
        "id": "asia_eu_via_cape",
        "label": "Asia → Europe (Cape diversion)",
        "kind": "shipping",
        "points": [[1.29, 103.85], [-34.36, 18.47], [12.58, -28.0], [51.5, -0.13]],
        "status_normal": True,
        "active_when": "Red Sea disrupted",
        "indian_impact": "+10-15d transit, container surcharges spike",
    },
    {
        "id": "hormuz_oil",
        "label": "Hormuz oil export lane",
        "kind": "oil",
        "points": [[25.0, 52.0], [26.57, 56.25], [22.0, 60.0], [15.0, 65.0], [18.93, 72.83]],
        "status_normal": True,
        "flow_volume_mbpd": 20.5,
        "indian_impact": "60% of Indian crude imports route through here",
    },
    {
        "id": "russia_oil_india",
        "label": "Russia → India crude (discounted Urals)",
        "kind": "oil",
        "points": [[68.0, 38.0], [55.0, 50.0], [40.0, 60.0], [25.0, 68.0], [18.93, 72.83]],
        "status_normal": True,
        "flow_volume_mbpd": 1.8,
        "indian_impact": "RIL / IOC discounted-barrel margin boost",
    },
    {
        "id": "ldn_lng",
        "label": "Qatar LNG → India",
        "kind": "lng",
        "points": [[25.28, 51.53], [26.57, 56.25], [22.0, 60.0], [18.0, 65.0], [22.30, 70.0]],
        "status_normal": True,
        "indian_impact": "Petronet, GAIL, GSPL gas grid feeders",
    },
]


# ─── Helpers ───────────────────────────────────────────────────────────────

def _placeholder(db) -> str:
    return "%s" if getattr(db, "is_postgres", False) else "?"


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _augment_hotspot(db, spot: Dict) -> Dict:
    """Add live event count + dynamic severity overlay from the events table."""
    out = dict(spot)
    out["dynamic_signals"] = []
    out["live_alpha_max"] = 0
    out["live_count"] = 0
    if db is None or not getattr(db, "conn", None):
        out["effective_severity"] = spot.get("base_severity", 30)
        return out

    kws = spot.get("keywords") or []
    if not kws:
        out["effective_severity"] = spot.get("base_severity", 30)
        return out
    p = _placeholder(db)
    # Match any keyword in title or summary, last 14d
    like_clauses = " OR ".join(
        ["(LOWER(title) LIKE {p} OR LOWER(summary) LIKE {p})".format(p=p)] * len(kws)
    )
    params: List = []
    for kw in kws:
        kw_l = f"%{kw.lower()}%"
        params.append(kw_l); params.append(kw_l)
    sql = f"""SELECT title, source, published_at, impact_score
              FROM events
             WHERE ({like_clauses})
               AND COALESCE(published_at, created_at) >= datetime('now', '-14 days')
          ORDER BY COALESCE(published_at, created_at) DESC LIMIT 8"""
    if getattr(db, "is_postgres", False):
        sql = sql.replace("datetime('now', '-14 days')", "NOW() - INTERVAL '14 days'")
    try:
        cur = db.conn.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
    except Exception as e:
        logger.debug("hotspot events query failed: %s", e)
        rows = []

    live_alphas = []
    for r in rows:
        try:
            if isinstance(r, dict):
                title = r.get("title"); src = r.get("source")
                pub = r.get("published_at"); imp = r.get("impact_score")
            else:
                title, src, pub, imp = r[0], r[1], r[2], r[3]
            out["dynamic_signals"].append({
                "title": title, "source": src,
                "published_at": str(pub) if pub else None,
                "impact_score": float(imp or 0),
            })
            live_alphas.append(float(imp or 0))
        except Exception:
            continue
    out["live_count"] = len(out["dynamic_signals"])
    out["live_alpha_max"] = max(live_alphas) if live_alphas else 0
    # Dynamic severity = base + bonus for live activity
    base = spot.get("base_severity", 30)
    overlay = min(40, out["live_count"] * 5 + (out["live_alpha_max"] / 100) * 20)
    out["effective_severity"] = round(min(100, base + overlay))
    # Severity tier label for UI colouring
    sev = out["effective_severity"]
    out["severity_tier"] = (
        "critical" if sev >= 80 else
        "high"     if sev >= 60 else
        "medium"   if sev >= 40 else
        "low"
    )
    return out


# ─── Endpoints ─────────────────────────────────────────────────────────────

@bp.route("/api/geo/map/hotspots", methods=["GET"])
def geo_hotspots():
    """Return seeded hotspots, augmented with live event counts + severity."""
    db = _get_db() if _get_db else None
    augmented = [_augment_hotspot(db, s) for s in SEED_HOTSPOTS]
    augmented.sort(key=lambda x: x.get("effective_severity", 0), reverse=True)
    return jsonify({"success": True, "count": len(augmented),
                    "as_of": _now_iso(), "data": augmented})


@bp.route("/api/geo/map/flows", methods=["GET"])
def geo_flows():
    """Return commodity flow lanes (oil, LNG, shipping)."""
    # In v2 each lane gets a live status from hotspot proximity. For now
    # status_normal=True until any hotspot near a lane endpoint flips it.
    db = _get_db() if _get_db else None
    flows: List[Dict] = []
    # Build a quick severity index by region keyword for lane status
    if db is not None and getattr(db, "conn", None):
        for h in SEED_HOTSPOTS:
            aug = _augment_hotspot(db, h)
            sev = aug.get("effective_severity", 0)
            for f in SEED_FLOWS:
                if h.get("id", "") in f.get("id", "") and sev >= 65:
                    f["status_normal"] = False
                    f["disruption_label"] = f"{aug['label']} severity {sev}"
    flows = [dict(f) for f in SEED_FLOWS]
    return jsonify({"success": True, "count": len(flows), "data": flows})


@bp.route("/api/geo/map/exposure/<region_id>", methods=["GET"])
def geo_exposure(region_id: str):
    """For a given hotspot id, return alpha-graded exposed tickers + sectors."""
    spot = next((s for s in SEED_HOTSPOTS if s["id"] == region_id), None)
    if not spot:
        return jsonify({"success": False, "error": "unknown region"}), 404

    db = _get_db() if _get_db else None
    tickers = spot.get("tickers_exposed") or []
    sectors = spot.get("sectors") or {}

    enriched: List[Dict] = []
    if db is not None and getattr(db, "conn", None) and tickers:
        p = _placeholder(db)
        placeholders = ",".join([p] * len(tickers))
        try:
            cur = db.conn.cursor()
            cur.execute(
                f"""SELECT ticker, MAX(alpha_score) AS top_alpha,
                          MAX(sentiment) AS sentiment, MAX(headline) AS headline
                     FROM signals
                    WHERE ticker IN ({placeholders})
                      AND status = 'active'
                 GROUP BY ticker""",
                tuple(tickers),
            )
            rows = cur.fetchall() or []
            for r in rows:
                if isinstance(r, dict):
                    tk = r.get("ticker"); a = r.get("top_alpha")
                    se = r.get("sentiment"); hd = r.get("headline")
                else:
                    tk, a, se, hd = r[0], r[1], r[2], r[3]
                enriched.append({
                    "ticker": tk, "alpha": round(float(a or 0), 1),
                    "sentiment": se, "headline": hd,
                })
        except Exception as e:
            logger.debug("exposure query failed: %s", e)
    # Backfill tickers without active signals (so the full list shows up)
    seen = {e["ticker"] for e in enriched}
    for tk in tickers:
        if tk not in seen:
            enriched.append({"ticker": tk, "alpha": 0, "sentiment": None, "headline": None})
    enriched.sort(key=lambda x: x.get("alpha", 0) or 0, reverse=True)

    return jsonify({
        "success": True,
        "region_id": region_id,
        "region_label": spot.get("label"),
        "thesis": spot.get("thesis"),
        "sectors": [{"sector": k, "direction": v} for k, v in sectors.items()],
        "tickers": enriched,
        "as_of": _now_iso(),
    })
