"""Source tiering, content-hash dedup, freshness decay, cross-source confirmation.

Plug-in module for hybrid_scraper. Apply BEFORE alpha scoring so weights flow
through the rest of the pipeline.

Design:
- Tier 1 (1.0): regulators / exchanges — BSE/NSE/SEBI/RBI/MCA filings
- Tier 2 (0.85): major financial press — ET, Mint, Reuters, Bloomberg, Moneycontrol
- Tier 3 (0.6):  aggregators — Google News, NewsAPI, Yahoo
- Tier 4 (0.3):  unknown / blogs / tipster sites

A signal that fires above alpha=70 must have either:
  - 1 Tier-1 source, OR
  - 2 independent Tier-1/2 sources within CONFIRM_WINDOW_HOURS
Otherwise it is downgraded to "watch" status and capped at alpha=65.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


CONFIRM_WINDOW_HOURS = 6
FRESHNESS_HALF_LIFE_HOURS = 8.0       # news weight = exp(-age/8)
FILING_HALF_LIFE_HOURS = 72.0          # filings decay slower


# ---- TIER MAP ---------------------------------------------------------------

# Substring patterns to source name OR URL host. Order matters: first match wins.
_TIER_RULES: List[Tuple[str, int, str]] = [
    # Tier 1 - regulators/exchanges (highest authority)
    ("bseindia", 1, "BSE"),
    ("bse_india", 1, "BSE"),
    ("nseindia", 1, "NSE"),
    ("nse_india", 1, "NSE"),
    ("sebi.gov", 1, "SEBI"),
    ("rbi.org", 1, "RBI"),
    ("mca.gov", 1, "MCA"),
    ("dipp.gov", 1, "DIPP"),

    # Tier 2 - major financial press
    ("reuters", 2, "Reuters"),
    ("bloomberg", 2, "Bloomberg"),
    ("economictimes", 2, "ET"),
    ("livemint", 2, "Mint"),
    ("moneycontrol", 2, "Moneycontrol"),
    ("business-standard", 2, "BusinessStandard"),
    ("thehindubusinessline", 2, "HinduBL"),
    ("financialexpress", 2, "FinancialExpress"),
    ("ndtvprofit", 2, "NDTVProfit"),
    ("ft.com", 2, "FT"),
    ("wsj.com", 2, "WSJ"),
    ("cnbc", 2, "CNBC"),

    # Tier 3 - aggregators
    ("google_news", 3, "GoogleNews"),
    ("news.google", 3, "GoogleNews"),
    ("newsapi", 3, "NewsAPI"),
    ("yahoo", 3, "Yahoo"),
    ("zerodha", 3, "Zerodha"),
]


@dataclass
class SourceMeta:
    tier: int
    weight: float
    canonical: str  # normalized source name


def _tier_to_weight(tier: int) -> float:
    return {1: 1.0, 2: 0.85, 3: 0.6, 4: 0.3}.get(tier, 0.3)


def classify_source(source_name: str, link: str = "") -> SourceMeta:
    """Classify a source by tier. Looks at source_name and the URL host."""
    haystack = (source_name or "").lower()
    if link:
        try:
            haystack += " " + (urlparse(link).hostname or "").lower()
        except Exception:
            pass

    for needle, tier, canonical in _TIER_RULES:
        if needle in haystack:
            return SourceMeta(tier=tier, weight=_tier_to_weight(tier), canonical=canonical)
    return SourceMeta(tier=4, weight=_tier_to_weight(4), canonical=source_name or "unknown")


# ---- CONTENT-HASH DEDUP -----------------------------------------------------

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Used for content hash."""
    if not text:
        return ""
    t = text.lower()
    t = _PUNCT_RE.sub(" ", t)
    return " ".join(t.split())


def content_hash(title: str, summary: str = "", chars: int = 500) -> str:
    """SHA1 of normalized (title + first N chars of summary).

    More robust than title-only dedup: catches syndicated mirrors that change
    the headline trivially but keep the body identical.
    """
    base = _normalize((title or "") + " " + (summary or ""))[:chars]
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def shingle_set(text: str, k: int = 4) -> set:
    """K-shingles of token n-grams for near-duplicate detection."""
    tokens = _TOKEN_RE.findall(_normalize(text))
    if len(tokens) < k:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i:i + k]) for i in range(len(tokens) - k + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


# ---- FRESHNESS DECAY --------------------------------------------------------

def _parse_age_hours(published: Optional[str]) -> float:
    """Return age in hours; 0 if unparseable (treat as fresh)."""
    if not published:
        return 0.0
    try:
        # Try ISO first
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except Exception:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(published)
        except Exception:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = (now - dt).total_seconds() / 3600.0
    return max(0.0, delta)


def freshness_weight(published: Optional[str], is_filing: bool = False) -> float:
    """Exponential decay: weight = exp(-age_hours / half_life). Range (0, 1]."""
    half = FILING_HALF_LIFE_HOURS if is_filing else FRESHNESS_HALF_LIFE_HOURS
    age = _parse_age_hours(published)
    return math.exp(-age / half)


def freshness_label(published: Optional[str]) -> str:
    """Human-readable freshness band for UI badges."""
    age = _parse_age_hours(published)
    if age < 1:
        return "breaking"      # <1h
    if age < 6:
        return "fresh"         # 1-6h
    if age < 24:
        return "today"
    if age < 72:
        return "recent"
    return "stale"


# ---- CROSS-SOURCE CONFIRMATION ---------------------------------------------

@dataclass
class ClusteredEvent:
    """An event that has been clustered across sources by content similarity."""
    canonical_title: str
    sources: List[Dict] = field(default_factory=list)  # [{name, tier, link, published}]
    earliest_ts: float = 0.0
    latest_ts: float = 0.0
    content_key: str = ""

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def distinct_tier_count(self) -> int:
        """Distinct sources counting same-domain duplicates only once."""
        seen = set()
        for s in self.sources:
            seen.add(s.get("canonical") or s.get("name"))
        return len(seen)

    @property
    def best_tier(self) -> int:
        return min((s["tier"] for s in self.sources), default=4)

    @property
    def velocity_per_hour(self) -> float:
        """Articles per hour over the cluster's lifespan."""
        span = max(0.25, (self.latest_ts - self.earliest_ts) / 3600.0)
        return self.source_count / span

    def confirmation_status(self) -> str:
        """Returns one of: 'tier1', 'confirmed', 'single', 'unconfirmed'.

        - tier1:        any Tier-1 source present (treat as confirmed)
        - confirmed:    >=2 distinct Tier-1/2 sources within CONFIRM_WINDOW_HOURS
        - single:       1 Tier-1/2 source
        - unconfirmed:  only Tier 3-4 sources
        """
        if self.best_tier == 1:
            return "tier1"
        major = [s for s in self.sources if s["tier"] <= 2]
        major_distinct = len({s.get("canonical") for s in major})
        if major_distinct >= 2:
            window_secs = CONFIRM_WINDOW_HOURS * 3600
            if (self.latest_ts - self.earliest_ts) <= window_secs:
                return "confirmed"
        if major_distinct == 1:
            return "single"
        return "unconfirmed"


def cluster_events(articles: Iterable[Dict], jaccard_threshold: float = 0.15,
                   shingle_k: int = 1) -> List[ClusteredEvent]:
    """Cluster articles into events via title shingle similarity.

    Articles are dicts with at least: title, source, link, published.
    Returns list of ClusteredEvent. Each cluster represents one *real-world event*.
    """
    clusters: List[ClusteredEvent] = []
    cluster_shingles: List[set] = []

    for art in articles:
        title = art.get("title", "")
        published = art.get("published") or art.get("timestamp") or ""
        try:
            ts = _parse_iso_timestamp(published)
        except Exception:
            ts = time.time()
        meta = classify_source(art.get("source", ""), art.get("link", ""))
        sh = shingle_set(title, k=shingle_k)

        best_idx, best_sim = -1, 0.0
        for i, existing in enumerate(cluster_shingles):
            sim = jaccard(sh, existing)
            if sim > best_sim:
                best_sim = sim
                best_idx = i

        if best_sim >= jaccard_threshold and best_idx >= 0:
            cl = clusters[best_idx]
            cl.sources.append({
                "name": art.get("source", ""),
                "canonical": meta.canonical,
                "tier": meta.tier,
                "weight": meta.weight,
                "link": art.get("link", ""),
                "published": published,
                "ts": ts,
            })
            cl.earliest_ts = min(cl.earliest_ts, ts)
            cl.latest_ts = max(cl.latest_ts, ts)
            # Intentionally do NOT union shingles into the cluster — match against
            # the centroid (first article's tokens) only. Prevents drift where
            # successively-merged clusters become too broad and start absorbing
            # unrelated stories that share a couple of generic tokens.
        else:
            cl = ClusteredEvent(
                canonical_title=title,
                content_key=content_hash(title, art.get("summary", "")),
                earliest_ts=ts,
                latest_ts=ts,
            )
            cl.sources.append({
                "name": art.get("source", ""),
                "canonical": meta.canonical,
                "tier": meta.tier,
                "weight": meta.weight,
                "link": art.get("link", ""),
                "published": published,
                "ts": ts,
            })
            clusters.append(cl)
            cluster_shingles.append(sh)

    return clusters


def _parse_iso_timestamp(s: str) -> float:
    """Best-effort timestamp parse. Returns epoch seconds."""
    if not s:
        return time.time()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return time.time()


# ---- COMPOSITE EVENT WEIGHT -------------------------------------------------

def composite_event_weight(
    cluster: ClusteredEvent,
    is_filing: bool = False,
) -> Dict:
    """Return final weight + diagnostics for downstream alpha scoring.

    weight = best_source_weight * freshness * confirmation_multiplier
    """
    src_weight = max((s["weight"] for s in cluster.sources), default=0.3)

    latest_pub = max((s.get("published") for s in cluster.sources), default="")
    fresh = freshness_weight(latest_pub, is_filing=is_filing)

    status = cluster.confirmation_status()
    confirm_mult = {
        "tier1": 1.10,
        "confirmed": 1.05,
        "single": 1.00,
        "unconfirmed": 0.55,   # heavy penalty — single tipster source
    }.get(status, 1.0)

    weight = src_weight * fresh * confirm_mult

    return {
        "weight": round(weight, 4),
        "source_weight": round(src_weight, 3),
        "freshness": round(fresh, 3),
        "freshness_label": freshness_label(latest_pub),
        "confirmation": status,
        "confirmation_multiplier": confirm_mult,
        "source_count": cluster.source_count,
        "distinct_sources": cluster.distinct_tier_count,
        "best_tier": cluster.best_tier,
        "velocity_per_hour": round(cluster.velocity_per_hour, 2),
        "is_high_velocity": cluster.velocity_per_hour >= 3.0,
    }


def alpha_cap_for_status(status: str) -> Optional[float]:
    """Hard cap on alpha for unconfirmed events. None = no cap."""
    if status == "unconfirmed":
        return 65.0
    return None
