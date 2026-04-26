"""
Reddit JSON scraper — no OAuth required.

Reddit exposes unauthenticated JSON at
    https://www.reddit.com/r/<sub>/new.json?limit=N
with just a custom User-Agent. This is rate-limited (60 req/min unauth) but
plenty for a handful of subs polled every few minutes.

Trade-offs vs PRAW:
  + No CLIENT_ID/CLIENT_SECRET/user auth required
  + Works anywhere HTTP works
  + Simple to debug (just a GET)
  − Lower rate limit than OAuth
  − Fewer fields (no private metadata)
  − Can 403 if Reddit thinks you're a bot — we rotate User-Agent

If REDDIT_CLIENT_ID is set, RedditCollector (PRAW) is preferred. This module
is the fallback that runs when no creds are present.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from net import request_with_retry, CircuitOpen
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
    "IndianStreet",
    "IndianStockAnalysis",
]

# Rotate UAs so Reddit doesn't tag us as a scraper
_USER_AGENTS = [
    "alphaevent:v0.2 (by /u/anon)",
    "Mozilla/5.0 alphaevent-newsbot/0.2",
    "AlphaEvent Research Bot 0.2",
]


class RedditJsonCollector:
    """Polls subreddit .json endpoints without OAuth."""

    def __init__(self, known_tickers: Iterable[str], subs: Optional[List[str]] = None):
        self.known_tickers = list(known_tickers)
        env = os.getenv("REDDIT_SUBS", "")
        if env:
            self.subs = [s.strip() for s in env.split(",") if s.strip()]
        else:
            self.subs = subs or DEFAULT_SUBS
        self._seen_ids: set[str] = set()

    @property
    def available(self) -> bool:
        return True  # Always available

    def _fetch(self, sub: str, limit: int = 25) -> List[dict]:
        bucket = get_bucket()
        if not bucket.acquire("reddit", 1.0, max_wait=5.0):
            inc("scraper_ratelimit_skips_total", source="reddit_json")
            return []
        ua = random.choice(_USER_AGENTS)
        try:
            resp = request_with_retry(
                "GET",
                f"https://www.reddit.com/r/{sub}/new.json",
                source="reddit",
                attempts=2,
                timeout=12,
                params={"limit": limit, "raw_json": 1},
                headers={"User-Agent": ua, "Accept": "application/json"},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            children = data.get("data", {}).get("children", [])
            return [c.get("data", {}) for c in children]
        except CircuitOpen:
            return []
        except json.JSONDecodeError as exc:
            logger.debug("reddit json parse failed sub=%s err=%s", sub, exc)
            return []
        except Exception as exc:
            logger.debug("reddit json fetch failed sub=%s err=%s", sub, exc)
            inc("scraper_social_errors_total", platform="reddit_json")
            return []

    def collect(self, limit_per_sub: int = 25) -> List[dict]:
        posts: List[dict] = []
        for sub in self.subs:
            raw_posts = self._fetch(sub, limit=limit_per_sub)
            for rp in raw_posts:
                pid = rp.get("id") or rp.get("name")
                if not pid or pid in self._seen_ids:
                    continue
                self._seen_ids.add(pid)

                title = rp.get("title", "") or ""
                selftext = rp.get("selftext", "") or ""
                text = f"{title}\n{selftext}".strip()

                tickers = extract_tickers(text, self.known_tickers)
                # Keep ticker-less posts as generic buzz (filtered at rendering time if needed)
                age_hours = max(
                    (time.time() - (rp.get("created_utc") or time.time())) / 3600.0,
                    0.1,
                )
                score_value = (rp.get("score") or 0) + (rp.get("num_comments") or 0) * 2
                velocity = score_value / age_hours

                posted_at = datetime.fromtimestamp(
                    rp.get("created_utc") or time.time(), tz=timezone.utc
                )
                permalink = rp.get("permalink") or ""
                url = f"https://reddit.com{permalink}" if permalink else (rp.get("url") or "")

                post = build_post(
                    platform="reddit",
                    handle=f"r/{sub}",
                    text=text,
                    url=url,
                    posted_at=posted_at,
                    tickers=tickers,
                    score=velocity,
                )
                post["author"] = rp.get("author") or "unknown"
                post["upvotes"] = rp.get("score") or 0
                post["num_comments"] = rp.get("num_comments") or 0
                posts.append(post)
                inc("scraper_social_posts_total", platform="reddit_json")
            time.sleep(0.3)  # polite pacing
        logger.info("reddit_json collected %d posts across %d subs", len(posts), len(self.subs))
        return posts
