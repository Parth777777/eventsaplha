"""Thin Groq client wrapper with governor integration.

Centralizes the urllib.request pattern that was previously duplicated 6+ times
across api.py, api_v3.py, jargon/loader.py, forensics/article_forensics.py,
etc. Every new caller (summarize, explain, chat, briefing) goes through here.

Usage:
    from backend.ai_groq import groq_chat
    text = groq_chat(
        module="summarize",
        messages=[
            {"role": "system", "content": "..."},
            {"role": "user",   "content": "..."},
        ],
        max_tokens=120,
        est_tokens=300,
    )
    if text is None:  # budget exhausted, governor refused, or network failure
        text = template_fallback(...)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Generator, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Make scraper/ importable so the groq_governor import works whether the
# caller runs as backend.ai_groq, ai_groq, or via the worker daemon.
_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scraper", _ROOT / "backend"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)


def _governor():
    try:
        from groq_governor import governor  # type: ignore
        return governor
    except Exception:
        return None


def _api_key() -> Optional[str]:
    return os.getenv("GROQ_API_KEY") or None


def _model(prefer_fast: bool = False) -> str:
    if prefer_fast:
        return os.getenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
    return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def groq_chat(
    *,
    module: str,
    messages: List[dict],
    max_tokens: int = 200,
    est_tokens: int = 600,
    temperature: float = 0.3,
    prefer_fast: bool = False,
    timeout_secs: int = 12,
) -> Optional[str]:
    """Non-streaming completion. Returns the assistant text, or None on
    budget refusal / network error / missing key. Never raises."""
    key = _api_key()
    if not key:
        return None
    gov = _governor()
    if gov is not None and not gov.can_spend(module, est_tokens=est_tokens):
        return None
    try:
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps({
                "model": _model(prefer_fast),
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # Groq sits behind Cloudflare which 403s `Python-urllib/X.Y`
                # with error 1010 ("browser signature banned"). A real
                # browser UA + an Accept header gets past the bot filter.
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
            data = json.loads(resp.read())
        msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if gov is not None:
            try:
                gov.record_spend(module, int((data.get("usage") or {}).get("total_tokens", est_tokens)))
            except Exception:
                pass
        return (msg or "").strip()
    except urllib.error.HTTPError as e:
        if e.code == 429 and gov is not None:
            gov.record_429(module)
        logger.warning("groq_chat(%s) HTTP %s", module, e.code)
        return None
    except Exception as e:
        logger.warning("groq_chat(%s) failed: %s", module, e)
        return None


def groq_chat_stream(
    *,
    module: str,
    messages: List[dict],
    max_tokens: int = 600,
    est_tokens: int = 1500,
    temperature: float = 0.3,
    prefer_fast: bool = False,
    timeout_secs: int = 60,
) -> Generator[str, None, None]:
    """Streaming completion — yields content deltas as they arrive.

    Used by /api/chat for SSE. On budget refusal yields nothing.
    """
    key = _api_key()
    if not key:
        return
    gov = _governor()
    if gov is not None and not gov.can_spend(module, est_tokens=est_tokens):
        return
    try:
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps({
                "model": _model(prefer_fast),
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # See groq_chat() above for the why — Cloudflare 1010 fix.
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
                "Accept": "text/event-stream",
            },
        )
        total = 0
        with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        total += len(text) // 4  # rough token estimate
                        yield text
                except Exception:
                    continue
        if gov is not None:
            try:
                gov.record_spend(module, max(50, total))
            except Exception:
                pass
    except urllib.error.HTTPError as e:
        if e.code == 429 and gov is not None:
            gov.record_429(module)
        logger.warning("groq_chat_stream(%s) HTTP %s", module, e.code)
    except Exception as e:
        logger.warning("groq_chat_stream(%s) failed: %s", module, e)
