"""
Twitter/X collector — Nitter-first, snscrape fallback, optional X API v2 basic.

Rotates through a pool of Nitter instances. On rate-limits or failures,
advances to the next instance; on persistent failure of all instances, falls
back to snscrape (if installed). If X_BEARER_TOKEN is set we skip scraping and
use the paid API.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Iterable, List, Optional
from urllib.parse import quote_plus

from net import request_with_retry, CircuitOpen
from ratelimit import get_bucket
from metrics import inc
from social.common import build_post, extract_tickers

logger = logging.getLogger(__name__)

DEFAULT_NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe",
    "https://nitter.unixfox.eu",
]

DEFAULT_HANDLES = [
    "CNBCTV18News",
    "moneycontrolcom",
    "ETMarkets",
    "livemint",
    "NSEIndia",
    "BSEIndia",
    "RBI",
    "SEBI_India",
]


def _parse_nitter_html(html: str, handle: str) -> List[dict]:
    """Very forgiving HTML parser for Nitter timelines.

    Nitter HTML structure is stable enough that a regex over tweet containers
    works without a full parser dependency. We return raw tweet dicts keyed on
    (tweet_id, text, posted_at, url).
    """
    posts: List[dict] = []
    # Each tweet: <div class="timeline-item"> ... <a class="tweet-link" href="/<handle>/status/<id>"> ...
    # Nitter puts text in <div class="tweet-content ...">TEXT</div>
    tweet_blocks = re.findall(
        r'<div class="timeline-item[^"]*">(.*?)</div>\s*</div>\s*</div>',
        html,
        flags=re.DOTALL,
    )
    for block in tweet_blocks:
        m_id = re.search(r'href="/[^"]+/status/(\d+)"', block)
        m_text = re.search(r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>', block, flags=re.DOTALL)
        m_ts = re.search(r'<span class="tweet-date"><a[^>]*title="([^"]+)"', block)
        if not (m_id and m_text):
            continue
        text = re.sub(r"<[^>]+>", " ", m_text.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        try:
            ts = datetime.strptime(m_ts.group(1), "%b %d, %Y · %I:%M %p %Z") if m_ts else datetime.now(timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        posts.append({"id": m_id.group(1), "text": text, "posted_at": ts, "url": f"https://twitter.com/{handle}/status/{m_id.group(1)}"})
    return posts


class TwitterCollector:
    def __init__(self, known_tickers: Iterable[str], handles: Optional[List[str]] = None):
        self.known_tickers = list(known_tickers)
        env_handles = os.getenv("X_HANDLES") or os.getenv("TWITTER_HANDLES")
        if env_handles:
            self.handles = [h.strip().lstrip("@") for h in env_handles.split(",") if h.strip()]
        else:
            self.handles = handles or DEFAULT_HANDLES
        self.instances = [
            i.strip().rstrip("/")
            for i in os.getenv("NITTER_INSTANCES", ",".join(DEFAULT_NITTER_INSTANCES)).split(",")
            if i.strip()
        ]
        self._seen: set[str] = set()

    @property
    def available(self) -> bool:
        # Even without X API, we can still try Nitter + snscrape
        return True

    def _collect_via_x_api(self, handle: str) -> List[dict]:
        token = os.getenv("X_BEARER_TOKEN", "")
        if not token:
            return []
        try:
            import requests

            resp = requests.get(
                f"https://api.twitter.com/2/users/by/username/{handle}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.status_code != 200:
                return []
            uid = resp.json()["data"]["id"]
            r2 = requests.get(
                f"https://api.twitter.com/2/users/{uid}/tweets",
                headers={"Authorization": f"Bearer {token}"},
                params={"max_results": 20, "tweet.fields": "created_at"},
                timeout=10,
            )
            if r2.status_code != 200:
                return []
            out = []
            for t in r2.json().get("data", []):
                out.append({
                    "id": t["id"],
                    "text": t["text"],
                    "posted_at": datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")),
                    "url": f"https://twitter.com/{handle}/status/{t['id']}",
                })
            return out
        except Exception as exc:
            logger.debug("x api fetch failed handle=%s err=%s", handle, exc)
            return []

    def _collect_via_nitter(self, handle: str) -> List[dict]:
        bucket = get_bucket()
        for inst in self.instances:
            if not bucket.acquire("nitter", 1.0, max_wait=5.0):
                inc("scraper_ratelimit_skips_total", source="nitter")
                continue
            try:
                resp = request_with_retry(
                    "GET",
                    f"{inst}/{handle}",
                    source="nitter",
                    attempts=2,
                    timeout=12,
                    headers={"User-Agent": "Mozilla/5.0 alphaevent/0.1"},
                )
                if resp.status_code != 200:
                    continue
                return _parse_nitter_html(resp.text, handle)
            except CircuitOpen:
                break
            except Exception as exc:
                inc("scraper_social_errors_total", platform="nitter")
                logger.debug("nitter %s failed: %s", inst, exc)
                continue
        return []

    def _collect_via_snscrape(self, handle: str) -> List[dict]:
        try:
            import snscrape.modules.twitter as sntwitter  # type: ignore

            scraper = sntwitter.TwitterUserScraper(handle)
            out: List[dict] = []
            for i, tweet in enumerate(scraper.get_items()):
                if i >= 20:
                    break
                out.append({
                    "id": str(tweet.id),
                    "text": tweet.rawContent or tweet.content,
                    "posted_at": tweet.date,
                    "url": tweet.url,
                })
            return out
        except Exception as exc:
            logger.debug("snscrape failed handle=%s err=%s", handle, exc)
            return []

    def collect(self) -> List[dict]:
        posts: List[dict] = []
        for handle in self.handles:
            raw = self._collect_via_x_api(handle) or self._collect_via_nitter(handle) or self._collect_via_snscrape(handle)
            for t in raw:
                if t["id"] in self._seen:
                    continue
                self._seen.add(t["id"])
                tickers = extract_tickers(t["text"], self.known_tickers)
                if not tickers:
                    continue
                posts.append(build_post(
                    platform="twitter",
                    handle=handle,
                    text=t["text"],
                    url=t["url"],
                    posted_at=t["posted_at"],
                    tickers=tickers,
                    score=1.0,
                ))
                inc("scraper_social_posts_total", platform="twitter")
            time.sleep(0.25)  # gentle pacing even past the bucket
        logger.info("twitter collected %d posts across %d handles", len(posts), len(self.handles))
        return posts
