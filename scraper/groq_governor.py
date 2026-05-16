"""
Centralized Groq budget governor.

Tracks token spend per UTC day across all call sites (verification, intent,
forensics, jargon, reasoning) so we don't blow the TPD limit and trigger
the 429 retry-storm seen in scraper.log.

State persists to a tiny JSON file so usage survives scraper restarts within
the same UTC day.

Usage:
    from groq_governor import governor

    if not governor.can_spend("verification", est_tokens=700):
        return None  # budget exhausted, skip the call
    resp = make_groq_call(...)
    governor.record_spend("verification", actual_tokens=resp.usage.total_tokens)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# --- Defaults; override via env ---
# Groq free tier (llama-3.3-70b-versatile): 100k tokens/day.
# Set GROQ_TPD higher if on paid tier.
DEFAULT_TPD = int(os.getenv("GROQ_TPD", "100000"))

# Per-module budget weights (sum should be <= 1.0). Tunable via env.
DEFAULT_BUDGETS = {
    "verification": float(os.getenv("GROQ_BUDGET_VERIFICATION", "0.55")),
    "intent":       float(os.getenv("GROQ_BUDGET_INTENT", "0.20")),
    "reasoning":    float(os.getenv("GROQ_BUDGET_REASONING", "0.15")),
    "jargon":       float(os.getenv("GROQ_BUDGET_JARGON", "0.05")),
    "forensics":    float(os.getenv("GROQ_BUDGET_FORENSICS", "0.05")),
}

# Hard-stop margin — refuse new calls when within this fraction of the TPD.
SAFETY_MARGIN = 0.90  # stop at 90% TPD, leaving headroom for retries.

# Circuit breaker: after this many consecutive 429s, refuse calls for cooldown.
CB_FAIL_THRESHOLD = 3
CB_COOLDOWN_SECS = 300  # 5 minutes


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class _Governor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state_path = Path(os.getenv("GROQ_STATE_FILE", "")) if os.getenv("GROQ_STATE_FILE") else \
                           Path(__file__).parent.parent / "data" / "groq_state.json"
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._tpd = DEFAULT_TPD
        self._budgets = dict(DEFAULT_BUDGETS)
        self._spent: Dict[str, int] = {}
        self._day = _today_utc()
        self._cb_fail = 0
        self._cb_until = 0.0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._state_path.read_text())
            if data.get("day") == self._day:
                self._spent = {k: int(v) for k, v in (data.get("spent") or {}).items()}
                self._cb_fail = int(data.get("cb_fail", 0))
                self._cb_until = float(data.get("cb_until", 0.0))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("groq_governor: failed to load state: %s", e)

    def _persist(self) -> None:
        try:
            self._state_path.write_text(json.dumps({
                "day": self._day,
                "spent": self._spent,
                "cb_fail": self._cb_fail,
                "cb_until": self._cb_until,
            }))
        except Exception as e:
            logger.warning("groq_governor: failed to persist state: %s", e)

    def _roll_day_if_needed(self) -> None:
        today = _today_utc()
        if today != self._day:
            self._day = today
            self._spent = {}
            self._cb_fail = 0
            self._cb_until = 0.0
            self._persist()
            logger.info("groq_governor: rolled to new UTC day %s", today)

    def _module_cap(self, module: str) -> int:
        weight = self._budgets.get(module)
        if weight is None:
            # Unknown module — give it a small slice rather than refuse outright.
            weight = 0.02
        return int(self._tpd * weight)

    # Modules considered ESSENTIAL — they keep flowing even in save mode.
    # Anything not in this set is paused once we cross 80% TPD.
    _SAVE_MODE_PROTECTED = {"verification", "reasoning"}
    _SAVE_MODE_THRESHOLD = 0.80   # at 80% TPD, refuse non-essential calls

    def can_spend(self, module: str, est_tokens: int = 600) -> bool:
        """Return True if the call is allowed to proceed."""
        with self._lock:
            self._roll_day_if_needed()
            now = time.time()
            if now < self._cb_until:
                return False  # circuit breaker open
            total_used = sum(self._spent.values())
            if total_used + est_tokens > int(self._tpd * SAFETY_MARGIN):
                return False
            # Save Mode: when overall usage is high, ration tokens to essential
            # work only. Frees budget for verification + reasoning when it
            # matters most (e.g. late in the trading day).
            if (total_used / max(self._tpd, 1)) >= self._SAVE_MODE_THRESHOLD \
                    and module not in self._SAVE_MODE_PROTECTED:
                return False
            mod_cap = self._module_cap(module)
            mod_used = self._spent.get(module, 0)
            if mod_used + est_tokens > mod_cap:
                return False
            return True

    def record_spend(self, module: str, actual_tokens: int) -> None:
        with self._lock:
            self._roll_day_if_needed()
            self._spent[module] = self._spent.get(module, 0) + max(0, int(actual_tokens))
            # Successful call resets the failure counter.
            self._cb_fail = 0
            self._persist()

    def record_429(self, module: str) -> None:
        """Account for a 429 — bumps the circuit breaker and trips it past threshold."""
        with self._lock:
            self._cb_fail += 1
            # Approximate: assume the rejected call still cost ~half its est tokens
            # (Groq counts request even on rate-limit reject in some scenarios).
            self._spent[module] = self._spent.get(module, 0) + 300
            if self._cb_fail >= CB_FAIL_THRESHOLD:
                self._cb_until = time.time() + CB_COOLDOWN_SECS
                logger.warning(
                    "groq_governor: circuit breaker OPEN (cooldown %ds) after %d consecutive 429s",
                    CB_COOLDOWN_SECS, self._cb_fail,
                )
            self._persist()

    def status(self) -> Dict[str, object]:
        with self._lock:
            self._roll_day_if_needed()
            total_used = sum(self._spent.values())
            now = time.time()
            return {
                "day": self._day,
                "tpd": self._tpd,
                "total_used": total_used,
                "total_pct": round(100 * total_used / max(self._tpd, 1), 2),
                "by_module": {
                    m: {
                        "used": self._spent.get(m, 0),
                        "cap": self._module_cap(m),
                    }
                    for m in self._budgets
                },
                "circuit_breaker_open": now < self._cb_until,
                "circuit_breaker_resume_in_s": max(0, int(self._cb_until - now)),
            }


governor = _Governor()


def call_with_governor(module: str, est_tokens: int, fn, *args, **kwargs):
    """Convenience wrapper. `fn` must return an object with `.usage.total_tokens`
    on success, or raise. 429 errors must contain '429' in str(exc) to be detected.
    Returns the result, or None if budget exhausted / call failed.
    """
    if not governor.can_spend(module, est_tokens=est_tokens):
        return None
    try:
        result = fn(*args, **kwargs)
        try:
            tokens = int(result.usage.total_tokens)
        except Exception:
            tokens = est_tokens
        governor.record_spend(module, tokens)
        return result
    except Exception as exc:
        if "429" in str(exc) or "rate_limit" in str(exc).lower():
            governor.record_429(module)
        raise
