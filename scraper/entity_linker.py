"""Subject-confidence scoring for ticker matches.

Sits between hybrid_scraper.extract_entities (which finds candidate tickers)
and the signal writer (which persists to DB). For each candidate ticker, it
computes a 0.0-1.0 confidence that the ticker is the EDITORIAL SUBJECT of the
article (not just incidentally mentioned).

Solves the "Mstar order tagged to BSE stock" class of bug: a headline like
"Mstar wins order from BSE-listed company" should NOT tag BSE the ticker,
because BSE appears in exchange-reference context, not as the article subject.

Public API:
    score(ticker, text, title) -> (confidence: float, evidence: dict)
    is_exchange_context(ticker, text) -> bool
    filter_candidates(candidates, text, title, min_conf) -> [(t, conf, ev), ...]
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple

# Tickers that double as exchange/index names. Most "BSE"/"NSE" mentions in
# financial news refer to the exchange, NOT the listed parent companies.
EXCHANGE_TICKERS = {"BSE", "NSE"}

# Patterns that indicate BSE/NSE is being used in an EXCHANGE reference
# (sensex, listing, trading venue) rather than the ticker for the parent
# company. If any pattern matches near the BSE/NSE mention, downgrade.
_EXCHANGE_CONTEXT_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\b(bse|nse)[- ]listed\b",
        r"\b(bse|nse)[- ]traded\b",
        r"\bon (the )?(bse|nse)\b",
        r"\b(bse|nse) (sensex|nifty|500|100|200|midcap|smallcap|index|indices|listed)\b",
        r"\b(bse|nse) (sme|emerge|main\s*board)\b",
        r"\blisted on (the )?(bse|nse)\b",
        r"\btraded on (the )?(bse|nse)\b",
        r"\b(bse|nse) (filing|disclosure|announcement|circular|notification)\b",
        r"\bunder (bse|nse)\b",
        r"\b(bse|nse) data\b",
    )
]

# Agent-role prefixes (extends hybrid_scraper._AGENT_PREFIXES). A ticker
# mentioned only after one of these is not the subject.
_AGENT_PREFIXES = (
    "audited by", "advised by", "underwritten by", "led by",
    "managed by", "represented by", "cleared by", "rated by",
    "covered by", "recommended by", "analyzed by", "tracked by",
    "reviewed by", "sponsored by", "guaranteed by", "arranged by",
    "flagged by", "noted by", "commented by", "researched by",
    "according to", "as per", "per note from", "per a report by",
    "brokerage", "broker at", "analyst at", "note from", "report from",
    "from analyst", "research by", "data from",
)

# Order-recipient phrases — when present, the ticker AFTER these is the SUBJECT
# (it received/won the order). e.g., "Mstar wins order from BSE-listed company"
# — here BSE is the agent (the exchange-listing reference), Mstar is the subject.
_RECEIVED_FROM_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\b(wins?|received|secured?|bags?|gets?|awarded) (an? )?(order|contract|deal|mandate) from\b",
        r"\b(supplies?|supplied|delivered|delivers?) to\b",
        r"\bsigned (an? )?(mou|agreement|deal|contract) with\b",
    )
]


def is_exchange_context(ticker: str, text: str) -> bool:
    """Return True if BSE/NSE in `text` appears to mean the exchange, not the
    ticker for the parent listed company.

    Returns False for any ticker not in EXCHANGE_TICKERS — most calls short-circuit.
    """
    if ticker not in EXCHANGE_TICKERS:
        return False
    for pat in _EXCHANGE_CONTEXT_PATTERNS:
        if pat.search(text):
            return True
    return False


# Cache: id(companies_map) -> set of first tokens that are heads of 2+
# companies in that map. Built once per process; companies_map is a module-
# level constant in callers so id() is stable.
_AMBIGUOUS_FIRST_TOKEN_CACHE: Dict[int, set] = {}


def _ambiguous_first_tokens(companies_map: Dict[str, str]) -> set:
    """Return the set of lowercased first-tokens that head 2+ companies in
    `companies_map`. These tokens can never be safe match forms on their own —
    "tata" matches both TCS (Tata Consultancy) and TATASTEEL (Tata Steel)."""
    if not companies_map:
        return set()
    key = (id(companies_map), len(companies_map))
    cached = _AMBIGUOUS_FIRST_TOKEN_CACHE.get(key)
    if cached is not None:
        return cached
    counts: Dict[str, int] = {}
    for c in companies_map.values():
        if not c:
            continue
        tok = c.split(" ")[0].strip().lower()
        if len(tok) > 3:
            counts[tok] = counts.get(tok, 0) + 1
    ambiguous = {t for t, n in counts.items() if n >= 2}
    _AMBIGUOUS_FIRST_TOKEN_CACHE[key] = ambiguous
    return ambiguous


def _name_forms(ticker: str, short_names_map: Dict[str, str] = None,
                companies_map: Dict[str, str] = None) -> List[str]:
    """All lowercase strings this ticker could appear as in news text.

    Subject-extraction bug fix (2026-05-20): when the first token of the
    company name is shared with another company in `companies_map` (e.g.
    "Tata" appears as the first word of 13 different Tata-group tickers),
    we MUST NOT register the bare token as a match form — doing so makes
    every Tata-prefixed headline match every Tata-prefixed ticker. Instead,
    fall back to the two-word form ("tata consultancy", "tata steel"),
    which IS unambiguous.
    """
    forms = {ticker.lower()}
    if short_names_map:
        for sn, t in short_names_map.items():
            if t == ticker:
                forms.add(sn.lower())
    if companies_map:
        company = companies_map.get(ticker, "")
        if company:
            ambig = _ambiguous_first_tokens(companies_map)
            words = company.split(" ")
            first = words[0].strip().lower() if words else ""
            if len(first) > 3 and first not in ambig:
                forms.add(first)
            elif len(words) >= 2:
                # Bigram fallback for ambiguous brand prefixes.
                bigram = (words[0] + " " + words[1]).strip().lower()
                if len(bigram) > 4:
                    forms.add(bigram)
    return [f for f in forms if f]


def score(ticker: str, text: str, title: str = "",
          short_names_map: Dict[str, str] = None,
          companies_map: Dict[str, str] = None) -> Tuple[float, Dict]:
    """Score how confidently `ticker` is the SUBJECT of this article.

    Returns (confidence, evidence) where:
      confidence: 0.0 (incidental mention) to 1.0 (clearly the subject)
      evidence:   dict with 'reasons' list + intermediate scores

    Heuristic — combines six signals:
      1. Title presence       (+0.45)  strongest single signal
      2. Lead presence        (+0.20)  in first 200 chars of body
      3. Mention count >= 2   (+0.15)  multiple references
      4. No agent-role prefix (+0.10)  isn't "audited by X"
      5. Not exchange context (-0.60)  BSE/NSE as exchange = veto
      6. Not "received-from"  (-0.40)  ticker is the giver, not receiver
    """
    text_lc = text.lower()
    title_lc = (title or "").lower()
    names = _name_forms(ticker, short_names_map, companies_map)
    if not names:
        names = [ticker.lower()]

    reasons: List[str] = []
    conf = 0.0

    # Exchange-context veto first (kills false BSE/NSE matches before scoring)
    if is_exchange_context(ticker, text):
        # If the ticker name also appears in the title outside an exchange
        # reference, we forgive — but most cases don't.
        title_has_clean = False
        for n in names:
            if re.search(r"\b" + re.escape(n) + r"\b", title_lc) and \
               not is_exchange_context(ticker, title or ""):
                title_has_clean = True
                break
        if not title_has_clean:
            reasons.append("exchange_context_veto")
            return 0.0, {"confidence": 0.0, "reasons": reasons, "veto": "exchange"}

    # 1. Title presence
    title_hit = False
    for n in names:
        if title_lc and re.search(r"\b" + re.escape(n) + r"\b", title_lc):
            title_hit = True
            break
    if title_hit:
        conf += 0.45
        reasons.append("title_hit")

    # 2. Lead presence (first 200 chars of body)
    lead_lc = text_lc[:200]
    lead_hit = any(re.search(r"\b" + re.escape(n) + r"\b", lead_lc) for n in names)
    if lead_hit:
        conf += 0.20
        reasons.append("lead_hit")

    # 3. Mention count and agent-role check
    total_mentions = 0
    agent_mentions = 0
    received_from_mentions = 0
    for n in names:
        for m in re.finditer(r"\b" + re.escape(n) + r"\b", text_lc):
            total_mentions += 1
            preceding = text_lc[max(0, m.start() - 40):m.start()]
            if any(p in preceding for p in _AGENT_PREFIXES):
                agent_mentions += 1
            # "wins order from X" — X is the giver, not the subject
            for pat in _RECEIVED_FROM_PATTERNS:
                tail = text_lc[max(0, m.start() - 80):m.start()]
                if pat.search(tail):
                    received_from_mentions += 1
                    break

    if total_mentions >= 2:
        conf += 0.15
        reasons.append(f"mentions_{total_mentions}")

    # 4. Agent-role detection
    if total_mentions > 0 and agent_mentions == total_mentions:
        conf -= 0.30
        reasons.append("all_mentions_agent_role")
    elif total_mentions > 0 and agent_mentions < total_mentions:
        conf += 0.10
        reasons.append("has_non_agent_mention")

    # 6. Received-from veto (the giver is not the subject)
    if total_mentions > 0 and received_from_mentions >= total_mentions:
        conf -= 0.40
        reasons.append("only_in_received_from_context")

    # No mentions at all → 0
    if total_mentions == 0:
        return 0.0, {"confidence": 0.0, "reasons": ["no_mentions"]}

    # Clamp
    conf = max(0.0, min(1.0, conf))
    return conf, {
        "confidence": round(conf, 3),
        "reasons": reasons,
        "mentions": total_mentions,
        "agent_mentions": agent_mentions,
        "title_hit": title_hit,
    }


def filter_candidates(
    candidates: Iterable[str],
    text: str,
    title: str = "",
    min_conf: float = 0.45,
    short_names_map: Dict[str, str] = None,
    companies_map: Dict[str, str] = None,
) -> List[Tuple[str, float, Dict]]:
    """Score each candidate; return only those at/above min_conf.

    Returned list is sorted by confidence descending. Use the highest-confidence
    ticker as the primary subject; secondary mentions can be kept or dropped.
    """
    scored: List[Tuple[str, float, Dict]] = []
    for t in candidates:
        c, ev = score(t, text, title, short_names_map=short_names_map,
                      companies_map=companies_map)
        scored.append((t, c, ev))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [(t, c, ev) for t, c, ev in scored if c >= min_conf]


# --- News-type / source-tier classification ------------------------------

# Source-URL patterns that map to a high-trust news_type. Order matters:
# more specific patterns first.
_NEWS_TYPE_RULES = [
    ("filing",          re.compile(r"(bseindia\.com|nseindia\.com|sebi\.gov\.in)", re.I)),
    ("concall",         re.compile(r"(researchbytes|trendlyne.*concall|earningscall|transcript)", re.I)),
    ("research_report", re.compile(r"(researchreport|analyst-report|broker[-_]?note|/research/|/analyst/)", re.I)),
    ("regulatory",      re.compile(r"(rbi\.org|mca\.gov|incometaxindia|pib\.gov)", re.I)),
    ("press_release",   re.compile(r"(prnewswire|businesswire|/press[-_]?release|/announcement)", re.I)),
    ("social_buzz",     re.compile(r"(twitter\.com|x\.com|reddit\.com|t\.me/|telegram)", re.I)),
    ("news_article",    re.compile(r".*", re.I)),  # fallback
]


def classify_news_type(source: str, link: str = "") -> str:
    """Return one of: filing | concall | research_report | regulatory |
    press_release | social_buzz | news_article.

    Hook this into the article-ingest path so events.news_type / signals.news_type
    are set consistently. Powers the upstream source-quality filter.
    """
    blob = f"{source or ''} {link or ''}".lower()
    for label, pat in _NEWS_TYPE_RULES:
        if pat.search(blob):
            return label
    return "news_article"


# Source confidence multiplier (used by alpha-score gates downstream)
NEWS_TYPE_WEIGHT = {
    "filing":         1.20,   # primary source — boost
    "concall":        1.15,
    "regulatory":     1.15,
    "research_report": 1.00,
    "press_release":  0.95,
    "news_article":   0.90,
    "social_buzz":    0.50,   # display only, do not promote to signal
}


def is_promotable_to_signal(news_type: str, subject_confidence: float) -> bool:
    """Final gate: can this article become a curated signal?

    Filings/concalls/regulatory: confidence >= 0.4 (high-trust source).
    News articles: confidence >= 0.6 (need clear subject linkage).
    Social: never directly (must be promoted separately by social hub).
    """
    if news_type == "social_buzz":
        return False
    threshold = 0.40 if news_type in ("filing", "concall", "regulatory") else 0.60
    return subject_confidence >= threshold
