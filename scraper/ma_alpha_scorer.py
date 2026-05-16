"""
Tickwave - M&A Alpha Scoring Engine

Computes 0-100 M&A Alpha Score from 6 weighted factors:
  1. Deal size relative to target market cap   (25%)
  2. Sector synergy (acquirer vs target)       (20%)
  3. Acquirer track record (post-deal returns) (15%)
  4. Target valuation premium / discount       (20%)
  5. Regulatory risk (CCI scrutiny, foreign)   (10%)
  6. News / social momentum                    (10%)

Output: float in [0, 100]. Each factor contribution captured in `factors_json`
on the deal row for UI transparency.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


WEIGHTS = {
    "size":       0.25,
    "synergy":    0.20,
    "track":      0.15,
    "valuation":  0.20,
    "regulatory": 0.10,
    "momentum":   0.10,
}


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def size_score(deal_value_cr: Optional[float]) -> Tuple[float, str]:
    """Larger deal value → higher score, log scaled.
       100 cr → ~10, 1000 cr → ~40, 10000 cr → ~70, 50000 cr → ~95.
    """
    if not deal_value_cr or deal_value_cr <= 0:
        return 30.0, "no deal value disclosed (low default)"
    score = math.log10(deal_value_cr + 1) / math.log10(50001) * 100
    return _clip(score), f"₹{deal_value_cr:,.0f} cr"


def synergy_score(sector_acquirer: Optional[str], sector_target: Optional[str], deal_type: str) -> Tuple[float, str]:
    """Same sector or vertical-integration adjacent = high synergy.
       Diversification (different sectors) = lower synergy + higher integration risk.
       Schemes/mergers within group score higher than acquisitions across sectors.
    """
    if not sector_acquirer or not sector_target:
        return 50.0, "sector data missing (neutral)"
    if sector_acquirer.strip().lower() == sector_target.strip().lower():
        base = 80
        why = f"same sector ({sector_acquirer})"
    else:
        # Loose adjacency map (could be replaced by a richer ontology later)
        adjacencies = {
            "banking": {"financial services", "nbfc", "insurance"},
            "auto": {"auto components", "tyres", "logistics"},
            "it": {"telecom", "digital media", "fintech"},
            "pharma": {"healthcare", "biotech", "diagnostics"},
            "consumer goods": {"retail", "food processing", "fmcg"},
        }
        a = sector_acquirer.lower()
        t = sector_target.lower()
        adjacent = any(t in v or a in v for v in adjacencies.values()) or \
                   t in adjacencies.get(a, set()) or a in adjacencies.get(t, set())
        base = 60 if adjacent else 35
        why = f"{sector_acquirer} → {sector_target} ({'adjacent' if adjacent else 'cross-sector'})"
    if deal_type in ("scheme", "merger") and base >= 60:
        base = min(100, base + 5)
    return _clip(base), why


def track_record_score(db, acquirer_name: Optional[str]) -> Tuple[float, str]:
    """Score the acquirer based on count + average outcome of prior deals.
    For now, reward serial acquirers (more experience) and penalize a track
    of withdrawn deals. Returns a neutral score if no history.
    """
    if not acquirer_name:
        return 50.0, "no acquirer (neutral)"
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT status, COUNT(*) AS n FROM ma_deals
               WHERE LOWER(acquirer_name) = LOWER(?) GROUP BY status""",
            (acquirer_name,),
        )
        rows = cur.fetchall()
    except Exception:
        return 50.0, "history unavailable (neutral)"
    if not rows:
        return 50.0, "first observed deal for this acquirer"
    counts = {r[0] if not hasattr(r, "keys") else r["status"]:
              r[1] if not hasattr(r, "keys") else r["n"] for r in rows}
    completed = counts.get("effective", 0)
    withdrawn = counts.get("withdrawn", 0)
    total = sum(counts.values())
    if total == 0:
        return 50.0, "no track record"
    completion_rate = completed / max(1, total)
    base = 40 + completion_rate * 50
    base -= min(20, withdrawn * 5)  # each withdrawn deal -5, capped
    return _clip(base), f"{total} prior deals, {completed} completed, {withdrawn} withdrawn"


def valuation_score(deal_value_cr: Optional[float], stake_pct: Optional[float]) -> Tuple[float, str]:
    """Implied target enterprise value vs deal value tells us premium.
    Without target price/marketcap data we approximate via stake size: deals
    that bid for a high stake (>=50%) at a notable value get more weight.
    """
    if not deal_value_cr or not stake_pct:
        return 50.0, "valuation inputs missing (neutral)"
    # Implied 100% value
    implied_full = deal_value_cr / max(1.0, stake_pct / 100.0)
    # Score: anchor at 5000cr fair value (median Indian M&A target)
    if implied_full < 500:
        score = 30
    elif implied_full < 5000:
        score = 50
    elif implied_full < 25000:
        score = 70
    else:
        score = 85
    return _clip(score), f"implied 100% value ≈ ₹{implied_full:,.0f} cr ({stake_pct}% stake)"


def regulatory_risk_score(deal_type: str, deal_value_cr: Optional[float],
                          payload: Dict) -> Tuple[float, str]:
    """Higher score = lower regulatory risk. Big cross-sector or foreign deals
    attract CCI/FDI scrutiny.
    """
    text = (payload.get("text") or "").lower()
    risk = 0
    if deal_value_cr and deal_value_cr > 5000:
        risk += 20
    if deal_type == "scheme":
        risk += 15  # NCLT scheme mandatory
    if any(w in text for w in ("foreign", "fdi", "global", "overseas")):
        risk += 15
    if "cci" in text or "competition commission" in text:
        risk += 10
    score = max(0, 100 - risk)
    return _clip(score), f"regulatory risk = {risk} → score {score}"


def momentum_score(db, deal_id: str, ann_date: Optional[str]) -> Tuple[float, str]:
    """Use deal-event count + age as a momentum proxy.
    Recent deal with multiple events → momentum; old deal with one event → cold.
    """
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ma_deal_events WHERE deal_id = ?", (deal_id,))
        n_events = cur.fetchone()[0] or 0
    except Exception:
        n_events = 0
    age_days = 0
    try:
        if ann_date:
            ad = datetime.strptime(ann_date[:10], "%Y-%m-%d").date()
            age_days = (date.today() - ad).days
    except Exception:
        pass
    # Multi-event recent deal scores high; single-event old deal scores low
    age_penalty = min(40, max(0, age_days - 7) // 3)  # >7d: -1 every 3 days
    score = 40 + n_events * 10 - age_penalty
    return _clip(score), f"{n_events} events · {age_days}d old"


def score_deal(db, deal: Dict) -> Tuple[float, Dict]:
    """Score a single deal dict (as returned by ma_scraper.deal_by_id or list_deals)."""
    payload = deal.get("payload") or {}
    if isinstance(deal.get("payload_json"), str):
        try:
            payload = json.loads(deal["payload_json"]) or {}
        except Exception:
            pass
    s_size, why_size = size_score(deal.get("deal_value_cr"))
    s_syn,  why_syn  = synergy_score(deal.get("sector_acquirer"), deal.get("sector_target"), deal.get("deal_type") or "")
    s_trk,  why_trk  = track_record_score(db, deal.get("acquirer_name"))
    s_val,  why_val  = valuation_score(deal.get("deal_value_cr"), deal.get("stake_pct"))
    s_reg,  why_reg  = regulatory_risk_score(deal.get("deal_type") or "", deal.get("deal_value_cr"), payload)
    s_mom,  why_mom  = momentum_score(db, deal.get("deal_id"), deal.get("announcement_date"))
    total = (
        s_size * WEIGHTS["size"] +
        s_syn  * WEIGHTS["synergy"] +
        s_trk  * WEIGHTS["track"] +
        s_val  * WEIGHTS["valuation"] +
        s_reg  * WEIGHTS["regulatory"] +
        s_mom  * WEIGHTS["momentum"]
    )
    factors = {
        "size":       {"score": round(s_size, 1), "why": why_size},
        "synergy":    {"score": round(s_syn,  1), "why": why_syn},
        "track":      {"score": round(s_trk,  1), "why": why_trk},
        "valuation":  {"score": round(s_val,  1), "why": why_val},
        "regulatory": {"score": round(s_reg,  1), "why": why_reg},
        "momentum":   {"score": round(s_mom,  1), "why": why_mom},
    }
    return round(_clip(total), 1), factors


def score_all_active(db) -> int:
    """Re-score every deal in the last 180 days that's not closed/withdrawn.
    Writes alpha_score back to ma_deals. Returns rows updated.
    """
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT * FROM ma_deals
               WHERE status NOT IN ('effective','withdrawn')
                 AND date(announcement_date) >= date('now', '-180 days')"""
        )
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"score_all_active fetch: {e}")
        return 0
    n = 0
    for d in rows:
        try:
            score, factors = score_deal(db, d)
            new_payload = d.get("payload_json") or "{}"
            try:
                payload = json.loads(new_payload)
            except Exception:
                payload = {}
            payload["_factors"] = factors
            cur.execute(
                "UPDATE ma_deals SET alpha_score = ?, payload_json = ?, updated_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (score, json.dumps(payload, default=str), d["deal_id"]),
            )
            n += 1
        except Exception as e:
            logger.debug(f"score_deal {d.get('deal_id')}: {e}")
            continue
    try:
        db.conn.commit()
    except Exception:
        pass
    return n
