"""
Shared HTTP helper with retry + circuit-breaker.

Wraps outbound requests so one misbehaving source can't stall the scraper.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import requests

logger = logging.getLogger(__name__)


class CircuitOpen(Exception):
    """Raised when the circuit breaker is open for a given source."""


@dataclass
class _BreakerState:
    failures: int = 0
    opened_at: Optional[float] = None
    half_open_trial: bool = False


class CircuitBreaker:
    """Per-source circuit breaker.

    Trips open after `fail_threshold` consecutive failures. While open, calls
    raise CircuitOpen immediately. After `reset_seconds`, one trial call is
    allowed (half-open); success closes the breaker, failure reopens it.
    """

    def __init__(self, fail_threshold: int = 5, reset_seconds: int = 60):
        self.fail_threshold = fail_threshold
        self.reset_seconds = reset_seconds
        self._states: Dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def _state(self, key: str) -> _BreakerState:
        with self._lock:
            st = self._states.get(key)
            if st is None:
                st = _BreakerState()
                self._states[key] = st
            return st

    def before(self, key: str) -> None:
        st = self._state(key)
        with self._lock:
            if st.opened_at is None:
                return
            elapsed = time.time() - st.opened_at
            if elapsed < self.reset_seconds:
                raise CircuitOpen(f"circuit open for {key} ({int(self.reset_seconds - elapsed)}s remaining)")
            st.half_open_trial = True

    def on_success(self, key: str) -> None:
        st = self._state(key)
        with self._lock:
            st.failures = 0
            st.opened_at = None
            st.half_open_trial = False

    def on_failure(self, key: str) -> None:
        st = self._state(key)
        with self._lock:
            st.failures += 1
            if st.failures >= self.fail_threshold and st.opened_at is None:
                st.opened_at = time.time()
                logger.warning("circuit opened source=%s failures=%d", key, st.failures)

    def status(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                k: {
                    "failures": v.failures,
                    "open": v.opened_at is not None,
                    "seconds_until_retry": max(0, int(self.reset_seconds - (time.time() - v.opened_at))) if v.opened_at else 0,
                }
                for k, v in self._states.items()
            }


_BREAKER = CircuitBreaker()


def get_breaker() -> CircuitBreaker:
    return _BREAKER


def request_with_retry(
    method: str,
    url: str,
    source: str,
    *,
    attempts: int = 3,
    base_backoff: float = 0.8,
    timeout: float = 15.0,
    session: Optional[requests.Session] = None,
    **kwargs: Any,
) -> requests.Response:
    """HTTP call with exponential backoff + jitter + circuit-breaker.

    `source` is the logical source key (e.g. "bse", "reddit"). All calls
    against the same source share a breaker.
    """
    _BREAKER.before(source)
    sess = session or requests
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            resp = sess.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise requests.HTTPError(f"http {resp.status_code} for {url}")
            _BREAKER.on_success(source)
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt == attempts:
                _BREAKER.on_failure(source)
                logger.warning("request failed source=%s url=%s attempts=%d err=%s", source, url, attempt, exc)
                raise
            sleep_s = base_backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc


def safe_call(fn: Callable[..., Any], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Call `fn` and swallow exceptions, returning `default`.

    Use this at the edge of collectors where an outage on one source shouldn't
    take down the whole pipeline.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("safe_call swallowed err=%s fn=%s", exc, getattr(fn, "__name__", fn))
        return default
