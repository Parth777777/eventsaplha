"""
Composite manipulation score (0-100).

Combines six signals — discrepancy, intent, source credibility, coordinated
campaign, pre-news volume anomaly, promoter selling — into one number with
human-readable reason codes.

Score bands:
   >= 70 — likely_manipulated
  40..69 — unverified
    < 40 — clean
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Weighted contributions (tuned conservatively; recompute after backtest)
WEIGHTS = {
    "discrepancy_flagged": 28,
    "intent_dump_setup": 20,
    "intent_hype": 12,
    "intent_attack": 10,
    "intent_promote": 8,
    "fingerprint_unsourced_numbers": 10,
    "fingerprint_anonymous_insider": 10,
    "fingerprint_contradicts_public_facts": 12,
    "fingerprint_promo_disguised_as_analysis": 8,
    "fingerprint_recycled_phrasing": 6,
    "coordinated_campaign": 15,
    "low_source_credibility": 12,
    "unexplained_volume_pre_news": 14,
    "promoter_selling_recent": 10,
}


def classify_band(score: int) -> str:
    if score >= 70:
        return "likely_manipulated"
    if score >= 40:
        return "unverified"
    return "clean"


def score_event(
    *,
    event_id: str,
    discrepancy: Optional[Dict] = None,
    intent: Optional[Dict] = None,
    source_credibility: float = 0.5,
    coordinated_campaign: bool = False,
    unexplained_volume_pre_news: bool = False,
    promoter_selling_recent: bool = False,
) -> Dict:
    """Compute composite manipulation score and reason list for an event.

    Inputs are optional; unknown inputs contribute nothing.
    """
    reasons: List[str] = []
    score = 0

    if discrepancy and discrepancy.get("flagged"):
        score += WEIGHTS["discrepancy_flagged"]
        reasons.append("filing_number_mismatch")

    if intent:
        intent_label = intent.get("intent")
        if intent_label == "dump_setup":
            score += WEIGHTS["intent_dump_setup"]
            reasons.append("intent_dump_setup")
        elif intent_label == "hype":
            score += WEIGHTS["intent_hype"]
            reasons.append("intent_hype")
        elif intent_label == "attack":
            score += WEIGHTS["intent_attack"]
            reasons.append("intent_attack")
        elif intent_label == "promote":
            score += WEIGHTS["intent_promote"]
            reasons.append("intent_promote")
        for flag in intent.get("fingerprint_flags", []) or []:
            key = f"fingerprint_{flag}"
            if key in WEIGHTS:
                score += WEIGHTS[key]
                reasons.append(flag)

    if coordinated_campaign:
        score += WEIGHTS["coordinated_campaign"]
        reasons.append("coordinated_campaign")

    if source_credibility < 0.35:
        score += WEIGHTS["low_source_credibility"]
        reasons.append("low_source_credibility")

    if unexplained_volume_pre_news:
        score += WEIGHTS["unexplained_volume_pre_news"]
        reasons.append("pre_news_volume")

    if promoter_selling_recent:
        score += WEIGHTS["promoter_selling_recent"]
        reasons.append("promoter_selling")

    score = min(100, max(0, score))
    band = classify_band(score)
    return {
        "event_id": event_id,
        "score": score,
        "band": band,
        "reasons": reasons,
    }


def persist(db, result: Dict) -> None:
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    try:
        if db.is_postgres:
            db.conn.cursor().execute(
                """INSERT INTO manipulation_scores (event_id, score, band, reasons)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (event_id) DO UPDATE SET
                     score = EXCLUDED.score, band = EXCLUDED.band,
                     reasons = EXCLUDED.reasons, computed_at = CURRENT_TIMESTAMP""",
                (result["event_id"], result["score"], result["band"],
                 json.dumps(result["reasons"])),
            )
        else:
            db.conn.execute(
                f"""INSERT OR REPLACE INTO manipulation_scores
                     (event_id, score, band, reasons, computed_at)
                     VALUES ({p}, {p}, {p}, {p}, CURRENT_TIMESTAMP)""",
                (result["event_id"], result["score"], result["band"],
                 json.dumps(result["reasons"])),
            )
        # also propagate to signals row
        if db.is_postgres:
            db.conn.cursor().execute(
                """UPDATE signals SET manipulation_score = %s,
                        article_discrepancy_flag = %s
                   WHERE event_id = %s""",
                (result["score"], "filing_number_mismatch" in result["reasons"], result["event_id"]),
            )
        else:
            db.conn.execute(
                f"""UPDATE signals SET manipulation_score = {p},
                        article_discrepancy_flag = {p}
                   WHERE event_id = {p}""",
                (result["score"], 1 if "filing_number_mismatch" in result["reasons"] else 0, result["event_id"]),
            )
        db.conn.commit()
    except Exception as exc:
        logger.warning("persist manipulation score failed: %s", exc)
        db.conn.rollback()


def fuse(
    *,
    event_id: str,
    ticker: str,
    base_score_result: Dict,
    pump_dump_result: Optional[Dict] = None,
    fine_print_result: Optional[Dict] = None,
    article_forensics_result: Optional[Dict] = None,
) -> Dict:
    """Combine the base manipulation score with pump-dump + fine-print + article.

    Produces a fused 0-100 score with a structured reason set so the UI can
    render a clean breakdown. The base score already covers discrepancy +
    intent + credibility; fused adds pump-dump and article-level tells.
    """
    base = int(base_score_result.get("score", 0))
    reasons = list(base_score_result.get("reasons", []))
    sub_scores: Dict[str, int] = {"base": base}

    if pump_dump_result:
        pump = int(pump_dump_result.get("pump_score", 0))
        sub_scores["pump_dump"] = pump
        if pump >= 60:
            reasons.append("likely_pump_pattern")
        elif pump >= 30:
            reasons.append("suspicious_pump_pattern")

    if fine_print_result:
        fp = int(fine_print_result.get("score", 0))
        sub_scores["fine_print"] = fp
        for f in fine_print_result.get("flags", []):
            if f.get("severity") == "critical":
                reasons.append(f"fp_{f['code']}")

    if article_forensics_result:
        art = int(article_forensics_result.get("rule_score", 0))
        sub_scores["article"] = art
        reasons.extend(article_forensics_result.get("rule_flags", []))
        llm = article_forensics_result.get("llm_verdict") or {}
        if llm.get("tone_bias") == "fake_neutral":
            reasons.append("fake_neutral_article")
            art = min(100, art + 10)
            sub_scores["article"] = art
        if llm.get("favouring_party") in {"company", "short_seller"}:
            reasons.append(f"favours_{llm.get('favouring_party')}")

    # Weighted fusion: base 40% + pump 25% + article 20% + fine_print 15%
    fused = (
        0.40 * sub_scores.get("base", 0)
        + 0.25 * sub_scores.get("pump_dump", 0)
        + 0.20 * sub_scores.get("article", 0)
        + 0.15 * sub_scores.get("fine_print", 0)
    )
    score = int(min(100, round(fused)))

    if score >= 70:
        band = "likely_manipulated"
    elif score >= 40:
        band = "unverified"
    else:
        band = "clean"

    # Dedup reasons preserving order
    seen = set()
    dedup = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            dedup.append(r)

    return {
        "event_id": event_id,
        "ticker": ticker,
        "score": score,
        "band": band,
        "sub_scores": sub_scores,
        "reasons": dedup,
    }


# Import Optional at module level (may not have been imported)
from typing import Optional  # noqa: E402


def promoter_selling_recent(db, ticker: str, days: int = 30) -> bool:
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    try:
        cursor = db.conn.cursor()
        if db.is_postgres:
            cursor.execute(
                """SELECT COUNT(*) FROM sebi_disclosures
                    WHERE ticker = %s AND transaction_type = 'sell'
                      AND transaction_date >= (CURRENT_DATE - INTERVAL '%s days')""",
                (ticker, days),
            )
        else:
            cursor.execute(
                f"""SELECT COUNT(*) FROM sebi_disclosures
                     WHERE ticker = {p} AND transaction_type = 'sell'
                       AND transaction_date >= date('now', '-{days} days')""",
                (ticker,),
            )
        row = cursor.fetchone()
        n = row[0] if not isinstance(row, dict) else row.get("count")
        return bool(n and int(n) > 0)
    except Exception as exc:
        logger.debug("promoter_selling_recent query failed: %s", exc)
        return False
