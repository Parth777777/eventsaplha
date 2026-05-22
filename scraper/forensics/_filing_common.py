"""
Shared filing helpers — single home for classification, number extraction,
PDF handling, and DB persistence used by both bse_filing_fetcher.py and
nse_filing_fetcher.py.

Carved out as part of Track 1 Phase B (corporate IR data) so we don't
duplicate 200 lines of identical logic across exchange-specific fetchers.

Exposes:
    classify_filing(title) -> str           # bucket name
    extract_numbers_from_text(text) -> list # canonical financial numbers
    download_pdf(url, source) -> bytes      # rate-limited PDF fetch
    pdf_to_text(bytes) -> str               # first 20 pages via pdfplumber
    upsert_filing(db, ticker, ...) -> int   # idempotent insert, returns id
    store_numbers(db, filing_id, text)      # extract + persist numeric claims

Buckets covered (extends original 4-bucket BSE classifier):
    quarterly_result, order_intimation, agm_notice, corporate_action,
    concall_transcript, investor_presentation, board_outcome,
    credit_rating, qualified_opinion, ratings_action, other
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import pdfplumber  # type: ignore
    PDF_OK = True
except Exception:
    pdfplumber = None
    PDF_OK = False

try:
    from net import request_with_retry  # type: ignore
    from ratelimit import get_bucket  # type: ignore
    from metrics import inc as _metric_inc  # type: ignore
except Exception:
    # Allow standalone import (e.g. unit tests) — degrade gracefully
    request_with_retry = None
    get_bucket = lambda: None  # noqa
    _metric_inc = lambda *a, **k: None  # noqa

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "alphaevent/0.2"
    ),
}

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Lowercase keyword → bucket. First match wins, so put narrower buckets first.
# Order matters: 'concall transcript' before 'concall', 'audit qualifications'
# before 'audit', etc.
_CLASSIFIER_RULES: List[Tuple[str, str]] = [
    # Concall transcripts / presentations / audio
    ("transcript",                 "concall_transcript"),
    ("conference call",            "concall_transcript"),
    ("earnings call",              "concall_transcript"),
    ("analyst meet",               "concall_transcript"),
    ("investor presentation",      "investor_presentation"),
    ("investor pres",              "investor_presentation"),
    ("investor meet",              "investor_presentation"),
    ("press release",              "press_release"),
    # Board outcomes / agenda
    ("outcome of board meeting",   "board_outcome"),
    ("board meeting outcome",      "board_outcome"),
    ("board meeting",              "board_outcome"),
    # Ratings (CRISIL / ICRA / CARE / Brickwork / India Ratings)
    ("crisil",                     "credit_rating"),
    ("icra",                       "credit_rating"),
    ("care ratings",               "credit_rating"),
    ("brickwork",                  "credit_rating"),
    ("india ratings",              "credit_rating"),
    ("rating action",              "credit_rating"),
    ("credit rating",              "credit_rating"),
    ("ratings revision",           "credit_rating"),
    # AGM / EGM
    ("annual general meeting",     "agm_notice"),
    ("postal ballot",              "agm_notice"),
    (" agm ",                      "agm_notice"),
    ("extra-ordinary general",     "agm_notice"),
    ("egm",                        "agm_notice"),
    # Audit / qualifications
    ("qualified opinion",          "qualified_opinion"),
    ("audit qualification",        "qualified_opinion"),
    ("auditor's report",           "qualified_opinion"),
    # Quarterly / annual results
    ("quarterly result",           "quarterly_result"),
    ("financial result",           "quarterly_result"),
    ("audited financial",          "quarterly_result"),
    # Corporate actions
    ("bonus issue",                "corporate_action"),
    ("stock split",                "corporate_action"),
    ("share split",                "corporate_action"),
    ("dividend",                   "corporate_action"),
    ("buy-back",                   "corporate_action"),
    ("buyback",                    "corporate_action"),
    ("rights issue",               "corporate_action"),
    # Order wins
    ("order received",             "order_intimation"),
    ("order intimation",           "order_intimation"),
    ("contract worth",             "order_intimation"),
    ("contract won",               "order_intimation"),
    # M&A
    ("acquisition",                "ma_deal"),
    ("merger",                     "ma_deal"),
    ("amalgamation",               "ma_deal"),
    # Insider / shareholding
    ("shareholding pattern",       "shareholding"),
    ("insider trading",            "insider_disclosure"),
    ("disclosure under regulation","insider_disclosure"),
]


def classify_filing(title: str) -> str:
    """Map a filing title to a canonical bucket. Returns 'other' if no match."""
    t = " " + (title or "").lower().strip() + " "
    for kw, bucket in _CLASSIFIER_RULES:
        if kw in t:
            return bucket
    # Quarterly result regex fallback (Q1/Q2/Q3/Q4)
    if re.search(r"\bq[1-4]\b", t):
        return "quarterly_result"
    return "other"


# ---------------------------------------------------------------------------
# Number extraction (canonical financial fields)
# ---------------------------------------------------------------------------

NUMBER_RE = re.compile(
    r"(?P<label>[A-Za-z ]{3,60}?)\s*[:\-]?\s*(?:₹|Rs\.?|INR|USD|\$)?\s*"
    r"(?P<value>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<unit>crore|cr|lakh|million|mn|billion|bn|%|percent)?",
    re.IGNORECASE,
)

FIELD_ALIASES: Dict[str, List[str]] = {
    "revenue":          ["revenue from operations", "total revenue", "total income", "net sales", "turnover"],
    "net_profit":       ["profit after tax", "net profit", "pat", "profit for the period"],
    "operating_profit": ["ebitda", "operating profit", "operating income"],
    "order_value":      ["order value", "contract value", "order worth", "order size"],
    "eps":              ["earnings per share", "eps", "basic eps"],
    "margin":           ["operating margin", "ebitda margin", "pat margin", "profit margin"],
}

_CANON_SORTED = sorted(
    [(alias, field) for field, aliases in FIELD_ALIASES.items() for alias in aliases],
    key=lambda x: -len(x[0]),
)


def _canonical_field(label: str) -> Optional[str]:
    """Longest-alias-wins match to canonical field name."""
    lab = label.lower().strip()
    for alias, field in _CANON_SORTED:
        if alias in lab:
            return field
    return None


def _normalize_unit(value: float, unit: Optional[str]) -> Tuple[float, str]:
    unit = (unit or "").lower().strip()
    if unit in ("crore", "cr"):
        return value, "cr"
    if unit in ("lakh",):
        return value / 100.0, "cr"
    if unit in ("million", "mn"):
        return value * 10.0 / 83.0, "cr"
    if unit in ("billion", "bn"):
        return value * 1000.0 * 10.0 / 83.0, "cr"
    if unit in ("%", "percent"):
        return value, "pct"
    return value, "raw"


def extract_numbers_from_text(text: str) -> List[Dict]:
    """Pull labelled, canonicalised numbers from free-form filing text."""
    out: List[Dict] = []
    for m in NUMBER_RE.finditer(text or ""):
        label = m.group("label").strip(": -\n\t")
        raw_val = m.group("value").replace(",", "")
        unit = m.group("unit")
        try:
            val = float(raw_val)
        except ValueError:
            continue
        canonical = _canonical_field(label)
        if not canonical:
            continue
        norm_val, norm_unit = _normalize_unit(val, unit)
        out.append({
            "field_name": canonical,
            "value":      norm_val,
            "unit":       norm_unit,
            "raw_text":   m.group(0)[:200],
        })
    return out


# ---------------------------------------------------------------------------
# PDF fetch + extract
# ---------------------------------------------------------------------------

def download_pdf(url: str, source: str = "bse", headers: Optional[Dict] = None,
                 max_wait: float = 5.0) -> Optional[bytes]:
    """Rate-limited PDF download. Returns bytes or None on failure."""
    if not url or request_with_retry is None:
        return None
    bucket = get_bucket()
    if bucket is not None:
        try:
            if not bucket.acquire(source, 1.0, max_wait=max_wait):
                return None
        except Exception:
            pass
    try:
        resp = request_with_retry(
            "GET", url, source=source,
            headers=headers or DEFAULT_HEADERS, timeout=25, attempts=2,
        )
        if resp.status_code != 200:
            return None
        return resp.content
    except Exception as exc:
        logger.debug("pdf download failed url=%s err=%s", url, exc)
        return None


def pdf_to_text(pdf_bytes: bytes, max_pages: int = 20) -> str:
    """Extract text from the first `max_pages` of a PDF. Empty string on failure."""
    if not PDF_OK or not pdf_bytes:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            parts = []
            for page in pdf.pages[:max_pages]:
                parts.append(page.extract_text() or "")
            return "\n".join(parts)
    except Exception as exc:
        logger.debug("pdfplumber failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Persistence (idempotent upsert)
# ---------------------------------------------------------------------------

def upsert_filing(db, ticker: str, ftype: str, title: str, pdf_url: str,
                  filed_at: Optional[datetime], text: str,
                  source: str = "bse", exchange: str = "BSE",
                  summary: Optional[str] = None) -> Optional[int]:
    """Insert-or-update a filings row. Returns row id.

    Source = which scraper produced it (bse / nse / crisil / ir_page).
    Exchange = exchange the company is listed on for this filing context.
    """
    is_pg = bool(getattr(db, "is_postgres", False))
    p = "%s" if is_pg else "?"
    try:
        cursor = db.conn.cursor()
        if is_pg:
            cursor.execute(
                """INSERT INTO filings
                     (ticker, filing_type, title, pdf_url, filed_at, source,
                      raw_text, exchange, summary)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (ticker, pdf_url) DO UPDATE SET
                     raw_text = EXCLUDED.raw_text,
                     summary  = COALESCE(EXCLUDED.summary, filings.summary),
                     exchange = EXCLUDED.exchange
                   RETURNING id""",
                (ticker, ftype, title[:300], pdf_url, filed_at, source,
                 (text or "")[:30000], exchange, (summary or None)),
            )
            row = cursor.fetchone()
            fid = row[0] if row else None
        else:
            cursor.execute(
                f"""INSERT OR REPLACE INTO filings
                     (ticker, filing_type, title, pdf_url, filed_at, source,
                      raw_text, exchange, summary)
                     VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})""",
                (ticker, ftype, title[:300], pdf_url,
                 filed_at.isoformat() if filed_at else None,
                 source, (text or "")[:30000], exchange, (summary or None)),
            )
            fid = cursor.lastrowid
        db.conn.commit()
        try:
            _metric_inc("scraper_filings_total", ticker=ticker, type=ftype, source=source)
        except Exception:
            pass
        return fid
    except Exception as exc:
        logger.warning("filing upsert failed ticker=%s err=%s", ticker, exc)
        try:
            db.conn.rollback()
        except Exception:
            pass
        return None


def store_numbers(db, filing_id: int, text: str) -> int:
    """Extract canonical numbers from text and persist to filing_numbers.
    Returns number of rows inserted."""
    if not filing_id or not text:
        return 0
    is_pg = bool(getattr(db, "is_postgres", False))
    p = "%s" if is_pg else "?"
    rows = extract_numbers_from_text(text)
    if not rows:
        return 0
    try:
        cursor = db.conn.cursor()
        for r in rows:
            cursor.execute(
                f"""INSERT INTO filing_numbers
                      (filing_id, field_name, value, unit, raw_text)
                      VALUES ({p}, {p}, {p}, {p}, {p})""",
                (filing_id, r["field_name"], r["value"], r["unit"], r["raw_text"]),
            )
        db.conn.commit()
        return len(rows)
    except Exception as exc:
        logger.debug("filing numbers insert failed: %s", exc)
        try:
            db.conn.rollback()
        except Exception:
            pass
        return 0


# ---------------------------------------------------------------------------
# Back-compat shims for the legacy BSE module (which expected these names)
# ---------------------------------------------------------------------------

# Older modules import `_classify_filing` directly — keep the underscore name.
_classify_filing = classify_filing
