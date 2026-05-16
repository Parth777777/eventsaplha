"""Company-logo proxy + disk cache.

Resolves a ticker → company logo bytes by trying public sources in order:
  1. BSE official logo file (needs the BSE scrip code)
  2. Trendlyne static logo (same scrip code namespace)
  3. SVG initials fallback (always succeeds)

The first successful raster source is cached on disk so future hits are
served straight from the filesystem. The SVG fallback is *not* cached —
that lets a later refresh pick up a real logo if BSE/Trendlyne add one.

A negative-cache file tracks tickers that all sources failed for, so we
don't hammer external endpoints. Negative-cache entries expire after
NEGATIVE_TTL_SECS so newly-listed tickers eventually get re-tried.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ---- Cache layout ----------------------------------------------------------

_HERE = Path(__file__).resolve().parent
CACHE_DIR = _HERE.parent / "data" / "logos"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
NEGATIVE_CACHE = CACHE_DIR / ".failed.json"
NEGATIVE_TTL_SECS = 24 * 3600  # 24h
HTTP_TIMEOUT = 6

_NEG_LOCK = threading.Lock()
_neg_mem: dict = {}


def _load_negative() -> dict:
    global _neg_mem
    if _neg_mem:
        return _neg_mem
    if NEGATIVE_CACHE.exists():
        try:
            import json
            _neg_mem = json.loads(NEGATIVE_CACHE.read_text() or "{}")
        except Exception:
            _neg_mem = {}
    return _neg_mem


def _save_negative() -> None:
    try:
        import json
        NEGATIVE_CACHE.write_text(json.dumps(_neg_mem))
    except Exception as e:
        logger.debug(f"_save_negative: {e}")


def _mark_failed(ticker: str) -> None:
    with _NEG_LOCK:
        _load_negative()
        _neg_mem[ticker] = int(time.time())
        _save_negative()


def _is_recently_failed(ticker: str) -> bool:
    with _NEG_LOCK:
        neg = _load_negative()
        ts = neg.get(ticker, 0)
        return ts and (int(time.time()) - ts) < NEGATIVE_TTL_SECS


# ---- External sources ------------------------------------------------------

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# Sources tried in order. Parqet.com is the workhorse for Indian listings —
# they index by NSE / BSE ticker symbols with .NS / .BO suffixes.
LOGO_SOURCES = [
    ("parqet_ns", "https://assets.parqet.com/logos/symbol/{ticker}.NS"),
    ("parqet_bo", "https://assets.parqet.com/logos/symbol/{ticker}.BO"),
]
# Min bytes for a "real" logo — anything smaller is a placeholder/error page.
MIN_IMAGE_BYTES = 300


def _fetch_url(url: str) -> Optional[bytes]:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except Exception as e:
        logger.debug(f"fetch_url {url}: {e}")
        return None
    if r.status_code != 200 or not r.content or len(r.content) < MIN_IMAGE_BYTES:
        return None
    ct = r.headers.get("Content-Type", "").lower()
    # Some sources return 200 + HTML "not found" page. Sniff for image bytes:
    if ct and "image" not in ct and "octet" not in ct:
        return None
    # PNG magic, JPEG magic, GIF magic, WebP magic
    head = r.content[:12]
    if not (head.startswith(b"\x89PNG\r\n\x1a\n")
            or head[:2] == b"\xff\xd8"
            or head[:3] == b"GIF"
            or head[:4] == b"RIFF"):
        return None
    return r.content


def _try_external(ticker: str, scrip: Optional[str] = None) -> Optional[Tuple[bytes, str]]:
    for name, tmpl in LOGO_SOURCES:
        url = tmpl.format(ticker=ticker)
        b = _fetch_url(url)
        if b:
            logger.info(f"logo cache miss {ticker} -> {name} ({len(b)} bytes)")
            return b, "image/png"
    return None


# ---- SVG initials fallback -------------------------------------------------

# Deterministic but pleasant palette: hash → pick. Avoids ugly random.
_PALETTE = [
    "#1e3a5f", "#3b5b8a", "#4a6b94", "#2d4a6b", "#5a3a5f",
    "#4f6d4a", "#6b3a3a", "#5a4a3a", "#3a5a5a", "#4a3a6b",
]


def _initials_svg(ticker: str, size: int = 96) -> bytes:
    t = (ticker or "?").strip().upper()
    initials = t[:2] if len(t) >= 2 else t
    # Hash-based color pick
    h = int(hashlib.md5(t.encode()).hexdigest()[:8], 16)
    bg = _PALETTE[h % len(_PALETTE)]
    # White text, slight outline for legibility on dark themes
    font_size = int(size * 0.42)
    svg = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size}" height="{size}">'
        f'<rect width="{size}" height="{size}" rx="{size//2}" fill="{bg}"/>'
        f'<text x="50%" y="50%" font-family="Segoe UI,Arial,sans-serif" '
        f'font-size="{font_size}" font-weight="600" fill="#fff" '
        f'text-anchor="middle" dominant-baseline="central">{initials}</text>'
        f'</svg>'
    )
    return svg.encode("utf-8")


# ---- Public API ------------------------------------------------------------

def get_logo(ticker: str) -> Tuple[bytes, str, bool]:
    """Resolve a ticker to logo bytes.

    Returns (bytes, mimetype, is_real). is_real=False means SVG initials
    fallback (no actual logo found).
    """
    t = (ticker or "").strip().upper()
    if not t:
        return _initials_svg("?"), "image/svg+xml", False

    # Disk cache hit
    cached = CACHE_DIR / f"{t}.png"
    if cached.exists() and cached.stat().st_size > 0:
        try:
            return cached.read_bytes(), "image/png", True
        except Exception:
            pass

    # Negative cache: serve fallback without re-trying
    if _is_recently_failed(t):
        return _initials_svg(t), "image/svg+xml", False

    # Try external sources
    result = _try_external(t)
    if result:
        b, mime = result
        try:
            cached.write_bytes(b)
        except Exception as e:
            logger.debug(f"cache write {t}: {e}")
        return b, mime, True

    # All sources failed — fall back, mark negative
    _mark_failed(t)
    return _initials_svg(t), "image/svg+xml", False


def warm_cache(tickers) -> dict:
    """Pre-fetch logos for a list of tickers. Returns {ticker: 'ok'|'fallback'}."""
    out = {}
    for t in tickers:
        _, _, real = get_logo(t)
        out[t.upper()] = "ok" if real else "fallback"
    return out
