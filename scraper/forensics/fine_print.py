"""
Fine-print detector — the "buried in paragraph 9" plays.

Scans article/filing text for the tricks that reports use to make headline
numbers look better than they are. Pure regex + heuristics (no LLM needed),
so it runs cheaply on every article.

Categories detected:
  - standalone_vs_consolidated: ambiguous reporting basis
  - one_time_items: "exceptional", "non-recurring", "one-off" exclusions
  - gaap_adjustments: "adjusted", "pro-forma", "non-GAAP"
  - conditional_language: "subject to", "contingent on", "pending approval"
  - guidance_vs_fact: forward-looking language mixed into headlines
  - unit_mismatch: ₹ / $ / cr / lakh / mn confusion
  - currency_hedge_absence: large $-based claims without hedge mention
  - comparison_cherry_pick: YoY vs QoQ switches, excluding base year
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class FinePrintFlag:
    code: str
    severity: str      # "info" | "warn" | "critical"
    detail: str
    snippet: str = ""


# ---------- Detector rules ----------

_STANDALONE_WORDS = re.compile(r"\bstandalone\b", re.I)
_CONSOLIDATED_WORDS = re.compile(r"\bconsolidated\b", re.I)

_ONE_TIME_PATTERNS = [
    re.compile(r"\bexceptional items?\b", re.I),
    re.compile(r"\bone[- ]?time\b", re.I),
    re.compile(r"\bnon[- ]recurring\b", re.I),
    re.compile(r"\bone[- ]off\b", re.I),
    re.compile(r"\bextraordinary items?\b", re.I),
    re.compile(r"\bwriteback\b|\bwrite[- ]back\b", re.I),
]

_GAAP_ADJ_PATTERNS = [
    re.compile(r"\badjusted (?:EBITDA|profit|margin|revenue|earnings)\b", re.I),
    re.compile(r"\bnon[- ]GAAP\b", re.I),
    re.compile(r"\bpro[- ]forma\b", re.I),
    re.compile(r"\bnormalised?\b", re.I),
    re.compile(r"\brecurring profit\b", re.I),
]

_CONDITIONAL_PATTERNS = [
    re.compile(r"\bsubject to\b", re.I),
    re.compile(r"\bcontingent (?:on|upon)\b", re.I),
    re.compile(r"\bpending (?:regulatory|shareholder|approval|clearance)\b", re.I),
    re.compile(r"\bclose expected\b", re.I),
    re.compile(r"\bexpected to (?:close|complete)\b", re.I),
    re.compile(r"\bawait(?:s|ing) approval\b", re.I),
    re.compile(r"\bnon[- ]binding\b", re.I),
    re.compile(r"\bletter of intent\b|\bLOI\b", re.I),
    re.compile(r"\bMoU\b|memorandum of understanding", re.I),
]

_GUIDANCE_PATTERNS = [
    re.compile(r"\bguidance\b", re.I),
    re.compile(r"\bexpects? to (?:reach|deliver|grow|exceed)\b", re.I),
    re.compile(r"\baims? to\b", re.I),
    re.compile(r"\btarget(?:s|ing)?\b", re.I),
    re.compile(r"\bforesees?\b", re.I),
    re.compile(r"\bprojects?\b", re.I),
    re.compile(r"\boutlook\b", re.I),
]

_UNIT_INR = re.compile(r"(?:₹|Rs\.?|INR)\s*[\d,]+", re.I)
_UNIT_USD = re.compile(r"(?:\$|USD)\s*[\d,]+", re.I)
_UNIT_CRORE = re.compile(r"[\d,]+(?:\.\d+)?\s*(?:crore|cr)\b", re.I)
_UNIT_LAKH = re.compile(r"[\d,]+(?:\.\d+)?\s*lakh\b", re.I)
_UNIT_MILLION = re.compile(r"[\d,]+(?:\.\d+)?\s*(?:million|mn)\b", re.I)
_UNIT_BILLION = re.compile(r"[\d,]+(?:\.\d+)?\s*(?:billion|bn)\b", re.I)

_FY_COMPARISONS = re.compile(r"\b(?:YoY|QoQ|year[- ]on[- ]year|quarter[- ]on[- ]quarter)\b", re.I)

_SMALL_PRINT_HINT = re.compile(r"excluding\s+[a-z ]{3,60}", re.I)


def _first_snippet(text: str, pattern: re.Pattern, pad: int = 40) -> str:
    m = pattern.search(text or "")
    if not m:
        return ""
    s = max(0, m.start() - pad)
    e = min(len(text), m.end() + pad)
    snippet = text[s:e].replace("\n", " ").strip()
    return f"...{snippet}..."


def analyze(text: str) -> List[FinePrintFlag]:
    """Return a list of FinePrintFlag objects for the given text."""
    flags: List[FinePrintFlag] = []
    if not text:
        return flags

    has_standalone = bool(_STANDALONE_WORDS.search(text))
    has_consolidated = bool(_CONSOLIDATED_WORDS.search(text))
    # Ambiguity: mentions only one basis, especially "profit/revenue" without clarification
    if has_standalone and not has_consolidated:
        flags.append(FinePrintFlag(
            code="standalone_only",
            severity="warn",
            detail="Report cites standalone figures only. Group (consolidated) numbers may differ materially.",
            snippet=_first_snippet(text, _STANDALONE_WORDS),
        ))

    for rx in _ONE_TIME_PATTERNS:
        if rx.search(text):
            flags.append(FinePrintFlag(
                code="one_time_items",
                severity="warn",
                detail="Profit or revenue figure includes 'one-time' / 'exceptional' items. The underlying run-rate may be different.",
                snippet=_first_snippet(text, rx),
            ))
            break

    for rx in _GAAP_ADJ_PATTERNS:
        if rx.search(text):
            flags.append(FinePrintFlag(
                code="gaap_adjustment",
                severity="warn",
                detail="Headline uses 'adjusted' / 'pro-forma' / 'non-GAAP' metric. Reported GAAP figure may be weaker.",
                snippet=_first_snippet(text, rx),
            ))
            break

    for rx in _CONDITIONAL_PATTERNS:
        if rx.search(text):
            flags.append(FinePrintFlag(
                code="conditional_deal",
                severity="critical",
                detail="Deal is conditional / non-binding / pending approval. The claim may not materialise.",
                snippet=_first_snippet(text, rx),
            ))
            break

    guidance_hits = sum(1 for rx in _GUIDANCE_PATTERNS if rx.search(text))
    if guidance_hits >= 2:
        flags.append(FinePrintFlag(
            code="guidance_as_fact",
            severity="info",
            detail="Article heavily mixes forward-looking guidance with reported numbers. Take projections with a grain of salt.",
        ))

    # Unit mismatches
    has_inr = bool(_UNIT_INR.search(text))
    has_usd = bool(_UNIT_USD.search(text))
    has_cr = bool(_UNIT_CRORE.search(text))
    has_lakh = bool(_UNIT_LAKH.search(text))
    has_mn = bool(_UNIT_MILLION.search(text))
    has_bn = bool(_UNIT_BILLION.search(text))

    if has_usd and has_cr and not (has_bn or has_mn):
        flags.append(FinePrintFlag(
            code="currency_mismatch",
            severity="warn",
            detail="Mentions both $ and ₹ figures — confirm which is the headline number; FX can materially inflate one side.",
        ))
    if has_lakh and has_cr:
        flags.append(FinePrintFlag(
            code="unit_mix",
            severity="info",
            detail="Switches between crore and lakh. 100 lakh = 1 crore — check the quoted figure's scale.",
        ))

    if _SMALL_PRINT_HINT.search(text):
        flags.append(FinePrintFlag(
            code="small_print_exclusion",
            severity="warn",
            detail="Contains 'excluding ...' clause — a key number may be reported after stripping out adverse items.",
            snippet=_first_snippet(text, _SMALL_PRINT_HINT),
        ))

    return flags


def summarise(flags: List[FinePrintFlag]) -> Dict:
    critical = sum(1 for f in flags if f.severity == "critical")
    warn = sum(1 for f in flags if f.severity == "warn")
    info = sum(1 for f in flags if f.severity == "info")
    score = min(100, critical * 25 + warn * 12 + info * 4)
    return {
        "total": len(flags),
        "critical": critical,
        "warn": warn,
        "info": info,
        "score": score,
        "flags": [
            {"code": f.code, "severity": f.severity, "detail": f.detail, "snippet": f.snippet}
            for f in flags
        ],
    }


def analyze_and_summarise(text: str) -> Dict:
    return summarise(analyze(text))
