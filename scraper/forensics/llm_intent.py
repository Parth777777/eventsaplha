"""
LLM intent + fingerprint extension.

Drop-in companion to LLMCrossVerifier in hybrid_scraper.py. Adds:
  - intent: what the article is trying to accomplish
  - fingerprint_flags: manipulation tells found in the text
  - source_credibility: outlet+author rolling hit-rate
  - coordinated_campaign_flag: same claim across ≥3 outlets within 2h

Kept separate so the base verifier stays stable and we can test this layer in
isolation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


INTENT_LABELS = [
    "promote", "warn", "neutral_report", "defend",
    "attack", "hype", "dump_setup", "accumulate_setup",
]


SYSTEM_PROMPT = """You are a forensic financial journalism analyst. Given a news article about an Indian listed company, return a JSON object with:
{
  "intent": one of ["promote", "warn", "neutral_report", "defend", "attack", "hype", "dump_setup", "accumulate_setup"],
  "intent_confidence": float 0.0-1.0,
  "fingerprint_flags": array of strings from ["unsourced_numbers", "anonymous_insider", "contradicts_public_facts", "promo_disguised_as_analysis", "recycled_phrasing", "pressure_language", "fear_language"],
  "rationale": one short sentence explaining your call (<100 chars).
}
Return only valid JSON. Be strict — don't invent fingerprint flags without evidence from the text."""


def classify_intent(article_text: str, groq_api_key: str = "",
                    model: Optional[str] = None) -> Optional[Dict]:
    """Call Groq with the intent schema. Returns parsed dict or None on failure."""
    if not groq_api_key:
        groq_api_key = os.getenv("GROQ_API_KEY", "")
    if not groq_api_key:
        return None
    model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    try:
        from groq_governor import governor as _gov
    except Exception:
        _gov = None
    if _gov is not None and not _gov.can_spend("intent", est_tokens=1500):
        return None

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": article_text[:4000]},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            },
            timeout=15,
        )
        if resp.status_code == 429 and _gov is not None:
            _gov.record_429("intent")
            return None
        resp.raise_for_status()
        if _gov is not None:
            try:
                tokens = int((resp.json().get("usage") or {}).get("total_tokens", 1500))
            except Exception:
                tokens = 1500
            _gov.record_spend("intent", tokens)
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict) or "intent" not in parsed:
            return None
        if parsed["intent"] not in INTENT_LABELS:
            parsed["intent"] = "neutral_report"
        parsed.setdefault("fingerprint_flags", [])
        if not isinstance(parsed["fingerprint_flags"], list):
            parsed["fingerprint_flags"] = []
        parsed.setdefault("intent_confidence", 0.5)
        return parsed
    except Exception as exc:
        logger.warning("llm intent failed: %s", exc)
        return None


def should_classify(event: Dict) -> bool:
    """Decide whether an event merits LLM intent analysis.

    Broader than base LLMCrossVerifier.needs_verification — we ALWAYS classify
    earnings/order_win/merger/insider events, because those are the categories
    most prone to manipulation.
    """
    etype = (event.get("event_type") or "").lower()
    if etype in {"earnings", "order_win", "merger", "insider"}:
        return True
    if event.get("is_macro"):
        return True
    if (event.get("magnitude") or 0) >= 6:
        return True
    return False


# ---------- Heuristic intent classifier (no LLM, always runs) ----------

_HEURISTIC_INTENT_CUES = {
    "promote": [
        "multi-bagger", "multibagger", "stellar", "blockbuster", "breakthrough",
        "massive upside", "poised to rally", "poised to surge", "set to rally",
        "best-in-class", "transformational", "game-changing", "record high",
        "strong buy", "outperform", "bullish", "upgrade",
    ],
    "warn": [
        "downgrade", "sell rating", "caution", "risks", "concerns",
        "headwinds", "underperform", "bearish", "red flag", "deteriorating",
        "under pressure", "guidance cut", "earnings miss",
    ],
    "attack": [
        "fraud", "scam", "ponzi", "overleveraged", "unsustainable",
        "doomed", "collapse", "investigation", "probe", "lawsuit",
        "sebi action", "class action", "short-seller", "short seller",
    ],
    "defend": [
        "clarifies", "refutes", "denies", "rebuts", "responds to allegations",
        "refutes the claims", "no truth to", "rejects",
    ],
    "hype": [
        "to the moon", "massive rally", "rocket", "explosive growth",
        "skyrocket", "mega deal", "historic high", "soars", "surges",
    ],
    "dump_setup": [
        "target price hike", "buy on dip", "accumulate now", "entry point",
        "unmissable opportunity", "before it's too late", "don't miss",
    ],
    "accumulate_setup": [
        "bottom out", "oversold", "value pick", "turnaround", "recovery",
        "discount to peers", "re-rating expected",
    ],
}

_HEURISTIC_FINGERPRINT_CUES = {
    "anonymous_insider": ["sources told", "people familiar", "sources close to",
                          "insiders said", "requesting anonymity", "an insider"],
    "unsourced_numbers": ["could reach", "may touch", "might hit", "set to cross"],
    "promo_disguised_as_analysis": ["in our view", "we believe", "buying opportunity",
                                      "screaming buy", "no-brainer"],
    "recycled_phrasing": [],  # detected elsewhere via coordinated_campaign
    "pressure_language": ["before it's too late", "don't miss out", "limited time",
                           "act now", "opportunity of a lifetime"],
    "fear_language": ["imminent crash", "bubble about to burst", "panic selling",
                       "catastrophic", "worst fears"],
}


def heuristic_intent(event_or_text) -> Dict:
    """Rule-based intent + fingerprint classifier. Always returns a dict.

    Used as a fallback when Groq isn't configured, or as a fast pre-filter
    before spending LLM budget. Accepts either an event dict or raw text.
    """
    if isinstance(event_or_text, dict):
        text = " ".join([
            event_or_text.get("headline", "") or "",
            event_or_text.get("summary", "") or "",
            event_or_text.get("title", "") or "",
        ])
        sentiment = (event_or_text.get("sentiment") or "").lower()
        event_type = (event_or_text.get("event_type") or "").lower()
    else:
        text = str(event_or_text or "")
        sentiment = ""
        event_type = ""

    text_low = text.lower()
    if not text_low.strip():
        return {
            "intent": "neutral_report",
            "intent_confidence": 0.3,
            "fingerprint_flags": [],
            "rationale": "empty text",
            "classifier": "heuristic",
        }

    # Score each intent by how many of its cues are present
    scores: Dict[str, int] = {}
    for intent, cues in _HEURISTIC_INTENT_CUES.items():
        hits = sum(1 for cue in cues if cue in text_low)
        if hits > 0:
            scores[intent] = hits

    # If no specific intent signals, fall back to sentiment-based default
    if not scores:
        if sentiment == "bullish":
            intent = "promote"
        elif sentiment == "bearish":
            intent = "warn"
        else:
            intent = "neutral_report"
        confidence = 0.4
    else:
        intent = max(scores.items(), key=lambda x: x[1])[0]
        confidence = min(0.85, 0.45 + 0.1 * scores[intent])

    # Event-type modifiers: "insider" events with bearish tone → dump_setup
    if event_type == "insider" and sentiment == "bearish":
        intent = "dump_setup"
        confidence = max(confidence, 0.7)
    elif event_type == "insider" and sentiment == "bullish":
        intent = "accumulate_setup"
        confidence = max(confidence, 0.7)

    # Fingerprint flags
    flags: list = []
    for flag, cues in _HEURISTIC_FINGERPRINT_CUES.items():
        if any(cue in text_low for cue in cues):
            flags.append(flag)

    return {
        "intent": intent,
        "intent_confidence": round(confidence, 3),
        "fingerprint_flags": flags,
        "rationale": f"{len(flags)} fingerprint flag(s); scored by keyword cues",
        "classifier": "heuristic",
    }


def classify_intent_with_fallback(article_text: str, event: Dict = None,
                                    groq_api_key: str = "",
                                    model: Optional[str] = None) -> Dict:
    """Try LLM first; fall back to heuristic if LLM unavailable/fails.

    Guarantees a usable dict return — the UI can always render the intent field.
    """
    if not groq_api_key:
        groq_api_key = os.getenv("GROQ_API_KEY", "")

    # If LLM is available, try it first
    if groq_api_key:
        llm_result = classify_intent(article_text, groq_api_key, model)
        if llm_result:
            llm_result["classifier"] = "llm"
            return llm_result

    # Heuristic fallback
    return heuristic_intent({"headline": article_text, **(event or {})})


# ---------- Source credibility ----------

class SourceCredibility:
    """Rolling outlet + author credibility score."""

    DEFAULT = 0.5

    def __init__(self, db):
        self.db = db

    def _placeholder(self) -> str:
        return "%s" if getattr(self.db, "is_postgres", False) else "?"

    def get(self, outlet: str, author: Optional[str] = None) -> float:
        p = self._placeholder()
        try:
            cursor = self.db.conn.cursor()
            cursor.execute(
                f"SELECT credibility_score FROM source_credibility WHERE outlet = {p} AND (author = {p} OR author IS NULL) ORDER BY author NULLS LAST LIMIT 1"
                if self.db.is_postgres
                else f"SELECT credibility_score FROM source_credibility WHERE outlet = {p} AND (author = {p} OR author IS NULL) LIMIT 1",
                (outlet, author or ""),
            )
            row = cursor.fetchone()
            if row:
                v = row[0] if not isinstance(row, dict) else row.get("credibility_score")
                return float(v or self.DEFAULT)
        except Exception as exc:
            logger.debug("credibility get failed: %s", exc)
        return self.DEFAULT

    def recompute(self) -> int:
        """Recompute credibility from prediction hit-rates. Returns rows updated."""
        p = self._placeholder()
        try:
            cursor = self.db.conn.cursor()
            # Aggregate outlet-level hit rates from recent predictions
            cursor.execute(
                """SELECT s.source, COUNT(*) as n,
                          SUM(CASE WHEN p.hit_target = 1 OR p.hit_target = TRUE THEN 1 ELSE 0 END) as h
                     FROM signals s
                     JOIN predictions p ON p.event_id = s.event_id
                    WHERE p.actual_price IS NOT NULL
                    GROUP BY s.source"""
            )
            rows = cursor.fetchall()
            updated = 0
            for r in rows:
                outlet = r[0] if not isinstance(r, dict) else r.get("source")
                n = r[1] if not isinstance(r, dict) else r.get("n")
                h = r[2] if not isinstance(r, dict) else r.get("h")
                if not outlet or not n:
                    continue
                rate = float(h) / float(n)
                # Smoothed: shrink toward 0.5 when sample size is low
                credibility = (rate * n + 0.5 * 20) / (n + 20)
                if self.db.is_postgres:
                    self.db.conn.cursor().execute(
                        """INSERT INTO source_credibility (outlet, author, hit_rate, sample_size, credibility_score)
                           VALUES (%s, NULL, %s, %s, %s)
                           ON CONFLICT (outlet, author) DO UPDATE SET
                             hit_rate = EXCLUDED.hit_rate,
                             sample_size = EXCLUDED.sample_size,
                             credibility_score = EXCLUDED.credibility_score,
                             last_computed_at = CURRENT_TIMESTAMP""",
                        (outlet, rate, n, credibility),
                    )
                else:
                    self.db.conn.execute(
                        f"""INSERT OR REPLACE INTO source_credibility
                             (outlet, author, hit_rate, sample_size, credibility_score)
                             VALUES ({p}, NULL, {p}, {p}, {p})""",
                        (outlet, rate, n, credibility),
                    )
                updated += 1
            self.db.conn.commit()
            return updated
        except Exception as exc:
            logger.warning("credibility recompute failed: %s", exc)
            self.db.conn.rollback()
            return 0


# ---------- Coordinated campaign detection ----------

def _cluster_key(title: str, ticker: Optional[str]) -> str:
    # 10-char prefix of normalized title to cluster near-identical phrasing
    from ..social.common import normalize_headline
    return hashlib.sha256(f"{(ticker or '').upper()}|{normalize_headline(title)[:80]}".encode()).hexdigest()[:16]


def detect_coordinated_campaign(db, title: str, ticker: Optional[str],
                                window_hours: int = 2,
                                min_outlets: int = 3) -> bool:
    """Check whether this article is one of ≥min_outlets near-identical claims within window_hours.

    Uses scraped_articles table (which already stores recent articles with a
    title_hash). We look up how many distinct outlets reported a near-identical
    title in the window; min_outlets triggers the coordinated flag.
    """
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    try:
        from ..social.common import normalize_headline

        norm = normalize_headline(title)[:80]
        if not norm:
            return False

        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        cursor = db.conn.cursor()
        if db.is_postgres:
            cursor.execute(
                """SELECT DISTINCT source FROM scraped_articles
                    WHERE first_seen_at >= %s AND LOWER(title) LIKE %s""",
                (since, f"%{norm.split(' ')[0] if ' ' in norm else norm}%"),
            )
        else:
            cursor.execute(
                f"""SELECT DISTINCT source FROM scraped_articles
                     WHERE first_seen_at >= {p} AND LOWER(title) LIKE {p}""",
                (since.isoformat(), f"%{norm.split(' ')[0] if ' ' in norm else norm}%"),
            )
        outlets = {r[0] if not isinstance(r, dict) else r.get("source") for r in cursor.fetchall() if r}
        return len(outlets) >= min_outlets
    except Exception as exc:
        logger.debug("coordinated detect failed: %s", exc)
        return False
