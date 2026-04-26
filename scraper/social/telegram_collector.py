"""
Telegram collector — telethon user-session listener for public channels.

Because telethon maintains a persistent session, this collector is designed to
run as a long-lived thread started alongside the main scraper process. For
batch-mode scrapes (every N minutes) we snapshot the channel history since the
last poll instead of streaming.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

try:
    from telethon.sync import TelegramClient  # type: ignore
    from telethon.errors import FloodWaitError  # type: ignore

    TELETHON_AVAILABLE = True
except Exception:  # pragma: no cover
    TelegramClient = None
    FloodWaitError = Exception
    TELETHON_AVAILABLE = False

from metrics import inc
from social.common import build_post, extract_tickers

logger = logging.getLogger(__name__)

DEFAULT_CHANNELS = [
    # These are placeholders — real channel list should be configured via env
    "@StockMarketNSE",
    "@IndiaStockUpdates",
]


class TelegramCollector:
    def __init__(self, known_tickers: Iterable[str], channels: Optional[List[str]] = None):
        self.known_tickers = list(known_tickers)
        self.api_id = os.getenv("TELEGRAM_API_ID", "")
        self.api_hash = os.getenv("TELEGRAM_API_HASH", "")
        self.session = os.getenv("TELEGRAM_SESSION", "alphaevent")
        env_ch = os.getenv("TELEGRAM_CHANNELS", "")
        if env_ch:
            self.channels = [c.strip() for c in env_ch.split(",") if c.strip()]
        else:
            self.channels = channels or DEFAULT_CHANNELS
        self._last_poll: dict[str, datetime] = {}

    @property
    def available(self) -> bool:
        return TELETHON_AVAILABLE and bool(self.api_id and self.api_hash)

    def collect(self, lookback_minutes: int = 15) -> List[dict]:
        if not self.available:
            logger.debug("telegram collector disabled")
            return []

        posts: List[dict] = []
        try:
            with TelegramClient(self.session, int(self.api_id), self.api_hash) as client:  # type: ignore[call-arg]
                since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
                for channel in self.channels:
                    try:
                        for msg in client.iter_messages(channel, offset_date=datetime.now(timezone.utc), reverse=False):
                            if msg.date is None or msg.date < since:
                                break
                            text = (msg.message or "").strip()
                            if not text:
                                continue
                            tickers = extract_tickers(text, self.known_tickers)
                            if not tickers:
                                continue
                            posts.append(build_post(
                                platform="telegram",
                                handle=channel.lstrip("@"),
                                text=text,
                                url=f"https://t.me/{channel.lstrip('@')}/{msg.id}",
                                posted_at=msg.date,
                                tickers=tickers,
                                score=1.0,
                            ))
                            inc("scraper_social_posts_total", platform="telegram")
                    except FloodWaitError as exc:
                        logger.warning("telegram flood wait channel=%s seconds=%s", channel, getattr(exc, "seconds", "?"))
                        break
                    except Exception as exc:  # pragma: no cover
                        logger.warning("telegram error channel=%s err=%s", channel, exc)
                        inc("scraper_social_errors_total", platform="telegram")
                        continue
        except Exception as exc:
            logger.error("telegram client error: %s", exc)
            return []

        logger.info("telegram collected %d posts across %d channels", len(posts), len(self.channels))
        return posts
