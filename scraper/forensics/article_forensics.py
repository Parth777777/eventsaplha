"""
Article-level forensics — reads the article and flags whose interest it serves.

Two layers:

1. **Rule-based (always runs, free):**
   - anonymous source counts ("sources told", "close to the matter", "people familiar")
   - unsourced analyst-target claims ("target price of ₹500" with no analyst attribution)
   - promotional adjective density ("revolutionary", "game-changing", "breakthrough")
   - bear-shorting language density ("overleveraged", "unsustainable", "imminent collapse")
   - balanced-looking-but-biased structure (tiny bear counter-point buried at end)

2. **LLM layer (optional, only when GROQ_API_KEY set):**
   - favouring_party: "company" | "short_seller" | "industry" | "retail" | "none"
   - tone_bias: "bullish" | "bearish" | "neutral" | "fake_neutral"
   - confidence: 0-1
   - one-line rationale

The LLM layer is gated: only runs on articles that already have ≥2 rule-based
flags, to keep Groq usage under the free-tier quota.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# ---------- Rule-based detectors ----------

_ANONYMOUS_PATTERNS = [
    re.compile(r"\bsources? (?:told|said|close to)\b", re.I),
    re.compile(r"\bpeople familiar (?:with|close to)\b", re.I),
    re.compile(r"\baccording to (?:sources|people|insiders)\b", re.I),
    re.compile(r"\ban? insider\b", re.I),
    re.compile(r"\b(?:a|one) source said\b", re.I),
    re.compile(r"\brequesting anonymity\b", re.I),
    re.compile(r"\banonymous(?:ly)?\b", re.I),
]

_UNSOURCED_TARGET_PATTERNS = [
    re.compile(r"target price of (?:₹|Rs\.?|INR)?\s*[\d,]+", re.I),
    re.compile(r"could (?:hit|reach|touch) (?:₹|Rs\.?|INR)?\s*[\d,]+", re.I),
    re.compile(r"may double|might double", re.I),
    re.compile(r"\bset to rally\b|\bpoised to surge\b", re.I),
]

_ANALYST_ATTRIB_PATTERNS = [
    re.compile(r"\b(?:Jefferies|Morgan Stanley|Goldman|HSBC|JP Morgan|Citi|CLSA|Kotak|Nomura|Motilal)\b", re.I),
    re.compile(r"\banalyst(?:s)? at\b", re.I),
    re.compile(r"\bin a (?:note|report)\b", re.I),
]

_PROMOTIONAL_ADJECTIVES = [
    "revolutionary", "game-changing", "game changer", "breakthrough",
    "transformational", "unprecedented", "stellar", "blockbuster",
    "mega", "massive", "phenomenal", "skyrocket", "moonshot",
    "multi-bagger", "multibagger", "hidden gem", "best-in-class",
]

_BEAR_ADJECTIVES = [
    "unsustainable", "imminent collapse", "looming", "doomed",
    "fraud", "scam", "ponzi", "overleveraged", "overheated",
    "bubble", "crash imminent", "house of cards",
]


@dataclass
class ArticleForensics:
    anonymous_source_count: int = 0
    unsourced_target_claims: int = 0
    has_analyst_attribution: bool = False
    promotional_density: int = 0
    bear_density: int = 0
    suspicious_balance: bool = False
    rule_flags: List[str] = field(default_factory=list)
    llm_verdict: Optional[Dict] = None

    @property
    def rule_score(self) -> int:
        """Pure rule-based 0-100 manipulability index."""
        score = 0
        score += min(20, self.anonymous_source_count * 7)
        score += min(15, self.unsourced_target_claims * 8)
        if self.unsourced_target_claims > 0 and not self.has_analyst_attribution:
            score += 10
        score += min(20, self.promotional_density * 5)
        score += min(15, self.bear_density * 5)
        if self.suspicious_balance:
            score += 8
        return min(100, score)

    def to_dict(self) -> Dict:
        return {
            "anonymous_source_count": self.anonymous_source_count,
            "unsourced_target_claims": self.unsourced_target_claims,
            "has_analyst_attribution": self.has_analyst_attribution,
            "promotional_density": self.promotional_density,
            "bear_density": self.bear_density,
            "suspicious_balance": self.suspicious_balance,
            "rule_flags": self.rule_flags,
            "rule_score": self.rule_score,
            "llm_verdict": self.llm_verdict,
        }


def _count(patterns, text: str) -> int:
    total = 0
    for p in patterns:
        total += len(p.findall(text))
    return total


def _density(keywords: List[str], text: str) -> int:
    t = (text or "").lower()
    return sum(1 for kw in keywords if kw in t)


def _suspicious_balance(text: str) -> bool:
    """Article's last 15% contains the only caveat — classic one-sided-but-pretending."""
    if not text or len(text) < 500:
        return False
    tail = text[int(len(text) * 0.85):]
    caveat_words = ["however", "on the other hand", "risks include", "risks are",
                    "bears argue", "critics say", "downside"]
    has_caveat_in_tail = any(w in tail.lower() for w in caveat_words)
    body = text[: int(len(text) * 0.85)]
    has_caveat_in_body = any(w in body.lower() for w in caveat_words)
    return has_caveat_in_tail and not has_caveat_in_body


def analyze_rules(text: str) -> ArticleForensics:
    f = ArticleForensics()
    if not text:
        return f
    f.anonymous_source_count = _count(_ANONYMOUS_PATTERNS, text)
    f.unsourced_target_claims = _count(_UNSOURCED_TARGET_PATTERNS, text)
    f.has_analyst_attribution = bool(_count(_ANALYST_ATTRIB_PATTERNS, text))
    f.promotional_density = _density(_PROMOTIONAL_ADJECTIVES, text)
    f.bear_density = _density(_BEAR_ADJECTIVES, text)
    f.suspicious_balance = _suspicious_balance(text)
    # Build flag list
    if f.anonymous_source_count >= 2:
        f.rule_flags.append("heavy_anonymous_sourcing")
    elif f.anonymous_source_count >= 1:
        f.rule_flags.append("anonymous_sources")
    if f.unsourced_target_claims > 0 and not f.has_analyst_attribution:
        f.rule_flags.append("unsourced_target_claim")
    if f.promotional_density >= 3:
        f.rule_flags.append("promotional_tone")
    if f.bear_density >= 3:
        f.rule_flags.append("attack_piece")
    if f.suspicious_balance:
        f.rule_flags.append("balanced_in_form_only")
    return f


# ---------- LLM layer ----------

_LLM_PROMPT = """You read Indian financial news as a forensic analyst.
Return a JSON object with exactly this shape:
{
  "favouring_party": one of ["company","short_seller","industry","retail","regulator","none"],
  "tone_bias": one of ["bullish","bearish","neutral","fake_neutral"],
  "confidence": float 0.0-1.0,
  "rationale": "<100 chars plain-English justification",
  "specific_tells": ["..."]   // short phrases quoted/paraphrased from the text
}
"fake_neutral" means the article reads neutral but only cites sources from one side.
Only return valid JSON. No commentary."""


def llm_verdict(text: str) -> Optional[Dict]:
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        return None
    try:
        from groq_governor import governor as _gov
    except Exception:
        _gov = None
    if _gov is not None and not _gov.can_spend("forensics", est_tokens=1500):
        return None
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
                "messages": [
                    {"role": "system", "content": _LLM_PROMPT},
                    {"role": "user", "content": (text or "")[:4000]},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            },
            timeout=15,
        )
        if resp.status_code == 429 and _gov is not None:
            _gov.record_429("forensics")
            return None
        resp.raise_for_status()
        if _gov is not None:
            try:
                tokens = int((resp.json().get("usage") or {}).get("total_tokens", 1500))
            except Exception:
                tokens = 1500
            _gov.record_spend("forensics", tokens)
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except Exception as exc:
        logger.debug("llm verdict failed: %s", exc)
        return None


# ---------- Public API ----------

def analyze(text: str, use_llm: bool = True, llm_gate: int = 2) -> Dict:
    """Full analysis. LLM runs only if rule-flag count >= llm_gate (cost gate)."""
    f = analyze_rules(text)
    if use_llm and len(f.rule_flags) >= llm_gate:
        f.llm_verdict = llm_verdict(text)
    return f.to_dict()
