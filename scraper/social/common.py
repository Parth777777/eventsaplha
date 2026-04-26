"""
Shared helpers for social collectors.

Each collector emits "social posts" that the main pipeline treats as candidate
events. The canonical post dict shape is defined here so downstream
classification/alpha-scoring works identically to RSS events.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)


STOPWORDS: Set[str] = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "this", "that", "these", "those", "it", "its",
    "he", "she", "they", "we", "you", "i", "me", "my", "our", "their",
    "will", "would", "could", "should", "may", "might", "can", "do", "does",
    "did", "not", "no", "but", "if", "else", "than", "so", "up", "down",
}


def normalize_headline(text: str) -> str:
    """Lowercase, strip non-alphanumerics, drop stopwords, sort tokens.

    Used to cluster near-duplicate headlines across sources.
    """
    if not text:
        return ""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    tokens = [t for t in cleaned.split() if len(t) > 2 and t not in STOPWORDS]
    tokens = sorted(set(tokens))
    return " ".join(tokens)


def cluster_hash(ticker: Optional[str], normalized_headline: str) -> str:
    """Stable hash for clustering posts across sources."""
    key = f"{(ticker or '').upper()}|{normalized_headline}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def extract_tickers(text: str, known_tickers: Iterable[str]) -> List[str]:
    """Find any known tickers mentioned in the text.

    Matches $TICKER, #TICKER, and whole-word TICKER. Case-insensitive.
    """
    if not text:
        return []
    tickers = set()
    upper_text = text.upper()
    for t in known_tickers:
        if not t:
            continue
        # Whole-word match
        if re.search(rf"(?:^|[^A-Z0-9]){re.escape(t)}(?:[^A-Z0-9-]|$)", upper_text):
            tickers.add(t)
    return sorted(tickers)


def build_post(
    *,
    platform: str,
    handle: str,
    text: str,
    url: str,
    posted_at: datetime,
    tickers: List[str],
    score: float = 0.0,
) -> Dict:
    """Build the canonical social-post event dict.

    Every social post is tagged news_type="social_buzz" so downstream pipelines
    and the frontend can split it from traditional news articles.
    """
    norm = normalize_headline(text)
    primary_ticker = tickers[0] if tickers else None
    return {
        "platform": platform,
        "handle": handle,
        "source": f"{platform}:{handle}",
        "news_type": "social_buzz",
        "text": text[:2000],
        "title": text.split("\n", 1)[0][:280],
        "url": url,
        "link": url,
        "published_at": posted_at.isoformat(),
        "tickers": tickers,
        "primary_ticker": primary_ticker,
        "score": score,
        "normalized_headline": norm,
        "cluster_hash": cluster_hash(primary_ticker, norm),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
