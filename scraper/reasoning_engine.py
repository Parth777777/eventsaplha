"""
Reasoning engine — produces a defensible "why this pick" chain for every signal.

Every output claim is grounded in a specific data point the system already
computed: event_type prior, alpha-bucket empirical hit rate, sector move,
regime, volume confirmation, forensics score, promoter trend.

The reasoning dict has 6 structured parts:
  - primary_driver     : the single most important reason this signal exists
  - why_ticker         : why THIS ticker was picked (direct / sector / peer)
  - why_direction      : why bullish/bearish (news sentiment + regime + priors)
  - why_magnitude      : why the predicted return size is what it is
  - why_confidence     : what gives / reduces confidence in this call
  - risk_factors       : what could make this signal wrong
  - bullets            : 4-6 human-readable sentences (for UI rendering)

No hand-waving: if a component of the reasoning isn't available (e.g. no
calibration yet), we say so explicitly rather than making it up.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Patterns for pulling concrete numbers out of a headline so the reasoning
# anchors on real figures instead of generic templates.
_FACT_PATTERNS = [
    ("rupee_cr",   re.compile(r"(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d+)?)\s*(?:Cr|crore|Cr\.)\b", re.I)),
    ("rupee_lakh", re.compile(r"(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d+)?)\s*(?:lakh|lac)\b", re.I)),
    ("usd_mn",     re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:mn|million|m)\b", re.I)),
    ("usd_bn",     re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:bn|billion|b)\b", re.I)),
    ("percent",    re.compile(r"(?<![A-Za-z])([\-+]?\d+(?:\.\d+)?)\s*%")),
    ("bps",        re.compile(r"(\d+(?:\.\d+)?)\s*(?:bps|basis points)", re.I)),
]


def _extract_facts(headline: str) -> List[str]:
    """Pull concrete figures from a headline so reasoning can quote them.
    Returns up to 3 short fact strings; empty list if none found.
    """
    facts: List[str] = []
    if not headline:
        return facts
    seen = set()
    for kind, pat in _FACT_PATTERNS:
        for m in pat.finditer(headline):
            key = (kind, m.group(0).lower())
            if key in seen:
                continue
            seen.add(key)
            val = m.group(0).strip()
            facts.append(val)
            if len(facts) >= 3:
                return facts
    return facts


def _headline_gist(headline: str, max_chars: int = 110) -> str:
    """Trim headline to a single clean clause for embedding in reasoning."""
    if not headline:
        return ""
    h = headline.strip()
    # Cut at first sentence/punctuation boundary if early enough
    for sep in [". ", " — ", " – ", " - ", "; "]:
        idx = h.find(sep)
        if 30 <= idx <= max_chars:
            return h[:idx].rstrip()
    if len(h) <= max_chars:
        return h
    return h[: max_chars - 1].rstrip() + "…"


# Well-established "why-this-sector-moves-on-this-event" rules.
# Each rule is: event_type -> [list of plausible narratives].
EVENT_TYPE_NARRATIVES = {
    "earnings": {
        "direct": "earnings result is a direct, measurable fundamental driver",
        "bullish": "beat on revenue/EBITDA typically re-rates the stock upward on re-rating",
        "bearish": "miss on revenue/EBITDA typically de-rates, especially if guidance was reset",
        "regime_weight": {"bull_strong": 1.1, "bear_strong": 0.9, "crisis": 0.7, "sideways_calm": 1.0},
    },
    "merger": {
        "direct": "M&A directly changes the capital structure and strategic position",
        "bullish": "acquirer may re-rate on scale; target typically rallies to deal price",
        "bearish": "overpayment or dilution can hurt acquirer; regulatory risk can hit both sides",
        "regime_weight": {"bull_strong": 1.0, "bear_strong": 0.9, "crisis": 0.6, "sideways_calm": 1.0},
    },
    "policy": {
        "direct": "policy changes alter the operating environment for whole sectors",
        "bullish": "friendly rule changes (lower tax / liberalisation) expand TAM and margins",
        "bearish": "tightening (higher tax / tighter rules) compresses margins or growth",
        "regime_weight": {"bull_strong": 1.0, "bear_strong": 1.1, "crisis": 1.2, "sideways_calm": 0.9},
    },
    "order_win": {
        "direct": "new order adds directly to the order book — a forward-visible revenue stream",
        "bullish": "large order visibility improves revenue trajectory and working-capital cycle",
        "bearish": "rare — maybe if order comes with onerous terms or crowds out higher-margin work",
        "regime_weight": {"bull_strong": 1.0, "bear_strong": 0.8, "crisis": 0.7, "sideways_calm": 1.0},
    },
    "supply": {
        "direct": "supply chain event changes input costs or capacity",
        "bullish": "cheaper inputs / capacity expansion directly lifts margins",
        "bearish": "shortage / plant shutdown / export ban directly hits revenue and margins",
        "regime_weight": {"bull_strong": 0.9, "bear_strong": 1.0, "crisis": 1.1, "sideways_calm": 1.0},
    },
    "insider": {
        "direct": "insider activity reveals information from people with material non-public knowledge",
        "bullish": "promoter buying near support is a high-conviction accumulation signal",
        "bearish": "promoter selling / pledge rising ahead of news is a textbook distribution tell",
        "regime_weight": {"bull_strong": 1.0, "bear_strong": 1.2, "crisis": 1.3, "sideways_calm": 1.0},
    },
    "dividend": {
        "direct": "dividend changes signal management's capital-allocation stance",
        "bullish": "raised payout / buyback announcement signals cash strength and floor under price",
        "bearish": "dividend cut signals cash stress and typically triggers multi-day derating",
        "regime_weight": {"bull_strong": 0.9, "bear_strong": 1.0, "crisis": 1.1, "sideways_calm": 1.0},
    },
    "news": {
        "direct": "general news is a soft driver — needs corroboration by flows, volume, or filings",
        "bullish": "positive story can kick off momentum but usually needs confirming volume",
        "bearish": "negative story can anchor sentiment but must be checked for false-flag manipulation",
        "regime_weight": {"bull_strong": 1.0, "bear_strong": 1.0, "crisis": 0.9, "sideways_calm": 1.0},
    },
}


# Known sector-to-macro-theme tether (drawn from MACRO_SECTOR_MAP)
SECTOR_RATIONALE = {
    "IT": "USD-billed revenue; a weaker rupee boosts INR earnings",
    "BFSI": "earnings tied to loan growth + NIM; sensitive to rate moves",
    "ENERGY": "commodity-driven — crude & coal prices flow straight to margin",
    "METALS": "cyclical — demand is tied to China / global construction cycle",
    "AUTO": "discretionary; sensitive to fuel prices, rates, and input costs (steel)",
    "PHARMA": "USD export revenue + US FDA risk; defensive in bear regimes",
    "FMCG": "defensive + rural demand-driven; inflation hurts margins",
    "INFRA": "order-book-driven; government capex is the main lever",
    "TELECOM": "ARPU + regulatory-driven; spectrum auctions matter",
}


def _bucket_for(alpha: float) -> str:
    if alpha >= 80:
        return "80+"
    if alpha >= 65:
        return "65-80"
    if alpha >= 50:
        return "50-65"
    return "<50"


def _load_event_prior(db, event_type: str, horizon: str = "3D") -> Optional[Dict[str, Any]]:
    try:
        from calibration import load

        row = load(db, "event_priors", horizon)
        if not row:
            return None
        priors = row.get("params") or {}
        return priors.get(event_type)
    except Exception:
        return None


def _load_bucket_rate(db, alpha: float, horizon: str = "3D") -> Optional[float]:
    try:
        from calibration import load

        row = load(db, "alpha_buckets", horizon)
        if not row:
            return None
        bucket = _bucket_for(alpha)
        for b in (row.get("params") or {}).get("buckets", []):
            if b.get("bucket") == bucket:
                return float(b.get("hit_rate") or 0)
    except Exception:
        return None
    return None


def _get_sector(ticker: str) -> Optional[str]:
    try:
        from config import STOCK_SECTORS

        return STOCK_SECTORS.get((ticker or "").upper())
    except Exception:
        return None


def _recent_ticker_promoter_flags(db, ticker: str) -> List[str]:
    flags: List[str] = []
    try:
        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM sebi_disclosures WHERE ticker = ? AND transaction_type = 'sell'"
            if not getattr(db, "is_postgres", False)
            else "SELECT COUNT(*) FROM sebi_disclosures WHERE ticker = %s AND transaction_type = 'sell'",
            (ticker.upper(),),
        )
        row = cursor.fetchone()
        if row and int(row[0] if not isinstance(row, dict) else list(row.values())[0]) > 0:
            flags.append("recent_insider_sell")
        cursor.execute(
            "SELECT promoter_pledge_pct FROM promoter_holdings WHERE ticker = ? ORDER BY quarter_end DESC LIMIT 1"
            if not getattr(db, "is_postgres", False)
            else "SELECT promoter_pledge_pct FROM promoter_holdings WHERE ticker = %s ORDER BY quarter_end DESC LIMIT 1",
            (ticker.upper(),),
        )
        row = cursor.fetchone()
        if row:
            pledge = float(row[0] if not isinstance(row, dict) else list(row.values())[0] or 0)
            if pledge >= 25:
                flags.append(f"high_pledge_{pledge:.0f}pct")
            elif pledge >= 10:
                flags.append(f"pledge_{pledge:.0f}pct")
    except Exception:
        pass
    return flags


def explain(db, signal: Dict, event: Optional[Dict] = None) -> Dict[str, Any]:
    """Produce the full structured reasoning for a signal.

    `signal` is the enriched signal dict (intent, manipulation_score,
    volume fields, regime). `event` is the underlying event if available
    (for direct companies list, source, published_at).
    """
    ticker = (signal.get("ticker") or "").upper()
    event_type = (signal.get("event_type") or "news").lower()
    sentiment = (signal.get("sentiment") or "neutral").lower()
    regime = (signal.get("regime") or "").lower()
    alpha = float(signal.get("alpha_score") or 0)
    magnitude = float(signal.get("magnitude") or 0)
    headline = signal.get("headline") or (event or {}).get("title") or ""
    source = signal.get("source") or (event or {}).get("source") or "unknown"
    confidence_nlp = float(signal.get("confidence") or 0.5)

    narrative = EVENT_TYPE_NARRATIVES.get(event_type, EVENT_TYPE_NARRATIVES["news"])
    sector = _get_sector(ticker)
    company_name = signal.get("company") or ticker
    headline_facts = _extract_facts(headline)
    headline_gist = _headline_gist(headline)

    # --- why_ticker: direct mention vs sector-driven vs peer ---
    companies_field = (signal.get("company") or "").lower()
    companies_raw = (event or {}).get("companies", "") or ""
    explicit_mention = ticker in str(companies_raw).upper() or (ticker.lower() in headline.lower())
    if explicit_mention:
        why_ticker = f"'{ticker}' is explicitly named in the article — direct linkage."
        why_ticker_code = "direct"
    elif sector:
        sector_why = SECTOR_RATIONALE.get(sector, f"{sector} sector exposure")
        why_ticker = f"'{ticker}' belongs to the {sector} sector, which this article's macro theme affects. {sector_why}."
        why_ticker_code = "sector"
    else:
        why_ticker = f"'{ticker}' is in the monitored universe and matches keywords in the article."
        why_ticker_code = "keyword"

    # --- why_direction: sentiment + event_type + regime alignment ---
    direction_narr = narrative.get(sentiment, narrative.get("direct"))
    regime_key = regime.split("_")[0] if regime else "sideways_calm"
    regime_weight = narrative.get("regime_weight", {}).get(regime_key, 1.0)
    direction_reasons = [direction_narr]
    if regime_weight > 1.05:
        direction_reasons.append(f"{regime or 'current regime'} amplifies {event_type}-driven moves")
    elif regime_weight < 0.95:
        direction_reasons.append(f"{regime or 'current regime'} dampens the usual {event_type} response")
    why_direction = ". ".join(direction_reasons) + "."

    # --- why_magnitude: historical prior + volatility + regime ---
    horizon = "3D"
    event_prior = _load_event_prior(db, event_type, horizon)
    bucket_rate = _load_bucket_rate(db, alpha, horizon)
    magnitude_reasons: List[str] = []
    if event_prior and event_prior.get("samples", 0) >= 5:
        hp = event_prior.get("hit_rate")
        avg_pnl = event_prior.get("avg_signal_pnl")
        samples = event_prior.get("samples")
        magnitude_reasons.append(
            f"{event_type} events have an empirical hit-rate of {hp:.0%} (n={samples}) in resolved data"
        )
        if avg_pnl is not None:
            magnitude_reasons.append(
                f"average signal PnL on {event_type}: {avg_pnl:+.2f}% over 3 days"
            )
    else:
        magnitude_reasons.append(
            f"{event_type} priors are not yet calibrated on enough resolved predictions — using conservative default"
        )
    if magnitude >= 7:
        magnitude_reasons.append(f"reported magnitude is {magnitude:.0f}/10 (major)")
    elif magnitude >= 5:
        magnitude_reasons.append(f"reported magnitude is {magnitude:.0f}/10 (moderate)")
    else:
        magnitude_reasons.append(f"reported magnitude is {magnitude:.0f}/10 (minor — size the move small)")
    if headline_facts:
        magnitude_reasons.append(
            "headline figures: " + ", ".join(headline_facts)
        )
    why_magnitude = ". ".join(magnitude_reasons) + "."

    # --- why_confidence ---
    conf_reasons: List[str] = []
    conf_reasons.append(f"NLP sentiment confidence: {confidence_nlp:.0%}")
    if bucket_rate is not None:
        conf_reasons.append(
            f"alpha bucket {_bucket_for(alpha)} has empirical hit-rate {bucket_rate:.0%}"
        )
    manip = signal.get("manipulation_score")
    if manip is not None:
        conf_reasons.append(f"manipulation score: {manip}/100 (higher = less trustworthy)")
    volume_flag = signal.get("volume_confirmation")
    obv_div = signal.get("obv_divergence_flag")
    if volume_flag:
        conf_reasons.append("volume is confirming the move (OBV agrees)")
    elif obv_div:
        conf_reasons.append(f"volume analysis shows {obv_div} divergence — volume disagrees with price")
    intent = signal.get("intent")
    if intent in {"promote", "hype", "dump_setup"}:
        conf_reasons.append(f"article intent classified as '{intent}' — treat narrative with caution")
    why_confidence = ". ".join(conf_reasons) + "."

    # --- risk factors ---
    risks: List[str] = []
    promoter_flags = _recent_ticker_promoter_flags(db, ticker)
    for f in promoter_flags:
        if f.startswith("high_pledge"):
            risks.append(f"high promoter pledge ({f.split('_')[-1]}) — forced-sale risk on drawdowns")
        elif f == "recent_insider_sell":
            risks.append("recent insider sell disclosures — insiders are distributing")
        elif f.startswith("pledge_"):
            risks.append(f"moderate promoter pledge ({f.split('_')[-1]}) — worth watching")
    if manip and manip >= 40:
        risks.append("forensic signals elevated — headline may be manipulated")
    if intent in {"dump_setup", "hype"}:
        risks.append(f"article tone is '{intent}'-like — independently verify numbers")
    if magnitude < 4:
        risks.append("low event magnitude — noise risk higher than signal")
    if regime_weight < 1.0:
        risks.append("current market regime historically dampens this event type")
    if not risks:
        risks.append("no specific red flags identified beyond normal market risk")

    # --- primary driver — the one-sentence headline ---
    bucket_label = _bucket_for(alpha)
    fact_str = f" [{', '.join(headline_facts)}]" if headline_facts else ""
    gist_clause = f" — \"{headline_gist}\"" if headline_gist else ""
    if explicit_mention:
        primary = (
            f"{event_type.replace('_', ' ')} ({sentiment}) directly naming "
            f"{company_name} ({ticker}); alpha {alpha:.0f} ({bucket_label}){fact_str}{gist_clause}"
        )
    elif sector:
        primary = (
            f"{event_type.replace('_', ' ')} ({sentiment}) hits {sector} sector → "
            f"{company_name} ({ticker}); alpha {alpha:.0f} ({bucket_label}){fact_str}{gist_clause}"
        )
    else:
        primary = (
            f"{event_type.replace('_', ' ')} ({sentiment}) — indirect linkage to "
            f"{company_name} ({ticker}); alpha {alpha:.0f} ({bucket_label}){fact_str}{gist_clause}"
        )

    # --- bullets for UI ---
    bullets = [
        f"Ticker: {why_ticker}",
        f"Direction ({sentiment}): {why_direction}",
        f"Magnitude: {why_magnitude}",
        f"Confidence: {why_confidence}",
        "Risks: " + "; ".join(risks),
    ]
    if source and source != "unknown":
        bullets.append(
            f"Source: {source}"
            + (f" (credibility applied to confidence)" if signal.get("source_credibility") else "")
        )

    return {
        "primary_driver": primary,
        "why_ticker": why_ticker,
        "why_ticker_code": why_ticker_code,
        "why_direction": why_direction,
        "why_magnitude": why_magnitude,
        "why_confidence": why_confidence,
        "risk_factors": risks,
        "bullets": bullets,
        "headline_facts": headline_facts,
        "headline_gist": headline_gist,
        "meta": {
            "event_type": event_type,
            "sentiment": sentiment,
            "regime": regime,
            "sector": sector,
            "alpha": alpha,
            "magnitude": magnitude,
            "empirical_event_hit_rate": event_prior.get("hit_rate") if event_prior else None,
            "event_prior_samples": event_prior.get("samples") if event_prior else 0,
            "bucket_rate": bucket_rate,
        },
    }
