"""M&A (Mergers & Acquisitions) ingestion + deal lifecycle tracking.

Pulls from two source layers, deduped under one logical deal:
  1. Filings (BSE corporate announcements, NSE corporate-actions RSS, SEBI feeds)
       Authoritative but lagging — captures Reg 10 open offers, schemes of
       arrangement (Companies Act §391-394), SAST Reg 29 disclosures, and
       SEBI delisting orders.
  2. News (Moneycontrol / ET / Mint / BS / Reuters RSS already in config.py)
       Earlier signal, but noisier — used to catch deals before formal filing
       and to extract terms (deal value, consideration type) that are missing
       from the filing itself.

Schema:
    ma_deals(deal_id, acquirer_*, target_*, deal_type, deal_value_cr,
             consideration_type, stake_pct, status, announcement_date,
             expected_close_date, sector_*, source_url, source_tier,
             alpha_score, payload_json, created_at, updated_at)
    ma_deal_events(deal_id, event_date, event_type, description, source_url)

State machine:
    rumor → announced → sebi_filed → approved → effective
                     ↘ withdrawn

This module is best-effort: external feeds rate-limit or break frequently.
Errors are swallowed, logged at debug level, and the caller never crashes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, date, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------- M&A classifiers (regex on filing/news titles) ----------

MA_CLASSIFIERS = [
    # (deal_type, regex, default_status_when_only_match)
    ("open_offer", re.compile(r"open\s+offer|public\s+announcement|reg\.?\s*10\b|sast\s+reg(?:ulation)?s?\s*10", re.I), "announced"),
    ("scheme",     re.compile(r"scheme\s+of\s+arrangement|scheme\s+of\s+amalgamation|composite\s+scheme|sec(?:tion)?\s*230|sec(?:tion)?\s*391|sec(?:tion)?\s*394|nclt", re.I), "sebi_filed"),
    ("delisting",  re.compile(r"delist(?:ing)?|voluntary\s+delisting|reverse\s+book\s*build", re.I), "announced"),
    ("merger",     re.compile(r"merger|amalgamation|demerger|hive[- ]off", re.I), "announced"),
    ("acquisition", re.compile(r"acquir(?:e|ed|ing|sition)|acqua\.|takeover|controlling\s+stake|majority\s+stake", re.I), "announced"),
]

# Pattern: status-progression keywords that bump an existing deal forward
STATUS_PROGRESSION = [
    (re.compile(r"\b(?:cci|competition\s+commission)\s+(?:approves|approved)", re.I), "approved"),
    (re.compile(r"\b(?:nclt|national\s+company\s+law\s+tribunal)\s+(?:approves|approved|sanctions)", re.I), "approved"),
    (re.compile(r"\b(?:sebi|nse|bse)\s+(?:approves|approved)", re.I), "approved"),
    (re.compile(r"\beffective(?:\s+date)?\b|\bcompleted\b|\bconsummated\b|\bclosed\b", re.I), "effective"),
    (re.compile(r"\bwithdraw(?:n|al)\b|\bcalled\s+off\b|\bterminated\b", re.I), "withdrawn"),
]

# Deal value extraction: "Rs 1,234 crore", "₹500 cr", "USD 200 mn", "$1.5bn"
VALUE_RE = [
    (re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:cr(?:ore)?s?)\b", re.I), 1.0),
    (re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:lakh\s*cr(?:ore)?)", re.I), 100000.0),
    (re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:bn|billion)\b", re.I), 8300.0),    # 1bn USD ≈ 8300 cr at 83 INR
    (re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:mn|million)\b", re.I), 8.3),
    (re.compile(r"usd\s*([\d,]+(?:\.\d+)?)\s*(?:bn|billion)\b", re.I), 8300.0),
    (re.compile(r"usd\s*([\d,]+(?:\.\d+)?)\s*(?:mn|million)\b", re.I), 8.3),
]

STAKE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:stake|equity|shareholding|shares?)", re.I)
CONSID_RE = {
    "stock":  re.compile(r"share\s*swap|stock(?:-|\s)?(?:for|swap)|equity\s+consideration", re.I),
    "cash":   re.compile(r"\bcash\s+(?:deal|consideration|offer|payment)", re.I),
}

# Pull "X acquires Y" / "X to acquire Y" / "X-Y merger" style party extraction
PARTY_RE = [
    re.compile(r"^([A-Z][\w&. ]{2,80}?)\s+(?:to\s+)?acquir(?:e|es|ed|ing)\s+([A-Z][\w&. ]{2,80}?)(?:\s+for|\s+at|\s+in|;|,|\.|$)", re.I),
    re.compile(r"^([A-Z][\w&. ]{2,80}?)\s*[-–]\s*([A-Z][\w&. ]{2,80}?)\s+(?:merger|amalgamation|deal)", re.I),
    re.compile(r"merger\s+of\s+([A-Z][\w&. ]{2,80}?)\s+(?:with|and|into)\s+([A-Z][\w&. ]{2,80}?)(?:;|,|\.|$)", re.I),
]


def _classify_title(title: str) -> Optional[Tuple[str, str]]:
    """Returns (deal_type, default_status) or None."""
    for kind, rx, default_status in MA_CLASSIFIERS:
        if rx.search(title or ""):
            return kind, default_status
    return None


def _detect_status_bump(text: str, current: str) -> Optional[str]:
    """Returns a new status if `text` indicates progression from `current`."""
    order = ["rumor", "announced", "sebi_filed", "approved", "effective", "withdrawn"]
    cur_idx = order.index(current) if current in order else 0
    for rx, target in STATUS_PROGRESSION:
        if rx.search(text or ""):
            t_idx = order.index(target)
            if target == "withdrawn" or t_idx > cur_idx:
                return target
    return None


def _extract_deal_value_cr(text: str) -> Optional[float]:
    """Returns deal value in INR crore."""
    if not text:
        return None
    for rx, mult in VALUE_RE:
        m = rx.search(text)
        if m:
            try:
                num = float(m.group(1).replace(",", ""))
                return round(num * mult, 2)
            except ValueError:
                continue
    return None


def _extract_stake_pct(text: str) -> Optional[float]:
    if not text:
        return None
    m = STAKE_RE.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1))
        return v if 0 < v <= 100 else None
    except ValueError:
        return None


def _extract_consideration(text: str) -> Optional[str]:
    if not text:
        return None
    matched = [k for k, rx in CONSID_RE.items() if rx.search(text)]
    if "stock" in matched and "cash" in matched:
        return "mixed"
    if matched:
        return matched[0]
    return None


def _extract_parties(title: str) -> Tuple[Optional[str], Optional[str]]:
    if not title:
        return None, None
    for rx in PARTY_RE:
        m = rx.search(title)
        if m:
            a = m.group(1).strip().rstrip(".,")
            t = m.group(2).strip().rstrip(".,")
            if 2 < len(a) < 100 and 2 < len(t) < 100 and a.lower() != t.lower():
                return a, t
    return None, None


def _normalize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"\b(ltd|limited|inc|corporation|corp|plc|pvt|private|company|co\.?|the)\b", "", n)
    n = re.sub(r"[^a-z0-9]", "", n)
    return n


def _deal_id(acquirer: str, target: str, ann_date: Optional[date]) -> str:
    base = f"{_normalize_name(acquirer)}|{_normalize_name(target)}|{(ann_date or date.today()).isoformat()}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


# ---------- Schema ----------

def ensure_schema(db) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ma_deals (
                deal_id TEXT PRIMARY KEY,
                acquirer_name TEXT,
                acquirer_ticker TEXT,
                target_name TEXT,
                target_ticker TEXT,
                deal_type TEXT,
                deal_value_cr REAL,
                consideration_type TEXT,
                stake_pct REAL,
                status TEXT,
                announcement_date TEXT,
                expected_close_date TEXT,
                sector_acquirer TEXT,
                sector_target TEXT,
                source_url TEXT,
                source_tier INTEGER,
                alpha_score REAL,
                payload_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ma_status ON ma_deals(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ma_ann_date ON ma_deals(announcement_date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ma_acquirer_ticker ON ma_deals(acquirer_ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ma_target_ticker ON ma_deals(target_ticker)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ma_deal_events (
                deal_id TEXT NOT NULL,
                event_date TEXT NOT NULL,
                event_type TEXT NOT NULL,
                description TEXT,
                source_url TEXT,
                PRIMARY KEY (deal_id, event_date, event_type)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ma_events_deal ON ma_deal_events(deal_id, event_date)")
        db.conn.commit()
    except Exception as e:
        logger.warning(f"ma_scraper.ensure_schema: {e}")


# ---------- Upsert / dedup ----------

def _find_matching_deal(db, acquirer: str, target: str, ann_date: Optional[date], window_days: int = 7) -> Optional[str]:
    """Look up an existing deal_id with same acquirer+target within window. Returns deal_id or None."""
    a_norm = _normalize_name(acquirer)
    t_norm = _normalize_name(target)
    if not a_norm or not t_norm:
        return None
    try:
        cur = db.conn.cursor()
        if ann_date:
            lo = (ann_date - timedelta(days=window_days)).isoformat()
            hi = (ann_date + timedelta(days=window_days)).isoformat()
            cur.execute(
                """SELECT deal_id, acquirer_name, target_name FROM ma_deals
                   WHERE announcement_date BETWEEN ? AND ?""",
                (lo, hi),
            )
        else:
            cur.execute(
                """SELECT deal_id, acquirer_name, target_name FROM ma_deals
                   WHERE date(announcement_date) >= date('now', '-30 days')""",
            )
        for row in cur.fetchall():
            if (_normalize_name(row[1]) == a_norm and _normalize_name(row[2]) == t_norm):
                return row[0]
    except Exception as e:
        logger.debug(f"_find_matching_deal: {e}")
    return None


def upsert_deal(db, *, acquirer: str, target: str, deal_type: str,
                status: str, announcement_date: Optional[date],
                deal_value_cr: Optional[float] = None,
                consideration_type: Optional[str] = None,
                stake_pct: Optional[float] = None,
                source_url: Optional[str] = None,
                source_tier: int = 2,
                acquirer_ticker: Optional[str] = None,
                target_ticker: Optional[str] = None,
                payload: Optional[Dict] = None) -> Optional[str]:
    """Insert or merge into ma_deals. Returns deal_id."""
    if not acquirer or not target:
        return None
    existing_id = _find_matching_deal(db, acquirer, target, announcement_date)
    payload_json = json.dumps(payload or {}, default=str)

    try:
        cur = db.conn.cursor()
        if existing_id:
            # Merge: take non-null new values, advance status if newer is more advanced
            cur.execute(
                "SELECT status, deal_value_cr, consideration_type, stake_pct FROM ma_deals WHERE deal_id = ?",
                (existing_id,),
            )
            row = cur.fetchone()
            if row:
                cur_status = row["status"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
                cur_val    = row["deal_value_cr"] if hasattr(row, "keys") else row[1]
                cur_cons   = row["consideration_type"] if hasattr(row, "keys") else row[2]
                cur_stake  = row["stake_pct"] if hasattr(row, "keys") else row[3]

                bumped = _detect_status_bump(payload.get("text", "") if payload else "", cur_status)
                new_status = bumped or (status if _status_rank(status) > _status_rank(cur_status) else cur_status)

                cur.execute(
                    """UPDATE ma_deals SET
                          status = ?,
                          deal_value_cr = COALESCE(?, deal_value_cr),
                          consideration_type = COALESCE(?, consideration_type),
                          stake_pct = COALESCE(?, stake_pct),
                          updated_at = CURRENT_TIMESTAMP
                       WHERE deal_id = ?""",
                    (new_status,
                     deal_value_cr if deal_value_cr else None,
                     consideration_type if consideration_type else None,
                     stake_pct if stake_pct else None,
                     existing_id),
                )
                _append_deal_event(db, existing_id, announcement_date or date.today(),
                                   event_type=f"{deal_type}:{new_status}",
                                   description=(payload or {}).get("title"),
                                   source_url=source_url)
                db.conn.commit()
                return existing_id

        # New deal
        deal_id = _deal_id(acquirer, target, announcement_date)
        cur.execute(
            """INSERT OR IGNORE INTO ma_deals
                (deal_id, acquirer_name, acquirer_ticker, target_name, target_ticker,
                 deal_type, deal_value_cr, consideration_type, stake_pct, status,
                 announcement_date, source_url, source_tier, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (deal_id, acquirer, acquirer_ticker, target, target_ticker,
             deal_type, deal_value_cr, consideration_type, stake_pct, status,
             (announcement_date or date.today()).isoformat(),
             source_url, source_tier, payload_json),
        )
        _append_deal_event(db, deal_id, announcement_date or date.today(),
                           event_type=f"{deal_type}:{status}",
                           description=(payload or {}).get("title"),
                           source_url=source_url)
        db.conn.commit()
        return deal_id
    except Exception as e:
        logger.warning(f"upsert_deal {acquirer}/{target}: {e}")
        try:
            db.conn.rollback()
        except Exception:
            pass
        return None


def _status_rank(s: str) -> int:
    order = {"rumor": 0, "announced": 1, "sebi_filed": 2, "approved": 3, "effective": 4, "withdrawn": 5}
    return order.get(s, 0)


def _append_deal_event(db, deal_id: str, event_date: date,
                       event_type: str, description: Optional[str], source_url: Optional[str]) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            """INSERT OR IGNORE INTO ma_deal_events (deal_id, event_date, event_type, description, source_url)
               VALUES (?, ?, ?, ?, ?)""",
            (deal_id, event_date.isoformat() if isinstance(event_date, date) else str(event_date),
             event_type, description, source_url),
        )
    except Exception as e:
        logger.debug(f"_append_deal_event: {e}")


# ---------- Filings feeder (BSE corporate announcements) ----------

BSE_ANN_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_HEADERS = {"User-Agent": "Mozilla/5.0 alphaevent/0.1", "Referer": "https://www.bseindia.com/"}


def fetch_filings_recent(days: int = 7, limit: int = 200) -> List[Dict]:
    """Pull recent BSE-wide corporate announcements (no per-scrip iteration).
    Reuses the same endpoint as sebi_disclosures but with strScrip='' for the
    full firehose. Filters to titles matching M&A classifiers.
    """
    since = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    today = date.today().strftime("%Y%m%d")
    out: List[Dict] = []
    try:
        from net import request_with_retry
        from ratelimit import get_bucket
        bucket = get_bucket()
        if not bucket.acquire("bse", 1.0, max_wait=5.0):
            return []
        resp = request_with_retry(
            "GET", BSE_ANN_API, source="bse",
            params={"pageno": 1, "strCat": "-1", "strPrevDate": since,
                    "strScrip": "", "strSearch": "P", "strToDate": today,
                    "strType": "C", "subcategory": "-1"},
            headers=BSE_HEADERS, attempts=2, timeout=15,
        )
        data = resp.json()
        rows = list(data.get("Table") or data.get("table") or [])[:limit]
    except Exception as e:
        logger.debug(f"fetch_filings_recent: {e}")
        return []

    for row in rows:
        title = (row.get("HEADLINE") or row.get("NEWSSUB") or "").strip()
        cls = _classify_title(title)
        if not cls:
            continue
        deal_type, default_status = cls
        company_name = (row.get("SLONGNAME") or row.get("NAME") or "").strip() or None
        scrip = (row.get("SCRIP_CD") or "").strip() or None
        link = row.get("ATTACHMENTNAME") or row.get("ANNOUNCEMENT_URL") or ""
        if link and not link.startswith("http"):
            link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{link}"
        dt_raw = row.get("NEWS_DT") or row.get("NEWSDATE") or row.get("DT_TM")
        try:
            ann_dt = datetime.strptime(str(dt_raw)[:10], "%Y-%m-%d").date() if dt_raw else date.today()
        except Exception:
            ann_dt = date.today()
        # The filing's ticker is the *announcer*. For acquisitions/open-offers it's the acquirer;
        # for delistings/schemes it's the issuer. We treat the announcer as one side and try to
        # extract the counterparty from the headline.
        a_party, t_party = _extract_parties(title)
        if not a_party:
            a_party = company_name
        if not t_party:
            t_party = company_name  # at least one side is the issuer
        out.append({
            "title": title,
            "deal_type": deal_type,
            "status": default_status,
            "announcement_date": ann_dt,
            "acquirer": a_party,
            "target": t_party,
            "acquirer_ticker": None,
            "target_ticker": (company_name and scrip and company_name.upper()) or None,
            "deal_value_cr": _extract_deal_value_cr(title),
            "stake_pct": _extract_stake_pct(title),
            "consideration_type": _extract_consideration(title),
            "source_url": link or None,
            "source_tier": 1,
            "text": title,
        })
    return out


# ---------- Events-table feeder (reuses main scraper's prior work) ----------

def _looks_like_commentary(title: str) -> bool:
    """Filter out market commentary / analysis pieces that mention M&A but
    aren't actual deals. e.g. 'Top 10 mergers of 2025', 'rally 9% after demerger'.

    Only the most obvious tells — leave borderline cases for the party
    extraction to filter, so we don't lose real deals.
    """
    t = (title or "").lower()
    junk = [
        "top 10", "top 5", "best of", "year in review",
        "shares rally", "shares jump", "shares fall", "share price",
        "should you buy", "should you sell",
        "what they mean", "explained:", "what's driving", "what is driving",
        "navigating the", "deal value rises", "outlook for",
        "year ago", "five-year", "10-year", "decade of",
    ]
    return any(j in t for j in junk)


def _looks_like_company(name: str) -> bool:
    """Crude validity check for an extracted party name."""
    if not name or len(name) < 3 or len(name) > 80:
        return False
    n = name.strip()
    if n.lower() in ("share", "shares", "stake", "deal", "the company", "company",
                     "promoter", "promoters", "founder", "founders", "fund",
                     "based entity", "the firm"):
        return False
    # Must contain at least one letter and not be mostly numbers/punctuation
    if not re.search(r"[A-Za-z]{3,}", n):
        return False
    # Reject extracted strings that contain "Rs " or "%" — they're price fragments
    if re.search(r"\bRs\s*[\d,]+|\d+\s*%|\d+\s*cr(?:ore)?", n, re.I):
        return False
    return True


def fetch_from_events_table(db, days: int = 730, limit: int = 500) -> List[Dict]:
    """Reuse events already classified by the main scraper. The `events` table
    is populated by hybrid_scraper from RSS + news; this feeder lifts
    M&A-flavored rows into the ma_deals lifecycle.

    Default window is 2 years to give the tracker decent depth from existing data.
    """
    out: List[Dict] = []
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT event_id, title, summary, source, link, event_type,
                      sentiment, magnitude, impact_score, companies, published_at, created_at
               FROM events
               WHERE (event_type IN ('merger','acquisition')
                      OR LOWER(title) LIKE '%acqui%'
                      OR LOWER(title) LIKE '%merger%'
                      OR LOWER(title) LIKE '%open offer%'
                      OR LOWER(title) LIKE '%takeover%'
                      OR LOWER(title) LIKE '%scheme of arrangement%'
                      OR LOWER(title) LIKE '%delist%'
                      OR LOWER(title) LIKE '%amalgamation%')
                 AND date(created_at) >= date('now', ?)
               ORDER BY created_at DESC
               LIMIT ?""",
            (f"-{days} days", limit),
        )
        rows = cur.fetchall()
    except Exception as e:
        logger.debug(f"fetch_from_events_table: {e}")
        return []
    for r in rows:
        title = r["title"] or ""
        summary = r["summary"] or ""
        blob = title + " " + summary
        if _looks_like_commentary(title):
            continue
        cls = _classify_title(title) or _classify_title(blob)
        if not cls:
            continue
        deal_type, default_status = cls

        # Prefer the `companies` column (NLP-extracted by main scraper) over regex.
        comps_raw = (r["companies"] or "").strip()
        comps = [c.strip() for c in comps_raw.split(",") if c.strip() and _looks_like_company(c.strip())]
        a_party = t_party = None
        if len(comps) >= 2:
            a_party, t_party = comps[0], comps[1]
        elif len(comps) == 1:
            # Single company: try regex to find the other side
            ra, rt = _extract_parties(title)
            if not ra:
                ra, rt = _extract_parties(blob)
            if ra and rt and _looks_like_company(ra) and _looks_like_company(rt):
                # If extracted matches the single company, use both regex results
                if comps[0].lower() in (ra.lower(), rt.lower()):
                    a_party, t_party = ra, rt
                else:
                    a_party, t_party = comps[0], rt
            else:
                # Mark target as undisclosed but keep the deal so it surfaces
                a_party, t_party = comps[0], "(undisclosed)"
        else:
            # No companies — try regex
            ra, rt = _extract_parties(title)
            if not ra:
                ra, rt = _extract_parties(blob)
            if ra and rt and _looks_like_company(ra) and _looks_like_company(rt):
                a_party, t_party = ra, rt

        if not a_party or not t_party:
            continue
        try:
            pub = r["published_at"] or r["created_at"]
            ann_dt = datetime.strptime(str(pub)[:10], "%Y-%m-%d").date() if pub else date.today()
        except Exception:
            ann_dt = date.today()
        out.append({
            "title": title,
            "deal_type": deal_type,
            "status": default_status,
            "announcement_date": ann_dt,
            "acquirer": a_party,
            "target": t_party,
            "deal_value_cr": _extract_deal_value_cr(blob),
            "stake_pct": _extract_stake_pct(blob),
            "consideration_type": _extract_consideration(blob),
            "source_url": r["link"],
            "source_tier": 2,
            "text": blob,
        })
    return out


# ---------- News feeder (RSS) ----------

NEWS_FEEDS = [
    "https://www.moneycontrol.com/rss/business.xml",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.livemint.com/rss/companies",
    "https://www.business-standard.com/rss/markets-106.rss",
]


def fetch_news_recent(limit: int = 50) -> List[Dict]:
    """Scan known RSS feeds for M&A-flavored items."""
    try:
        import feedparser
    except ImportError:
        return []
    out: List[Dict] = []
    for url in NEWS_FEEDS:
        try:
            d = feedparser.parse(url)
        except Exception:
            continue
        for entry in (getattr(d, "entries", []) or [])[:50]:
            title = (entry.get("title") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            blob = title + " " + summary
            cls = _classify_title(title) or _classify_title(blob)
            if not cls:
                continue
            deal_type, default_status = cls
            try:
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                ann_dt = date(*pub[:3]) if pub else date.today()
            except Exception:
                ann_dt = date.today()
            a_party, t_party = _extract_parties(title) or _extract_parties(blob)
            if not a_party or not t_party:
                continue  # news without identifiable parties is too noisy to keep
            out.append({
                "title": title,
                "deal_type": deal_type,
                "status": "rumor",  # news-only, downgrade from default
                "announcement_date": ann_dt,
                "acquirer": a_party,
                "target": t_party,
                "deal_value_cr": _extract_deal_value_cr(blob),
                "stake_pct": _extract_stake_pct(blob),
                "consideration_type": _extract_consideration(blob),
                "source_url": entry.get("link"),
                "source_tier": 2,
                "text": blob,
            })
            if len(out) >= limit:
                return out
    return out


# ---------- Refresh orchestration ----------

def refresh_ma_deals(db, days: int = 7) -> Dict:
    """Top-level entry: pull filings + news, merge into ma_deals, score with the
    M&A alpha scorer if available. Safe to call repeatedly — dedup is built-in.
    """
    ensure_schema(db)
    counts = {"filings": 0, "news": 0, "events_table": 0, "new": 0, "merged": 0}

    # Events table (primary source — leverages prior scraper classification)
    from_events = fetch_from_events_table(db, days=max(days * 25, 730), limit=500)
    counts["events_table"] = len(from_events)
    for f in from_events:
        deal_id = upsert_deal(
            db,
            acquirer=f["acquirer"], target=f["target"],
            deal_type=f["deal_type"], status=f["status"],
            announcement_date=f["announcement_date"],
            deal_value_cr=f.get("deal_value_cr"),
            consideration_type=f.get("consideration_type"),
            stake_pct=f.get("stake_pct"),
            source_url=f.get("source_url"),
            source_tier=f.get("source_tier", 2),
            payload=f,
        )
        if deal_id:
            counts["new"] += 1

    # Filings (BSE — currently broken without per-scrip queries; kept for future)
    filings = fetch_filings_recent(days=days)
    counts["filings"] = len(filings)
    for f in filings:
        deal_id = upsert_deal(
            db,
            acquirer=f["acquirer"], target=f["target"],
            deal_type=f["deal_type"], status=f["status"],
            announcement_date=f["announcement_date"],
            deal_value_cr=f.get("deal_value_cr"),
            consideration_type=f.get("consideration_type"),
            stake_pct=f.get("stake_pct"),
            source_url=f.get("source_url"),
            source_tier=f.get("source_tier", 1),
            acquirer_ticker=f.get("acquirer_ticker"),
            target_ticker=f.get("target_ticker"),
            payload=f,
        )
        if deal_id:
            counts["new"] += 1

    # News
    news = fetch_news_recent()
    counts["news"] = len(news)
    for n in news:
        deal_id = upsert_deal(
            db,
            acquirer=n["acquirer"], target=n["target"],
            deal_type=n["deal_type"], status=n["status"],
            announcement_date=n["announcement_date"],
            deal_value_cr=n.get("deal_value_cr"),
            consideration_type=n.get("consideration_type"),
            stake_pct=n.get("stake_pct"),
            source_url=n.get("source_url"),
            source_tier=n.get("source_tier", 2),
            payload=n,
        )
        if deal_id:
            counts["merged"] += 1

    # Score
    try:
        from ma_alpha_scorer import score_all_active
        counts["scored"] = score_all_active(db)
    except Exception as e:
        logger.debug(f"ma scoring skipped: {e}")
        counts["scored"] = 0

    return counts


# ---------- Read helpers (used by API) ----------

# Quality filters: titles that look like personal-finance, lifestyle, opinion,
# or any of the patterns _looks_like_commentary catches. Applied at READ time
# so junk that's already in the DB stops surfacing without a full rescrape.
_JUNK_TITLE_PATTERNS = (
    # Personal finance / lifestyle / explainer
    "estate planning", "personal finance", "tax planning", "tax-saving",
    "retirement plan", "child education plan", "wedding plan",
    "do children have rights", "do you have rights",
    "explained:", "explainer:", "what is", "what are", "how to", "guide to",
    "should you buy", "should you invest", "is it time",
    "how will it impact", "how does it impact", "how it impacts",
    "horoscope", "lifestyle", "recipe", "travel", "movie", "festival",
    "education news", "career news", "job alert", "fashion",
    "live updates", "live news", "live blog", "live coverage",
    "top 10", "top 5", "best of", "year in review",
    # Earnings-results articles (not M&A — these get mis-tagged when text
    # contains the word "acquired" / "merge" in non-deal contexts)
    "q1 results", "q2 results", "q3 results", "q4 results",
    "q1 fy", "q2 fy", "q3 fy", "q4 fy", "q1 net", "q2 net", "q3 net", "q4 net",
    "earnings result", "earnings report", "results: net", "net profit jumps",
    "net profit rises", "net profit falls", "results:",
    "shares jump", "shares rally", "shares fall", "shares slump",
    "shares lift", "shares dip", "share price", "stock price",
    "stock jumps", "stock rallies", "stock falls",
    # Aggregate / market commentary
    "deals lift", "deals lifted", "mergers and acquisitions market",
    "ma activity", "m&a activity", "m&a market", "wrap:", "weekly wrap",
    "deal value rises", "deal value falls", "outlook for", "navigating the",
    # Misc financial junk
    "rs 1 lakh sip", "sip calculator", "loan emi", "best mutual funds",
    "best stocks", "stocks to buy", "stocks to watch", "small-cap to watch",
    "long-term picks", "five-year", "10-year", "decade of",
    "rs 1 lakh to", "rs 1 crore to", "in 24 years", "in 10 years",
    "in 5 years", "delivers 1,", "multibagger", "multi-bagger",
    "biggest media mergers", "biggest deals", "biggest m&a",
    "rbi's power play", "rbi power play",
    "stock crashes", "stock surges", "stock soars", "stock plunges",
    "to delist", "delisting from", "delisted from", "exits nifty", "exits sensex",
    "rises after inking", "falls after inking",
    # Non-finance content
    "cricket", "ipl", "olympic", "election result", "weather forecast",
)


def _is_junk_deal(row: Dict) -> bool:
    """Reject deals that look like misclassified personal-finance / lifestyle
    articles. Real M&A deals have either a non-empty target OR a deal_value_cr
    and don't carry junk-pattern phrases in the title.

    We're conservative: a deal must violate at least two quality signals to be
    dropped, so we don't accidentally cull edge-case real deals.
    """
    payload = {}
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except Exception:
        pass
    title = (payload.get("title") or "").lower()
    target = (row.get("target_name") or payload.get("target") or "").strip().lower()
    acquirer = (row.get("acquirer_name") or payload.get("acquirer") or "").strip().lower()
    value_cr = row.get("deal_value_cr") or 0
    try:
        value_cr = float(value_cr or 0)
    except Exception:
        value_cr = 0

    # Hard junk: title contains an obvious lifestyle / explainer / results pattern
    if any(p in title for p in _JUNK_TITLE_PATTERNS):
        return True
    # Hallucinated mega-deals: ₹1.5L cr+ is almost certainly the NLP misreading
    # market-cap or volume figures. Real M&A reporting always names a target.
    if value_cr > 150000:
        return True
    # Promoter / share-release / dividend articles are NOT deals
    junk_phrases_strict = (
        "promoters release", "promoters pledged", "release of pledged",
        "rights issue", "bonus issue", "stock split announcement",
        "dividend:", "dividend announce", "ex-dividend", "record date",
        "demerger: when", "demerger: how", "demerger: what",
        "mutual fund reports", "fund reports disposal", "fund reports purchase",
        "completes acquisition" "" if False else "",   # keep "completes acquisition" — those ARE deals
        "annual general meeting", "agm date", "egm date",
    )
    if any(p in title for p in junk_phrases_strict if p):
        return True
    # IMPAL is a known false-positive extraction (it's "India" being NLP-tagged
    # as a ticker). Any deal where target=IMPAL is bogus.
    if target == "impal" or acquirer == "impal":
        return True
    fails = 0
    if not target or target in ("(undisclosed)", "undisclosed", "unknown", "n/a", "tbd"):
        fails += 1
    if not acquirer or len(acquirer) < 3:
        fails += 1
    # No deal value AND no clear target = nothing-burger
    if value_cr <= 0 and (not target or target.startswith("(")):
        fails += 1
    # Unreasonably-large deal_value paired with weak metadata is a red flag
    if value_cr > 50000 and (not target or target.startswith("(")):
        fails += 1
    return fails >= 2


def list_deals(db, status: Optional[str] = None, sector: Optional[str] = None,
               min_value_cr: Optional[float] = None, days: int = 365, limit: int = 100) -> List[Dict]:
    ensure_schema(db)
    cur = db.conn.cursor()
    where = ["date(announcement_date) >= date('now', ?)"]
    args: List = [f"-{days} days"]
    if status:
        where.append("status = ?")
        args.append(status)
    if sector:
        where.append("(sector_acquirer = ? OR sector_target = ?)")
        args.extend([sector, sector])
    if min_value_cr is not None:
        where.append("COALESCE(deal_value_cr, 0) >= ?")
        args.append(min_value_cr)
    # Overfetch then quality-filter in Python so we still return `limit` clean
    # rows even if many in the window are junk.
    fetch_limit = max(int(limit) * 4, 200)
    args.append(fetch_limit)
    cur.execute(
        f"""SELECT * FROM ma_deals WHERE {' AND '.join(where)}
           ORDER BY date(announcement_date) DESC, COALESCE(alpha_score, 0) DESC
           LIMIT ?""",
        args,
    )
    rows = [dict(r) for r in cur.fetchall()]
    clean = [r for r in rows if not _is_junk_deal(r)]
    return clean[:limit]


def deal_by_id(db, deal_id: str) -> Optional[Dict]:
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute("SELECT * FROM ma_deals WHERE deal_id = ?", (deal_id,))
    row = cur.fetchone()
    if not row:
        return None
    deal = dict(row)
    cur.execute(
        "SELECT event_date, event_type, description, source_url FROM ma_deal_events WHERE deal_id = ? ORDER BY event_date ASC",
        (deal_id,),
    )
    deal["events"] = [dict(r) for r in cur.fetchall()]
    try:
        deal["payload"] = json.loads(deal.pop("payload_json") or "{}")
    except Exception:
        deal["payload"] = {}
    return deal


def deals_by_ticker(db, ticker: str, limit: int = 50) -> List[Dict]:
    ensure_schema(db)
    cur = db.conn.cursor()
    t = (ticker or "").upper().strip()
    cur.execute(
        """SELECT * FROM ma_deals
           WHERE acquirer_ticker = ? OR target_ticker = ?
           ORDER BY date(announcement_date) DESC LIMIT ?""",
        (t, t, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def ma_stats(db, days: int = 90) -> Dict:
    ensure_schema(db)
    cur = db.conn.cursor()
    cur.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(deal_value_cr), 0) AS total_value_cr,
                  SUM(CASE WHEN status IN ('announced','sebi_filed','approved') THEN 1 ELSE 0 END) AS active,
                  SUM(CASE WHEN status = 'sebi_filed' THEN 1 ELSE 0 END) AS sebi_filed
           FROM ma_deals WHERE date(announcement_date) >= date('now', ?)""",
        (f"-{days} days",),
    )
    row = cur.fetchone()
    out = dict(row) if row else {"total": 0, "total_value_cr": 0, "active": 0, "sebi_filed": 0}
    cur.execute(
        """SELECT COALESCE(sector_target, sector_acquirer, 'Unclassified') AS sector, COUNT(*) AS n
           FROM ma_deals WHERE date(announcement_date) >= date('now', ?)
           GROUP BY 1 ORDER BY n DESC LIMIT 5""",
        (f"-{days} days",),
    )
    out["top_sectors"] = [dict(r) for r in cur.fetchall()]
    return out
