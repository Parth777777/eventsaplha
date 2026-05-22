"""TickerWave news-quality classifier.

Rule-based filter that separates institutional-grade developments from
SEO/engagement noise. Runs in the hot path (insert_event, upsert_signal,
and at API read time), so it must stay cheap — pure regex + keyword
lookup, no LLM, no IO.

The classifier returns a NewsQuality dataclass:

    impact_tier   "critical" | "high" | "meaningful" | "generic" | "noise"
    quality_score 0..100 — drives sort order on the newsroom feed
    is_noise      shortcut: true when impact_tier == "noise"
    kinds         detected high-impact kinds: ["concall", "order_win", ...]
    reasons       human-readable: ["matched promo phrase: ...", ...]
    market_implication  optional one-line analytical takeaway (rewrite hint)
    suggested_event_type  best-guess event_type ('earnings'|'order_win'|...)

Callers should treat `impact_tier == "noise"` as suppressible and prefer
sorting by quality_score for newsroom-style listings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Noise / clickbait detectors
# ---------------------------------------------------------------------------
# Match patterns that almost always signal SEO-driven engagement bait rather
# than substantive market news. A single hit is enough to mark "noise".

_NOISE_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p, re.IGNORECASE) for p in (
    # "Top N stocks to buy" / "best stocks for ..." templates
    r"\btop\s+\d+\s+(?:stocks?|shares?|picks?)\b",
    r"\bbest\s+(?:stocks?|shares?|picks?)\s+(?:to\s+buy|for|of)\b",
    r"\bstocks?\s+to\s+(?:buy|watch|hold)\s+(?:now|today|this\s+week|in\s+\d{4})\b",
    r"\bhot\s+stocks?\b",
    r"\bmust[-\s]buy\s+stocks?\b",
    # Tipster/pump phrasing
    r"\bmultibagger\b",
    r"\bmulti[-\s]bagger\b",
    r"\b(?:10|100|1000)x\b",
    r"\bnext\s+(?:reliance|adani|tata|infosys|tcs)\b",
    r"\bhidden\s+gem\b",
    r"\bsmallcap\s+rocket\b",
    r"\b(?:rocket|blast)\s+(?:stock|call|tip)\b",
    r"\bbumper\s+(?:return|profit)\b",
    r"\bsure[-\s]?shot\b",
    r"\bguaranteed\s+(?:return|profit|call)\b",
    r"\b(?:vip|premium|paid)\s+(?:call|tip|group|channel)\b",
    r"\boperator\s+(?:buying|stock|call|activity)\b",
    r"\bcircuit\s+lock|upper\s+circuit\s+lock\b",
    # "Experts say" / vague punditry
    r"\bexperts?\s+(?:say|bullish|bearish|recommend|predict)\b",
    r"\banalysts?\s+(?:say|are\s+bullish|are\s+bearish)\b(?!.+(?:target|price|raised|cut))",
    r"\bcould\s+(?:double|triple|soar|explode|surge)\b",
    r"\bmay\s+(?:explode|skyrocket|soar)\b",
    r"\bset\s+to\s+(?:soar|explode|jump|surge)\b",
    r"\bwhy\s+(?:you\s+should|investors\s+should)\s+(?:buy|sell|watch)\b",
    # Vague-cause headlines: "rises after announcement", "falls on news"
    # — the cause-clause word exists but the cause itself is content-free.
    r"\b(?:rises?|falls?|jumps?|drops?|surges?|gains?|plunges?|slumps?)\s+"
    r"(?:after|on|following|amid|over|post)\s+"
    r"(?:announcement|news|reports?|update|statement|move|action|"
    r"developments?|comments?)\b",
    # Naked move-only headlines with no cause clause (handled separately
    # below — only fires when no high-impact kind is detected).
))

# Verbs that, on their own, indicate "stock moved" without explaining why.
# We treat headlines that are *only* move-verbs (no cause, no number, no
# proper-noun context beyond the ticker) as noise.
_MOVE_VERBS_RE = re.compile(
    r"\b(?:surges?|soars?|jumps?|rallies|rises?|gains?|climbs?|"
    r"falls?|plunges?|tumbles?|drops?|slumps?|sinks?|slides?|cracks?)\b",
    re.IGNORECASE,
)

# Cause-clause indicators that rescue an otherwise-bare move headline.
_CAUSE_INDICATORS = (
    " after ", " on ", " as ", " amid ", " following ", " post ", " ahead of ",
    " due to ", " because ", " over ", " on back of ",
)


# ---------------------------------------------------------------------------
# High-impact kind detectors
# ---------------------------------------------------------------------------
# Each entry: (kind, regex, base_quality_boost, suggested_event_type)
# Order matters only for `suggested_event_type` (first match wins).

_HIGH_IMPACT: Tuple[Tuple[str, re.Pattern, int, str], ...] = (
    ("earnings_result",
     re.compile(
         r"\b(?:q[1-4]\s*(?:fy|results?)|quarterly\s+results?|annual\s+report|"
         r"ebitda|profit\s+(?:after|before)\s+tax|pat\s+(?:rises?|falls?|jumps?)|"
         r"net\s+profit|revenue\s+(?:grew|rose|fell|jumped)|topline|bottomline)\b",
         re.IGNORECASE),
     22, "earnings"),
    ("guidance",
     re.compile(
         r"\b(?:raise[sd]?\s+guidance|cut[s]?\s+guidance|lower[sed]+\s+guidance|"
         r"revise[sd]?\s+(?:upward|downward)?\s*outlook|management\s+commentary|"
         r"beat[s]?\s+(?:estimates|expectations|consensus)|"
         r"misses?\s+(?:estimates|expectations|consensus))\b",
         re.IGNORECASE),
     20, "earnings"),
    ("concall",
     re.compile(
         r"\b(?:earnings\s+call|conference\s+call|concall|con\.?\s*call|"
         r"investor\s+(?:day|meet|presentation)|analyst\s+day|"
         r"capital\s+markets?\s+day)\b",
         re.IGNORECASE),
     20, "earnings"),
    ("order_win",
     re.compile(
         r"\b(?:order\s+win|wins?\s+(?:[^\s]+\s+){0,5}(?:order|contract|project|tender)|"
         r"secures?\s+(?:[^\s]+\s+){0,5}(?:order|contract|deal|tender|loa|"
         r"letter\s+of\s+(?:award|intent))|"
         r"bags?\s+(?:[^\s]+\s+){0,5}(?:order|contract|project|deal|loa)|"
         r"receives?\s+(?:[^\s]+\s+){0,5}(?:order|contract|loa|letter\s+of\s+(?:award|intent))|"
         r"gets?\s+(?:[^\s]+\s+){0,5}(?:order|contract|loa)|"
         r"emerges?\s+lowest\s+bidder|l1\s+bidder|"
         r"awarded?\s+(?:[^\s]+\s+){0,5}(?:order|contract|project|tender)|"
         r"signs?\s+(?:[^\s]+\s+){0,5}(?:contract|deal|mou|memorandum|pact|agreement)|"
         r"inks?\s+(?:[^\s]+\s+){0,5}(?:deal|contract|mou|pact|agreement|partnership)|"
         r"ties?\s+up\s+with|joint\s+venture|jv\s+with|"
         r"order\s+(?:book|inflow|intake|pipeline))\b",
         re.IGNORECASE),
     22, "order_win"),
    ("m_and_a",
     re.compile(
         r"\b(?:merger|acquisition|acquires?|to\s+acquire|takeover|"
         r"amalgamation|to\s+merge|open\s+offer|reverse\s+merger|"
         r"slump\s+sale|hive\s+off|de[-\s]?merger|spin[-\s]?off)\b",
         re.IGNORECASE),
     22, "merger"),
    ("capex",
     re.compile(
         r"\b(?:capex\s+(?:plan|push|expansion|outlay|programme|program)|"
         r"capacity\s+(?:expansion|addition|enhancement|ramp[-\s]?up)|"
         r"greenfield|brownfield|"
         r"new\s+(?:plant|factory|facility|unit|manufacturing|line)|"
         r"commission(?:s|ed|ing)?\s+(?:plant|unit|line|facility|capacity)|"
         r"open(?:s|ed|ing)?\s+(?:new\s+)?(?:plant|factory|facility|unit)|"
         r"break(?:s|ing)?\s+ground|inaugurat(?:es|ed|ing)\s+(?:plant|facility|unit)|"
         r"sets?\s+up\s+(?:plant|facility|capacity|unit|factory)|"
         r"setting\s+up\s+(?:plant|facility|capacity|unit|factory)|"
         r"(?:to\s+)?invest\s+(?:₹|rs\.?|inr|\$|usd)?\s*[\d,]+\s*(?:crore|cr|bn|billion|mn|million|lakh)|"
         r"expansion\s+(?:plan|project)|"
         r"to\s+expand\s+(?:capacity|production|manufacturing))\b",
         re.IGNORECASE),
     18, "order_win"),
    ("policy",
     re.compile(
         r"\b(?:pli\s+scheme|production[-\s]linked\s+incentive|"
         r"cabinet\s+(?:approves?|clears?)|union\s+budget|"
         r"gst\s+council|gst\s+rate|customs?\s+duty|import\s+duty|"
         r"export\s+(?:ban|incentive|duty)|tariff|"
         r"finance\s+ministry|ministry\s+of\s+\w+)\b",
         re.IGNORECASE),
     20, "policy"),
    ("rate_action",
     re.compile(
         r"\b(?:rbi\s+(?:hikes?|cuts?|holds?|raises?)|repo\s+rate|"
         r"mpc\s+(?:meeting|decision)|monetary\s+policy|"
         r"fed\s+(?:hikes?|cuts?|holds?)|fomc|"
         r"interest\s+rate\s+(?:hike|cut|decision))\b",
         re.IGNORECASE),
     26, "policy"),
    ("regulatory",
     re.compile(
         r"\b(?:sebi\s+(?:bars?|fines?|penalises?|orders?|issues?|action|bans?)|"
         r"cci\s+(?:approves?|orders?|fines?)|"
         r"nclt\s+(?:order|hearing|approves?)|"
         r"income\s+tax\s+(?:raid|notice|search)|enforcement\s+directorate|"
         r"ed\s+raid|cbi\s+(?:probe|raid)|insolvency|"
         r"insider\s+trading)\b",
         re.IGNORECASE),
     26, "policy"),
    ("rating",
     re.compile(
         r"\b(?:rating\s+(?:upgrade|downgrade|cut|raised?)|"
         r"(?:crisil|icra|care\s+ratings?|moody'?s?|s&p|fitch|brickwork)\s+"
         r"(?:upgrades?|downgrades?|cuts?|raises?|revises?)|"
         r"(?:upgraded?|downgraded?)\s+(?:by|to)\s+(?:crisil|icra|care|moody|s&p|fitch|brickwork)|"
         r"credit\s+rating|outlook\s+(?:revised|negative|positive|stable))\b",
         re.IGNORECASE),
     18, "policy"),
    ("promoter",
     re.compile(
         r"\b(?:promoter\s+(?:holding|stake|buy(?:ing)?|sell(?:ing)?|pledge|encumbrance)|"
         r"sast\s+(?:disclosure|filing)?|insider\s+(?:buy(?:ing)?|sell(?:ing)?|trade)|"
         r"pit\s+(?:disclosure)?|bulk\s+deal|block\s+deal|"
         r"open\s+market\s+(?:purchase|sale))\b",
         re.IGNORECASE),
     20, "insider"),
    ("debt_stress",
     re.compile(
         r"\b(?:default|defaults?\s+on|bond\s+default|debt\s+restructur|"
         r"bankruptcy|liquidation|wind[-\s]up|going\s+concern|"
         r"loan\s+restructur|debenture\s+default|cdr\s+plan|sdr)\b",
         re.IGNORECASE),
     20, "policy"),
    ("fundraise",
     re.compile(
         r"\b(?:qip|preferential\s+(?:allotment|issue)|fpo|"
         r"rights\s+issue|board\s+approves?\s+(?:fund|raise)|"
         r"raises?\s+(?:₹|rs\.?|inr|usd|\$)\s*[\d.,]+\s*(?:cr|crore|bn|billion|mn|million))\b",
         re.IGNORECASE),
     16, "merger"),
    ("supply_chain",
     re.compile(
         r"\b(?:supply\s+(?:chain|disruption|shortage|crunch)|"
         r"shutdown|plant\s+(?:halt|closure|fire|shut)|"
         r"strike|production\s+halt|chip\s+shortage|raw\s+material\s+(?:shortage|spike))\b",
         re.IGNORECASE),
     16, "supply"),
    ("geopolitics",
     re.compile(
         r"\b(?:geopolit|war|conflict|sanctions?|embargo|"
         r"middle\s+east|israel|iran|russia|ukraine|"
         r"opec\s*\+?\s+(?:cuts?|raises?|meeting)|red\s+sea|strait\s+of\s+hormuz)\b",
         re.IGNORECASE),
     16, "supply"),
    ("management_change",
     re.compile(
         r"\b(?:(?:ceo|cfo|coo|cto|cio|md|managing\s+director|chairman|chairperson|"
         r"executive\s+director|whole[-\s]time\s+director|company\s+secretary)\s+"
         r"(?:resigns?|steps?\s+down|exits?|quits?|appointed|named|elevated|"
         r"to\s+(?:join|step\s+down|exit)|takes\s+over|takes\s+charge)|"
         r"appoints?\s+(?:[^\s]+\s+){0,5}(?:ceo|cfo|coo|cto|cio|md|chairman|"
         r"managing\s+director|head|chief)|"
         r"names?\s+(?:[^\s]+\s+){0,4}(?:as\s+)?(?:ceo|cfo|coo|cto|cio|md|chief)|"
         r"new\s+(?:ceo|cfo|coo|cto|cio|md|managing\s+director|chairman|chief)|"
         r"to\s+head|to\s+lead\s+(?:as|the)\s+(?:ceo|cfo|md|chief)|"
         r"hires?\s+(?:[^\s]+\s+){0,4}(?:as\s+)?(?:ceo|cfo|coo|cto|md|chief|head\s+of)|"
         r"auditor\s+resigns?|board\s+(?:reshuffle|reconstitut))\b",
         re.IGNORECASE),
     18, "insider"),
    ("ipo_listing",
     re.compile(
         r"\b(?:ipo\s+(?:opens?|closes?|subscrib|listing|allotment|gmp)|"
         r"listing\s+(?:debut|gain|premium)|drhp|red\s+herring\s+prospectus|"
         r"price\s+band|anchor\s+(?:book|investor|allotment))\b",
         re.IGNORECASE),
     16, "ipo"),
    ("dividend_action",
     re.compile(
         r"\b(?:interim\s+dividend|final\s+dividend|special\s+dividend|"
         r"record\s+date|ex[-\s]dividend|board\s+recommends?\s+dividend|"
         r"bonus\s+(?:issue|share)|stock\s+split|share\s+split)\b",
         re.IGNORECASE),
     12, "dividend"),
)


# ---------------------------------------------------------------------------
# Tier mapping
# ---------------------------------------------------------------------------

# quality_score thresholds for the impact_tier label. Tuned so the median
# RSS/Google News headline lands in "generic" and structured filings,
# concalls, M&A, and major policy action clear "high".
_TIER_THRESHOLDS = (
    (80, "critical"),
    (54, "high"),
    (40, "meaningful"),
    (20, "generic"),
    (0,  "noise"),
)

# Tier-1 entity mentions add a small confidence boost — when the agent of
# the headline is a regulator, exchange, or government body, the news is
# almost always institutionally relevant by definition.
_INSTITUTIONAL_ENTITY_RE = re.compile(
    r"\b(?:rbi|sebi|cci|nclt|nse|bse|cabinet|finance\s+ministry|"
    r"ministry\s+of\s+\w+|pib|sat|niti\s+aayog|gst\s+council|"
    r"enforcement\s+directorate|ed\s+raid|cbi|income\s+tax\s+department|"
    r"crisil|icra|moody'?s?|s&p|fitch|opec|fomc|fed\s+chair)\b",
    re.IGNORECASE,
)


@dataclass
class NewsQuality:
    impact_tier: str
    quality_score: int
    is_noise: bool
    kinds: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    market_implication: Optional[str] = None
    suggested_event_type: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "impact_tier": self.impact_tier,
            "quality_score": self.quality_score,
            "is_noise": self.is_noise,
            "kinds": list(self.kinds),
            "reasons": list(self.reasons),
            "market_implication": self.market_implication,
            "suggested_event_type": self.suggested_event_type,
        }


# Pre-canned market implications by kind. Short, neutral, terminal-style.
_IMPLICATION_HINTS = {
    "earnings_result":
        "Earnings print — watch margin trajectory and guidance for sector read-through.",
    "guidance":
        "Guidance revision drives forward EPS reset; multiple compression / expansion to follow.",
    "concall":
        "Management commentary — listen for demand colour, margin levers, and tone shift.",
    "order_win":
        "Order book accretion — supports revenue visibility; check execution timeline.",
    "m_and_a":
        "Corporate action — re-rate combined entity; watch dilution and synergy claims.",
    "capex":
        "Capacity build-out — backloaded earnings impact; near-term cash drag, medium-term operating leverage.",
    "policy":
        "Policy shift — sector-wide rerating catalyst; identify direct beneficiaries vs. losers.",
    "rate_action":
        "Rate decision — yield-curve and banking-sector implications; risk assets reprice.",
    "regulatory":
        "Regulatory action — overhang on the named entity; watch second-order sector impact.",
    "rating":
        "Credit-rating action — cost-of-funds reset for the issuer; bond spreads to confirm.",
    "promoter":
        "Promoter/insider activity — directional conviction signal; check holding trajectory.",
    "debt_stress":
        "Balance-sheet stress — equity holders absorb residual; refinancing pipeline critical.",
    "fundraise":
        "Capital raise — dilution offset by stated use of proceeds; check post-money runway.",
    "supply_chain":
        "Supply-side shock — input cost / volume disruption; downstream margin asymmetry.",
    "geopolitics":
        "Geopolitical event — risk-off rotation; commodities and defensives typically lead.",
    "management_change":
        "Leadership change — strategy continuity risk; watch for guidance reset.",
    "ipo_listing":
        "Primary market activity — read-through to peer comps and sector appetite.",
    "dividend_action":
        "Capital-return event — yield-driven holders react; thin operating signal.",
}


def _detect_kinds(text: str) -> Tuple[List[str], int, Optional[str]]:
    """Return (kinds, summed_quality_boost, first_event_type).

    Boosts are summed but capped to avoid one-headline saturation.
    """
    kinds: List[str] = []
    boost = 0
    first_event_type: Optional[str] = None
    for kind, pattern, w, event_type in _HIGH_IMPACT:
        if pattern.search(text):
            kinds.append(kind)
            boost += w
            if first_event_type is None:
                first_event_type = event_type
    # Soft cap: two strong kinds saturate at +44; further matches add only +4 each.
    if boost > 44:
        excess = boost - 44
        boost = 44 + min(excess // 4, 8)
    return kinds, boost, first_event_type


def _is_bare_move_headline(text: str) -> bool:
    """True when the headline is dominated by a move-verb with no explanation.

    A "bare move" looks like 'XYZ surges 5%' or 'ABC stock plunges' — no
    cause clause, no event keyword. These are pure price-action noise.
    """
    if not _MOVE_VERBS_RE.search(text):
        return False
    low = " " + text.lower() + " "
    if any(c in low for c in _CAUSE_INDICATORS):
        return False
    # Short bare-move headlines are the worst offenders.
    return len(text) < 110


def _matched_noise_pattern(text: str) -> Optional[str]:
    for pat in _NOISE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)[:60]
    return None


def _tier_for_score(score: int) -> str:
    for threshold, label in _TIER_THRESHOLDS:
        if score >= threshold:
            return label
    return "noise"


def classify(title: Optional[str] = "", summary: Optional[str] = "",
             source: Optional[str] = "", source_tier: Optional[int] = None) -> NewsQuality:
    """Classify a news item.

    Pure function — safe to call in tight loops. ~50µs per call on hot path.
    """
    title = (title or "").strip()
    summary = (summary or "").strip()
    blob = (title + "  " + summary).strip()
    if not blob:
        return NewsQuality(
            impact_tier="noise",
            quality_score=0,
            is_noise=True,
            reasons=["empty title and summary"],
        )

    reasons: List[str] = []

    # 1. Noise gate — promo / clickbait phrasing kills the score outright.
    noise_match = _matched_noise_pattern(blob)
    if noise_match:
        reasons.append(f"matched promo/clickbait phrase: '{noise_match}'")
        return NewsQuality(
            impact_tier="noise",
            quality_score=5,
            is_noise=True,
            reasons=reasons,
        )

    # 2. Detect high-impact kinds.
    kinds, boost, suggested_event_type = _detect_kinds(blob)

    # 3. Bare-move headline (move-verb + no cause + no high-impact kind) is noise.
    if not kinds and _is_bare_move_headline(title):
        reasons.append("bare move-only headline (no cause clause)")
        return NewsQuality(
            impact_tier="noise",
            quality_score=15,
            is_noise=True,
            reasons=reasons,
        )

    # 4. Base score: short, kind-less, sourceless headlines start low.
    base = 30
    # Tier-1 (regulator/exchange) sources are inherently institutional.
    if source_tier == 1:
        base += 20
    elif source_tier == 2:
        base += 8
    elif source_tier == 4:
        base -= 5

    # 5. Numeric content (₹, %, crore, bps, EPS) is a quality proxy.
    # Match Indian-style numbers with comma separators ("15,000 cr").
    if re.search(r"(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d+)?", blob, re.IGNORECASE):
        base += 6
        reasons.append("contains rupee value")
    if re.search(r"\b[\d,]+(?:\.\d+)?\s*(?:crore|cr|bn|billion|mn|million|lakh)\b",
                 blob, re.IGNORECASE):
        base += 6
        reasons.append("contains scale figure")
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:%|bps|basis\s+points?)\b", blob, re.IGNORECASE):
        base += 4
        reasons.append("contains percentage / bps figure")

    # 6. Length / substance proxy. Sub-40-char headlines are usually thin.
    # Cap the penalty: a short headline with a strong kind ("RBI cuts repo")
    # still deserves to clear the "high" tier on the kind boost alone.
    if len(blob) > 200:
        base += 4
    elif len(blob) < 40:
        base -= 4

    # 6b. Institutional-entity mention (regulator, exchange, ministry,
    # rating agency). Small boost — these names alone don't make news
    # high-impact, but combined with a kind detection they tilt it.
    if _INSTITUTIONAL_ENTITY_RE.search(blob):
        base += 4
        reasons.append("mentions institutional entity")

    # 7. High-impact kind boost.
    if kinds:
        base += boost
        reasons.append(f"high-impact kinds: {', '.join(kinds)}")

    score = max(0, min(100, base))
    tier = _tier_for_score(score)
    is_noise = tier == "noise"

    # 8. Market implication — pick the first detected kind's canned hint.
    implication = None
    if kinds:
        implication = _IMPLICATION_HINTS.get(kinds[0])

    return NewsQuality(
        impact_tier=tier,
        quality_score=score,
        is_noise=is_noise,
        kinds=kinds,
        reasons=reasons,
        market_implication=implication,
        suggested_event_type=suggested_event_type,
    )


def classify_dict(item: dict) -> NewsQuality:
    """Convenience wrapper for dict-shaped items (API rows, scraped events)."""
    return classify(
        title=item.get("title") or item.get("headline"),
        summary=item.get("summary") or item.get("description") or item.get("text"),
        source=item.get("source") or item.get("feed"),
        source_tier=item.get("source_tier") or item.get("tier"),
    )


__all__ = [
    "NewsQuality",
    "classify",
    "classify_dict",
]
