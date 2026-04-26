"""
Per-source token-bucket rate limiter.

Shared across scraper threads. `acquire(source)` blocks until a token is
available for that source, so we never hammer an upstream past its budget.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last_refill: float


class TokenBucket:
    def __init__(self):
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._configure_defaults()

    def _configure_defaults(self) -> None:
        # conservative defaults — tune per source as we observe real quotas
        defaults = {
            "reddit": (60, 60 / 60.0),          # 60/min
            "twitter": (15, 15 / 60.0),         # 15/min (Nitter friendly)
            "nitter": (15, 15 / 60.0),
            "telegram": (30, 30 / 1.0),         # telethon allows bursts
            "yfinance": (10, 10 / 60.0),
            "groq": (30, 30 / 60.0),            # Groq free tier RPM
            "bse": (20, 20 / 60.0),
            "nse": (20, 20 / 60.0),
            "sebi": (10, 10 / 60.0),
            "screener": (10, 10 / 60.0),
            "newsapi": (100, 100 / 86400.0),    # 100 / day
            "rss": (60, 60 / 60.0),
        }
        now = time.time()
        for src, (cap, rate) in defaults.items():
            self._buckets[src] = _Bucket(cap, rate, cap, now)

    def configure(self, source: str, capacity: float, refill_per_sec: float) -> None:
        with self._lock:
            self._buckets[source] = _Bucket(capacity, refill_per_sec, capacity, time.time())

    def _ensure(self, source: str) -> _Bucket:
        if source not in self._buckets:
            self._buckets[source] = _Bucket(10, 10 / 60.0, 10, time.time())
        return self._buckets[source]

    def acquire(self, source: str, tokens: float = 1.0, max_wait: float = 30.0) -> bool:
        """Block until `tokens` are available. Returns False after max_wait seconds."""
        deadline = time.time() + max_wait
        while True:
            with self._lock:
                b = self._ensure(source)
                now = time.time()
                elapsed = now - b.last_refill
                b.tokens = min(b.capacity, b.tokens + elapsed * b.refill_per_sec)
                b.last_refill = now
                if b.tokens >= tokens:
                    b.tokens -= tokens
                    return True
                need = tokens - b.tokens
                sleep_for = need / b.refill_per_sec if b.refill_per_sec > 0 else max_wait
            if time.time() + sleep_for > deadline:
                logger.warning("rate limit wait exceeded source=%s", source)
                return False
            time.sleep(min(sleep_for, 1.0))

    def status(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            now = time.time()
            out: Dict[str, Dict[str, float]] = {}
            for src, b in self._buckets.items():
                elapsed = now - b.last_refill
                tokens = min(b.capacity, b.tokens + elapsed * b.refill_per_sec)
                out[src] = {
                    "capacity": b.capacity,
                    "tokens": round(tokens, 2),
                    "refill_per_sec": b.refill_per_sec,
                }
            return out


_BUCKET = TokenBucket()


def get_bucket() -> TokenBucket:
    return _BUCKET
