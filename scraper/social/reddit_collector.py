"""
Reddit collector — PRAW-backed, polls curated Indian investing subs.

Skips if PRAW or credentials aren't configured; never kills the scraper.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterable, List, Optional

try:
    import praw  # type: ignore
    from prawcore.exceptions import PrawcoreException  # type: ignore

    PRAW_AVAILABLE = True
except Exception:  # pragma: no cover
    praw = None
    PrawcoreException = Exception
    PRAW_AVAILABLE = False

from ratelimit import get_bucket
from metrics import inc
from social.common import build_post, extract_tickers

logger = logging.getLogger(__name__)

DEFAULT_SUBS = [
    "IndianStockMarket",
    "IndiaInvestments",
    "StockMarketIndia",
    "DalalStreetTalks",
    "IndianStreetBets",
]


class RedditCollector:
    def __init__(self, known_tickers: Iterable[str], subs: Optional[List[str]] = None):
        self.known_tickers = list(known_tickers)
        self.subs = subs or [s.strip() for s in os.getenv("REDDIT_SUBS", ",".join(DEFAULT_SUBS)).split(",") if s.strip()]
        self._reddit = None
        self._seen_ids: set[str] = set()

    @property
    def available(self) -> bool:
        if not PRAW_AVAILABLE:
            return False
        return bool(os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET"))

    def _client(self):
        if self._reddit is not None or not self.available:
            return self._reddit
        try:
            self._reddit = praw.Reddit(  # type: ignore[call-arg]
                client_id=os.getenv("REDDIT_CLIENT_ID"),
                client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
                user_agent=os.getenv("REDDIT_USER_AGENT", "alphaevent-scraper/0.1"),
                check_for_async=False,
            )
            self._reddit.read_only = True
        except Exception as exc:
            logger.error("reddit client init failed: %s", exc)
            self._reddit = None
        return self._reddit

    def collect(self, limit_per_sub: int = 25) -> List[dict]:
        if not self.available:
            logger.debug("reddit collector disabled (no praw or credentials)")
            return []
        client = self._client()
        if client is None:
            return []

        bucket = get_bucket()
        posts: List[dict] = []
        for sub in self.subs:
            if not bucket.acquire("reddit", 1.0, max_wait=5.0):
                inc("scraper_ratelimit_skips_total", source="reddit")
                logger.debug("reddit bucket empty, skipping %s", sub)
                continue
            try:
                for submission in client.subreddit(sub).new(limit=limit_per_sub):
                    if submission.id in self._seen_ids:
                        continue
                    self._seen_ids.add(submission.id)
                    text = f"{submission.title}\n{submission.selftext or ''}".strip()
                    tickers = extract_tickers(text, self.known_tickers)
                    if not tickers:
                        continue  # filter noise — only posts mentioning monitored tickers
                    age_hours = max((time.time() - submission.created_utc) / 3600.0, 0.1)
                    velocity = submission.score / age_hours
                    post = build_post(
                        platform="reddit",
                        handle=f"r/{sub}",
                        text=text,
                        url=f"https://reddit.com{submission.permalink}",
                        posted_at=datetime.fromtimestamp(submission.created_utc, tz=timezone.utc),
                        tickers=tickers,
                        score=velocity,
                    )
                    post["author"] = str(submission.author) if submission.author else "unknown"
                    post["upvotes"] = submission.score
                    post["num_comments"] = submission.num_comments
                    posts.append(post)
                    inc("scraper_social_posts_total", platform="reddit")
            except PrawcoreException as exc:
                inc("scraper_social_errors_total", platform="reddit")
                logger.warning("reddit error sub=%s err=%s", sub, exc)
            except Exception as exc:  # pragma: no cover
                inc("scraper_social_errors_total", platform="reddit")
                logger.warning("reddit unexpected sub=%s err=%s", sub, exc)
        logger.info("reddit collected %d posts across %d subs", len(posts), len(self.subs))
        return posts
