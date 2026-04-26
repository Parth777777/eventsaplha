"""
Tiny cache façade — uses Redis if REDIS_URL is set, otherwise in-process TTL dict.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REDIS = None
_REDIS_TRIED = False


def _redis():
    global _REDIS, _REDIS_TRIED
    if _REDIS is not None or _REDIS_TRIED:
        return _REDIS
    _REDIS_TRIED = True
    url = os.getenv("REDIS_URL", "")
    if not url:
        return None
    try:
        import redis  # type: ignore

        _REDIS = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
        _REDIS.ping()
        logger.info("redis cache connected")
    except Exception as exc:
        logger.warning("redis unavailable, falling back to in-proc cache: %s", exc)
        _REDIS = None
    return _REDIS


class _MemoryCache:
    def __init__(self, max_entries: int = 4096):
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._max = max_entries

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires < time.time():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            if len(self._data) >= self._max:
                # evict ~10% oldest
                for k, _ in sorted(self._data.items(), key=lambda kv: kv[1][0])[: self._max // 10]:
                    self._data.pop(k, None)
            self._data[key] = (time.time() + ttl, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


_MEM = _MemoryCache()


def cache_get(key: str) -> Optional[Any]:
    r = _redis()
    if r is not None:
        try:
            raw = r.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("redis get failed: %s", exc)
    return _MEM.get(key)


def cache_set(key: str, value: Any, ttl: int) -> None:
    r = _redis()
    if r is not None:
        try:
            r.setex(key, ttl, json.dumps(value, default=str))
            return
        except Exception as exc:
            logger.warning("redis set failed: %s", exc)
    _MEM.set(key, value, ttl)


def cache_delete(key: str) -> None:
    r = _redis()
    if r is not None:
        try:
            r.delete(key)
            return
        except Exception as exc:
            logger.warning("redis del failed: %s", exc)
    _MEM.delete(key)


def cached(prefix: str, ttl: int):
    """Function decorator — cache by (prefix, args, kwargs)."""

    def decorate(fn):
        def wrapped(*args, **kwargs):
            key = f"{prefix}:{hash((args, tuple(sorted(kwargs.items()))))}"
            hit = cache_get(key)
            if hit is not None:
                return hit
            result = fn(*args, **kwargs)
            if result is not None:
                cache_set(key, result, ttl)
            return result

        return wrapped

    return decorate
