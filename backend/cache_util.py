"""Tiny in-memory TTL response cache.

Used by hot read endpoints (/api/feed, /api/stock/<t>/intelligence, etc.)
so repeat hits within the TTL window skip all DB / yfinance work.

Keyed by (request.path, sorted query string). Thread-safe. Bounded.
"""
from __future__ import annotations

import functools
import threading
import time
from typing import Callable

from flask import make_response, request

_CACHE: dict = {}
_LOCK = threading.Lock()
_MAX = 2000


def _key() -> str:
    qs = request.query_string.decode("utf-8", errors="ignore")
    return f"{request.path}?{qs}"


def ttl_cache(seconds: int) -> Callable:
    """Decorator: cache the 200-response body for `seconds` per (path, query).

    Non-200 responses are not cached so transient backend failures don't
    poison the cache. Adds X-Cache: HIT/MISS for observability.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = _key()
            now = time.time()
            with _LOCK:
                hit = _CACHE.get(key)
            if hit and (now - hit["ts"]) < seconds:
                resp = make_response(hit["body"], 200)
                resp.headers["Content-Type"] = hit["ct"]
                resp.headers["X-Cache"] = "HIT"
                return resp
            resp = fn(*args, **kwargs)
            try:
                if hasattr(resp, "status_code") and resp.status_code == 200:
                    body = resp.get_data()
                    ct = resp.headers.get("Content-Type", "application/json")
                    with _LOCK:
                        if len(_CACHE) >= _MAX:
                            cutoff = sorted(v["ts"] for v in _CACHE.values())[len(_CACHE) // 4]
                            for k in [k for k, v in _CACHE.items() if v["ts"] <= cutoff]:
                                _CACHE.pop(k, None)
                        _CACHE[key] = {"ts": now, "body": body, "ct": ct}
                    resp.headers["X-Cache"] = "MISS"
            except Exception:
                pass
            return resp
        return wrapper
    return deco


def invalidate_prefix(prefix: str) -> int:
    """Drop every cached entry whose key starts with `prefix`. Returns count."""
    with _LOCK:
        keys = [k for k in _CACHE if k.startswith(prefix)]
        for k in keys:
            _CACHE.pop(k, None)
        return len(keys)


def stats() -> dict:
    with _LOCK:
        return {"entries": len(_CACHE), "max": _MAX}
