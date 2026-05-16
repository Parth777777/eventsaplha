"""
Source → news_type classifier.

Every event/signal carries a `news_type` field:
  - "news_article" — traditional journalism: RSS, Google News, NewsAPI, IR pages, BSE filings
  - "social_buzz"  — Reddit, Twitter/X, Telegram, YouTube comments (future)
  - "filing"       — official exchange/company filings (BSE, NSE, SEBI)
  - "unknown"      — source not classified

The frontend uses this to split the feed into two streams so retail can
distinguish between "someone wrote an article" and "the crowd is talking."
"""

from __future__ import annotations

SOCIAL_PREFIXES = ("reddit", "twitter", "telegram", "x:", "nitter")
FILING_PREFIXES = ("bse_ann", "bse:", "nse:", "sebi:", "ir:", "filing:")
NEWS_OUTLETS = {
    "et_markets", "et_stocks", "mint_markets", "mint_companies",
    "moneycontrol_markets", "moneycontrol_stocks", "bs_markets",
    "newsapi", "google_news", "rbi_press",
    "pib_releases", "pib_features", "mof_press", "pmo_press",
}


def classify(source: str) -> str:
    """Classify a source string into one of the four news_type buckets."""
    if not source:
        return "unknown"
    s = source.lower()
    if any(s.startswith(p) for p in SOCIAL_PREFIXES):
        return "social_buzz"
    if any(s.startswith(p) for p in FILING_PREFIXES):
        return "filing"
    if s == "bse_announcements":
        return "filing"
    if s in NEWS_OUTLETS:
        return "news_article"
    # Heuristic: outlet names usually contain letters only + common separators
    if ":" in s:  # platform:handle-style id is social
        return "social_buzz"
    return "news_article"


def label(news_type: str) -> str:
    """Human-readable label for the UI."""
    return {
        "news_article": "News article",
        "social_buzz": "Social buzz",
        "filing": "Official filing",
        "unknown": "Unknown",
    }.get(news_type, "Unknown")


def badge_color(news_type: str) -> str:
    """Returns a hex color suitable for a UI badge."""
    return {
        "news_article": "#8eb4e0",
        "social_buzz": "#f2a96b",
        "filing": "#2dd4aa",
        "unknown": "#9aa0aa",
    }.get(news_type, "#9aa0aa")
