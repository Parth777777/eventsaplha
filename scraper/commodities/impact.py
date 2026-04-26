"""
Commodity + war → Indian sector impact composition.

Pulls today's commodity moves from yfinance, joins with active macro events
tagged by the existing MACRO_SECTOR_MAP, and returns a per-sector impact card.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

try:
    import yfinance as yf  # type: ignore

    YF_OK = True
except Exception:
    yf = None
    YF_OK = False

from metrics import inc, observe
import config as scraper_config

logger = logging.getLogger(__name__)

# yfinance tickers for commodities we care about
COMMODITY_TICKERS = {
    "brent_crude": "BZ=F",
    "wti_crude": "CL=F",
    "gold": "GC=F",
    "silver": "SI=F",
    "natgas": "NG=F",
    "copper": "HG=F",
    "aluminium": "ALI=F",
    "wheat": "ZW=F",
}

# Sector sensitivity coefficients (1% commodity move → X% sector move).
# Rough coefficients from historical regressions; refine quarterly offline.
SECTOR_SENSITIVITY = {
    "brent_crude": {"ENERGY": 0.55, "AUTO": -0.30, "AVIATION": -0.45},
    "gold": {"METALS": 0.25, "BFSI": -0.10},
    "copper": {"METALS": 0.45, "INFRA": 0.20},
    "natgas": {"ENERGY": 0.35, "FMCG": -0.10},
    "wheat": {"FMCG": -0.25},
}


def _latest_pct_change(ticker: str) -> Optional[Dict]:
    if not YF_OK:
        return None
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d", interval="1d")
        if hist is None or hist.empty or len(hist) < 2:
            return None
        close_today = float(hist["Close"].iloc[-1])
        close_prev = float(hist["Close"].iloc[-2])
        pct = (close_today - close_prev) / close_prev * 100.0
        return {"close": round(close_today, 4), "pct_change": round(pct, 3),
                "asof": hist.index[-1].isoformat()}
    except Exception as exc:
        logger.debug("commodity fetch failed ticker=%s err=%s", ticker, exc)
        return None


def snapshot_commodities() -> Dict[str, Dict]:
    """Return current state for all tracked commodities."""
    out: Dict[str, Dict] = {}
    for name, yt in COMMODITY_TICKERS.items():
        row = _latest_pct_change(yt)
        if row is not None:
            out[name] = {"yf_ticker": yt, **row}
            observe("commodity_pct_change", abs(row["pct_change"]), commodity=name)
    inc("commodity_snapshots_total")
    return out


def _active_macro_themes(db, lookback_hours: int = 24) -> List[Dict]:
    """Return distinct macro themes tagged in recent events.

    We key off the existing `events` table — when the scraper tags a macro
    article it stores summary + companies fields. Themes are inferred from
    keywords defined in MACRO_SECTOR_MAP.
    """
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        cursor = db.conn.cursor()
        if db.is_postgres:
            cursor.execute(
                "SELECT title, summary FROM events WHERE created_at >= %s ORDER BY created_at DESC LIMIT 500",
                (since,),
            )
        else:
            cursor.execute(
                f"SELECT title, summary FROM events WHERE created_at >= {p} ORDER BY created_at DESC LIMIT 500",
                (since.isoformat(),),
            )
        rows = cursor.fetchall()
    except Exception as exc:
        logger.debug("macro theme query failed: %s", exc)
        return []

    themes_found: Dict[str, Dict] = {}
    macro_map = getattr(scraper_config, "MACRO_SECTOR_MAP", {})
    for r in rows:
        text = (r[0] if not isinstance(r, dict) else r.get("title") or "") + " " + \
               (r[1] if not isinstance(r, dict) else r.get("summary") or "")
        text_low = text.lower()
        for theme, meta in macro_map.items():
            keywords = meta.get("keywords", [])
            if any(kw in text_low for kw in keywords):
                slot = themes_found.setdefault(theme, {
                    "theme": theme,
                    "affected_sectors": meta.get("sectors", {}),
                    "magnitude_boost": meta.get("magnitude_boost", 1.0),
                    "example_headlines": [],
                })
                headline = r[0] if not isinstance(r, dict) else r.get("title", "")
                if len(slot["example_headlines"]) < 3:
                    slot["example_headlines"].append(headline)
    return list(themes_found.values())


def compose_impact(db) -> Dict:
    """Build the full commodities-impact payload used by /api/commodities/impact."""
    commodities = snapshot_commodities()
    macro = _active_macro_themes(db)

    # Sector-level expected impact = Σ (commodity pct × sensitivity coefficient)
    sector_impact: Dict[str, float] = {}
    for cname, data in commodities.items():
        pct = data.get("pct_change", 0.0)
        coef_map = SECTOR_SENSITIVITY.get(cname, {})
        for sector, coef in coef_map.items():
            sector_impact[sector] = sector_impact.get(sector, 0.0) + pct * coef

    # Layer in macro-theme direction: each theme contributes +/- 0.5 per affected sector
    for theme in macro:
        for sector, direction in theme.get("affected_sectors", {}).items():
            signed = 0.5 if direction == "bullish" else (-0.5 if direction == "bearish" else 0)
            sector_impact[sector] = sector_impact.get(sector, 0.0) + signed * theme.get("magnitude_boost", 1.0)

    # Normalize to % swing estimate
    ranked = sorted(sector_impact.items(), key=lambda kv: -abs(kv[1]))

    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "commodities": commodities,
        "macro_themes": macro,
        "sector_impact": [{"sector": s, "expected_pct": round(v, 3),
                           "direction": "bullish" if v > 0 else "bearish" if v < 0 else "neutral"}
                          for s, v in ranked],
    }
