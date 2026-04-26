"""
Telegram public-web scraper — no API credentials required.

Telegram exposes a public web view at https://t.me/s/<channel> that renders
recent messages as HTML. This works for any public channel and doesn't need
api_id/api_hash. Perfect for read-only monitoring of stock-news channels.

Trade-offs vs Telethon:
  + No credentials, no phone number, no session file
  + Works behind corporate networks
  + Survives flood-wait because it's just HTTP GET
  − No real-time streaming (we poll)
  − Only public channels (can't see private)
  − Only the last ~20 messages per poll

Curated channel list is in DEFAULT_CHANNELS below. Override with env
TELEGRAM_WEB_CHANNELS (comma-separated, with or without @).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from net import request_with_retry, CircuitOpen
from ratelimit import get_bucket
from metrics import inc
from social.common import build_post, extract_tickers

logger = logging.getLogger(__name__)

# Curated public Indian stock market Telegram channels.
# These are well-known broadcast channels — easy to add/remove via env.
DEFAULT_CHANNELS = [
    "StockMarketNSE",
    "nsebseindia",
    "sharemarketnews",
    "NiftyBankNifty50",
    "StockMarketNewsIndia",
    "bseindiaofficial",
    "ETMarkets",
    "moneycontrolcom",
    "CNBCTV18News",
    "livemintoff",
]


# ---------- HTML parsing ----------

# Each message block:
# <div class="tgme_widget_message_wrap ..."><div class="tgme_widget_message ..." data-post="CHANNEL/ID">
#   <div class="tgme_widget_message_text js-message_text" ...>TEXT</div>
#   <a class="tgme_widget_message_date" href="..."><time datetime="ISO">...</time></a>
# </div></div>
_MSG_BLOCK_RE = re.compile(
    r'<div class="tgme_widget_message[^"]*"[^>]*data-post="([^"]+)"(.*?)(?=<div class="tgme_widget_message_wrap|</section>)',
    re.DOTALL,
)
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_TAG_STRIP = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    return _WHITESPACE_RE.sub(" ", _TAG_STRIP.sub(" ", html)).strip()


def parse_channel_html(html: str, channel: str) -> List[dict]:
    """Extract structured message dicts from a t.me/s/<channel> page."""
    out: List[dict] = []
    if not html:
        return out
    for m in _MSG_BLOCK_RE.finditer(html):
        post_id = m.group(1)  # "channel/id"
        body = m.group(2)
        text_m = _TEXT_RE.search(body)
        if not text_m:
            continue
        text = _strip_html(text_m.group(1))
        if not text or len(text) < 10:
            continue
        time_m = _TIME_RE.search(body)
        try:
            posted_at = (
                datetime.fromisoformat(time_m.group(1)) if time_m
                else datetime.now(timezone.utc)
            )
        except Exception:
            posted_at = datetime.now(timezone.utc)
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        out.append({
            "id": post_id,
            "text": text,
            "posted_at": posted_at,
            "url": f"https://t.me/{post_id}",
        })
    return out


class TelegramWebCollector:
    """Polls t.me/s/<channel> for each curated channel. No auth needed."""

    def __init__(self, known_tickers: Iterable[str], channels: Optional[List[str]] = None):
        self.known_tickers = list(known_tickers)
        env = os.getenv("TELEGRAM_WEB_CHANNELS", "")
        if env:
            self.channels = [c.strip().lstrip("@") for c in env.split(",") if c.strip()]
        else:
            self.channels = channels or DEFAULT_CHANNELS
        self._seen: set[str] = set()

    @property
    def available(self) -> bool:
        return True  # Always available — no creds needed

    def _fetch(self, channel: str) -> str:
        bucket = get_bucket()
        if not bucket.acquire("telegram", 1.0, max_wait=5.0):
            inc("scraper_ratelimit_skips_total", source="telegram_web")
            return ""
        try:
            resp = request_with_retry(
                "GET",
                f"https://t.me/s/{channel}",
                source="telegram_web",
                attempts=2,
                timeout=12,
                headers={"User-Agent": "Mozilla/5.0 alphaevent/0.1"},
            )
            if resp.status_code != 200:
                return ""
            return resp.text
        except CircuitOpen:
            return ""
        except Exception as exc:
            logger.debug("telegram_web fetch failed channel=%s err=%s", channel, exc)
            inc("scraper_social_errors_total", platform="telegram_web")
            return ""

    def collect(self) -> List[dict]:
        posts: List[dict] = []
        for channel in self.channels:
            html = self._fetch(channel)
            if not html:
                continue
            messages = parse_channel_html(html, channel)
            for msg in messages:
                if msg["id"] in self._seen:
                    continue
                self._seen.add(msg["id"])
                tickers = extract_tickers(msg["text"], self.known_tickers)
                # Keep even ticker-less messages for breaking-news detection —
                # they'll be surfaced in news-split "buzz" feed but won't generate signals.
                post = build_post(
                    platform="telegram",
                    handle=channel,
                    text=msg["text"],
                    url=msg["url"],
                    posted_at=msg["posted_at"],
                    tickers=tickers,
                    score=1.0,
                )
                posts.append(post)
                inc("scraper_social_posts_total", platform="telegram_web")
        logger.info("telegram_web collected %d posts across %d channels", len(posts), len(self.channels))
        return posts
