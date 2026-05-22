"""
Credit rating fetcher — (Track 1 Phase B).

Resolution priority:
    1. filings table where filing_type='credit_rating'  (most reliable —
       rating actions are also pushed to the exchange as filings, classified
       by _filing_common.classify_filing on Phase B keywords like
       'crisil'/'icra'/'care ratings'/'rating action')
    2. Best-effort agency-site scrape per ticker (CRISIL, ICRA, CARE)
       — included but disabled by default because public agency pages
       are heavily rate-limited and change frequently. Enable per-ticker
       via fetch(..., scrape=True) on a cron with backoff.

Output shape (per item):
    {
      agency:  'CRISIL'|'ICRA'|'CARE'|'BRICKWORK'|'INDIA_RATINGS'|'OTHER',
      action:  'upgrade'|'downgrade'|'reaffirmed'|'withdrawn'|'new'|'unknown',
      prior:   str (e.g. 'BBB+/Stable')   or None,
      current: str (e.g. 'A-/Stable')      or None,
      outlook: str (e.g. 'Stable'|'Positive') or None,
      date:    'YYYY-MM-DD',
      url:     pdf or press-release URL,
      source:  'filings'|'crisil'|'icra'|'care',
    }
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from forensics._filing_common import (
    download_pdf as _common_download_pdf,
    pdf_to_text,
)

logger = logging.getLogger(__name__)

# Agency keyword → canonical agency name. Order matters (longest first).
_AGENCY_RULES = [
    ("india ratings",  "INDIA_RATINGS"),
    ("brickwork",      "BRICKWORK"),
    ("care ratings",   "CARE"),
    ("crisil",         "CRISIL"),
    ("icra",           "ICRA"),
    ("moody's",        "MOODYS"),
    ("fitch",          "FITCH"),
    ("standard & poor",  "SNP"),
    ("s&p",            "SNP"),
]

# Rating-action keyword → canonical action verb.
_ACTION_RULES = [
    ("upgraded",      "upgrade"),
    ("upgrade",       "upgrade"),
    ("downgraded",    "downgrade"),
    ("downgrade",     "downgrade"),
    ("reaffirmed",    "reaffirmed"),
    ("affirmed",      "reaffirmed"),
    ("withdrawn",     "withdrawn"),
    ("assigned",      "new"),
    ("placed",        "watchlist"),
    ("watch",         "watchlist"),
]

# Rating grade regex (e.g. AAA, AA+, BBB-/Negative, CRISIL A1+)
_GRADE_RE = re.compile(
    r"\b(?:CRISIL|ICRA|CARE|BWR|IND|S&P|MOODYS)\s+"
    r"([A-D][A-Z]{0,2}[+\-]?(?:1?)(?:/[A-Z][a-z]+)?)",
    re.IGNORECASE,
)
# Simpler grade-only regex for fallback
_GRADE_FALLBACK_RE = re.compile(r"\b([A-D][A-Z]{0,2}[+\-]?(?:1?))\b")


def _detect_agency(title: str, text: str = "") -> str:
    blob = ((title or "") + " " + (text or ""))[:2000].lower()
    for kw, agency in _AGENCY_RULES:
        if kw in blob:
            return agency
    return "OTHER"


def _detect_action(title: str, text: str = "") -> str:
    blob = ((title or "") + " " + (text or ""))[:2000].lower()
    for kw, action in _ACTION_RULES:
        if kw in blob:
            return action
    return "unknown"


def _detect_grades(text: str) -> Dict[str, Optional[str]]:
    """Try to extract 'prior' and 'current' grades. Heuristic — looks for
    'from X to Y' patterns, falling back to the first/last grade in text."""
    if not text:
        return {"prior": None, "current": None, "outlook": None}
    # "from BBB+/Stable to A-/Stable"
    m = re.search(
        r"from\s+([A-D][A-Z]{0,2}[+\-]?(?:/[A-Z][a-z]+)?)\s+to\s+([A-D][A-Z]{0,2}[+\-]?(?:/[A-Z][a-z]+)?)",
        text, re.IGNORECASE,
    )
    if m:
        prior = m.group(1)
        current = m.group(2)
        outlook = (current.split("/")[1] if "/" in current else None)
        return {"prior": prior, "current": current, "outlook": outlook}
    # Fallback: pick first plausible grade as 'current'
    g = _GRADE_RE.search(text) or _GRADE_FALLBACK_RE.search(text)
    if g:
        return {"prior": None, "current": g.group(1), "outlook": None}
    return {"prior": None, "current": None, "outlook": None}


def from_filings_db(db, ticker: str, limit: int = 12) -> List[Dict]:
    """Pull credit-rating actions previously stored in filings."""
    if not db or not ticker:
        return []
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    sql = f"""
        SELECT title, pdf_url, filed_at, raw_text, source, exchange
          FROM filings
         WHERE ticker = {p}
           AND filing_type = 'credit_rating'
         ORDER BY filed_at DESC
         LIMIT {p}
    """
    try:
        cur = db.conn.cursor()
        cur.execute(sql, (ticker.upper(), limit))
        rows = cur.fetchall() or []
    except Exception as exc:
        logger.debug("credit rating db query failed ticker=%s err=%s", ticker, exc)
        return []
    out: List[Dict] = []
    for r in rows:
        title, url, filed_at, text, source, exchange = (
            r[0], r[1], r[2], r[3], r[4],
            (r[5] if len(r) > 5 else None),
        )
        text = text or ""
        agency = _detect_agency(title, text)
        action = _detect_action(title, text)
        grades = _detect_grades(text)
        date_str = ""
        try:
            date_str = filed_at.strftime("%Y-%m-%d") if hasattr(filed_at, "strftime") else str(filed_at)[:10]
        except Exception:
            date_str = str(filed_at)[:10] if filed_at else ""
        out.append({
            "agency":  agency,
            "action":  action,
            "prior":   grades["prior"],
            "current": grades["current"],
            "outlook": grades["outlook"],
            "date":    date_str,
            "url":     url or "",
            "source":  source or "filings",
            "title":   title or "",
            "exchange": exchange or "",
        })
    return out


def resolve(db, ticker: str, limit: int = 12) -> List[Dict]:
    """Public entry — currently DB-only (Phase B). Agency-site scrapers can
    be added later behind a feature flag once the BSE/NSE bucket coverage
    proves insufficient."""
    return from_filings_db(db, ticker, limit=limit)


def enrich_filing(text: str, title: str = "") -> Dict:
    """Inline enrichment helper for the AI-research / summary pipeline.
    Given the raw filing text, return the structured rating-action record."""
    return {
        "agency":  _detect_agency(title, text),
        "action":  _detect_action(title, text),
        **_detect_grades(text),
    }
