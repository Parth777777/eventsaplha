"""Equity research report composer — pulls from every scraped table and
asks Groq to write the narrative sections (thesis + risk factors).

Sections (in order):
    1. snapshot       — sector, mcap, 1Y/5Y returns        (always shown)
    2. catalysts      — last 90d top events                 (always shown)
    3. financials     — revenue/margin/ROCE snapshot        (pro/starter)
    4. forensic       — promoter pledge, fraud signals      (pro/starter)
    5. peers          — 3-5 closest peers side-by-side      (pro/starter)
    6. thesis         — Groq AI: "why someone would buy"    (pro/starter)
    7. risks          — Groq AI: "what could go wrong"      (pro/starter)
    8. methodology    — disclaimer + how scoring works      (always shown)

Free preview returns sections 1, 2, 8 + the first sentence of section 6
(blurred in UI). Cached 24h server-side in equity_reports.

Usage from a Flask route:
    from backend.equity_research import build_report
    report = build_report(get_db(), ticker, include_narrative=True)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.ai_groq import groq_chat

logger = logging.getLogger(__name__)

FREE_SECTIONS = {'snapshot', 'catalysts', 'methodology'}
ALL_SECTIONS = ['snapshot', 'catalysts', 'financials', 'forensic',
                'peers', 'thesis', 'risks', 'methodology']

CACHE_TTL_HOURS = 24

_SCHEMA_ENSURED = False


def _ensure_schema(db) -> None:
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS equity_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                report_json TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tokens_used INTEGER DEFAULT 0
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_equity_reports_ticker "
            "ON equity_reports(ticker, generated_at)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS research_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                ticker TEXT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_usage_user "
            "ON research_usage(user_id, used_at)"
        )
        db.conn.commit()
        _SCHEMA_ENSURED = True
    except Exception as e:
        logger.warning("equity_research schema ensure failed: %s", e)


# ── Section composers (cheap — pure DB reads) ──────────────────────────

def _snapshot(db, ticker: str) -> Dict[str, Any]:
    """Section 1: company snapshot."""
    out: Dict[str, Any] = {'ticker': ticker}
    try:
        from stock_universe import STOCK_UNIVERSE  # type: ignore
        meta = STOCK_UNIVERSE.get(ticker.upper()) or {}
        out['company'] = meta.get('company') or meta.get('Company') or ticker
        out['sector']  = meta.get('sector')  or meta.get('Sector')  or 'Unknown'
        out['mcap_cr'] = meta.get('mcap_cr') or meta.get('MarketCap') or None
    except Exception:
        out.setdefault('company', ticker)
        out.setdefault('sector', 'Unknown')
    # Last close + 1Y/5Y returns — best effort from any available source
    try:
        cur = db.conn.cursor()
        row = cur.execute(
            "SELECT entry_price FROM signals WHERE ticker = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if row:
            out['last_close'] = float(row[0]) if row[0] else None
    except Exception:
        pass
    return out


def _catalysts(db, ticker: str, days: int = 90, limit: int = 10) -> List[Dict[str, Any]]:
    """Section 2: top recent catalysts ordered by alpha_score."""
    try:
        cur = db.conn.cursor()
        rows = cur.execute(
            """SELECT event_type, sentiment, alpha_score, headline, link,
                      created_at, COALESCE(explanation, '') AS explanation
               FROM signals
               WHERE ticker = ?
                 AND created_at >= datetime('now', ?)
               ORDER BY alpha_score DESC, created_at DESC
               LIMIT ?""",
            (ticker, f'-{int(days)} days', int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            try:
                d = dict(r)
            except (TypeError, ValueError):
                d = {
                    'event_type': r[0], 'sentiment': r[1],
                    'alpha_score': r[2], 'headline': r[3],
                    'link': r[4], 'created_at': r[5], 'explanation': r[6],
                }
            out.append(d)
        return out
    except Exception as e:
        logger.debug("catalysts lookup failed: %s", e)
        return []


def _financials(db, ticker: str) -> Dict[str, Any]:
    """Section 3: financial snapshot. Best-effort from fundamentals tables."""
    out: Dict[str, Any] = {}
    for tbl, fields in (
        ('fundamentals_snapshot', ['revenue_cr', 'pat_cr', 'op_margin_pct',
                                    'roce_pct', 'debt_to_equity', 'pe', 'pb']),
        ('fundamentals',          ['revenue', 'profit', 'margin', 'roce',
                                    'd_to_e', 'pe', 'pb']),
    ):
        try:
            cur = db.conn.cursor()
            row = cur.execute(
                f"SELECT {','.join(fields)} FROM {tbl} "
                f"WHERE ticker = ? ORDER BY rowid DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            if row:
                try:
                    out.update(dict(row))
                except (TypeError, ValueError):
                    for i, f in enumerate(fields):
                        out[f] = row[i]
                if out:
                    return out
        except Exception:
            continue
    return out


def _forensic(db, ticker: str) -> List[Dict[str, Any]]:
    """Section 4: forensic flags."""
    out: List[Dict[str, Any]] = []
    for tbl, fields in (
        ('article_forensics', ['flag_type', 'severity', 'reasoning', 'created_at']),
        ('promoter_events',   ['event_type', 'change_pct', 'detail', 'event_date']),
        ('manipulation_patterns', ['pattern', 'severity', 'detail', 'detected_at']),
    ):
        try:
            cur = db.conn.cursor()
            rows = cur.execute(
                f"SELECT {','.join(fields)} FROM {tbl} "
                f"WHERE ticker = ? ORDER BY rowid DESC LIMIT 5",
                (ticker,),
            ).fetchall()
            for r in rows:
                try:
                    d = dict(r)
                except (TypeError, ValueError):
                    d = {f: r[i] for i, f in enumerate(fields)}
                d['source_table'] = tbl
                out.append(d)
        except Exception:
            continue
    return out


def _peers(db, ticker: str) -> List[Dict[str, Any]]:
    """Section 5: peer comparison."""
    try:
        from stock_universe import STOCK_UNIVERSE  # type: ignore
        meta = STOCK_UNIVERSE.get(ticker.upper()) or {}
        sector = meta.get('sector') or meta.get('Sector')
        if not sector:
            return []
        peers = [
            {'ticker': t, 'company': (m.get('company') or m.get('Company') or t)}
            for t, m in STOCK_UNIVERSE.items()
            if (m.get('sector') or m.get('Sector')) == sector and t.upper() != ticker.upper()
        ]
        return peers[:5]
    except Exception:
        return []


# ── Narrative sections (Groq-backed) ───────────────────────────────────

_THESIS_SYSTEM = (
    "You write an investment thesis paragraph for an Indian retail trader. "
    "3-4 sentences. Lead with the most compelling bull case in plain English. "
    "Cite concrete numbers from the context. No hedging boilerplate, no "
    "'consult your advisor' — the disclaimer is rendered separately."
)
_RISKS_SYSTEM = (
    "You write a risk factors paragraph for an Indian retail trader. "
    "3-4 sentences. Name 2-3 specific risks tied to the stock + sector "
    "+ recent catalysts. Plain English, concrete, no boilerplate."
)


def _narrative(db, ticker: str, snapshot: Dict, catalysts: List,
               financials: Dict, forensic: List, peers: List) -> Dict[str, Any]:
    """Build the AI-written thesis + risks. Returns {} if Groq refused
    (governor exhausted or no API key); caller falls back to template."""
    ctx_lines: List[str] = [
        f"Ticker: {ticker}",
        f"Company: {snapshot.get('company', ticker)}",
        f"Sector: {snapshot.get('sector', 'Unknown')}",
    ]
    if snapshot.get('mcap_cr'):
        ctx_lines.append(f"Market cap: ₹{snapshot['mcap_cr']} cr")
    if snapshot.get('last_close'):
        ctx_lines.append(f"Last close: ₹{snapshot['last_close']:.2f}")

    if catalysts:
        ctx_lines.append("\nTop recent catalysts (last 90d):")
        for c in catalysts[:6]:
            score = c.get('alpha_score') or 0
            ctx_lines.append(
                f"- {c.get('event_type','event')} ({c.get('sentiment','neutral')}, "
                f"α={float(score):.0f}): {(c.get('headline') or '')[:140]}"
            )

    if financials:
        kvs = [f"{k}={v}" for k, v in financials.items() if v is not None]
        if kvs:
            ctx_lines.append("\nFinancials: " + " · ".join(kvs[:8]))

    if forensic:
        ctx_lines.append(f"\nForensic flags ({len(forensic)} on record):")
        for f in forensic[:4]:
            ctx_lines.append(
                f"- [{f.get('source_table')}] {f.get('flag_type') or f.get('event_type') or f.get('pattern')}: "
                f"{(f.get('reasoning') or f.get('detail') or '')[:140]}"
            )

    if peers:
        ctx_lines.append("\nPeers: " + ", ".join(p['ticker'] for p in peers))

    user_prompt = "\n".join(ctx_lines)

    thesis = groq_chat(
        module='reasoning',
        messages=[
            {"role": "system", "content": _THESIS_SYSTEM},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=240,
        est_tokens=1600,
        temperature=0.35,
    )
    risks = groq_chat(
        module='reasoning',
        messages=[
            {"role": "system", "content": _RISKS_SYSTEM},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=220,
        est_tokens=1600,
        temperature=0.4,
    )
    return {
        'thesis': (thesis or '').strip(),
        'risks':  (risks  or '').strip(),
    }


def _template_thesis(snapshot: Dict, catalysts: List, financials: Dict) -> str:
    """Programmatic fallback when Groq refuses."""
    parts = []
    company = snapshot.get('company') or snapshot.get('ticker')
    sector = snapshot.get('sector', '')
    parts.append(f"{company} sits in the {sector} sector.")
    if catalysts:
        top = catalysts[0]
        parts.append(
            f"The strongest recent catalyst (α={float(top.get('alpha_score') or 0):.0f}) "
            f"was a {top.get('event_type','event')} "
            f"with {top.get('sentiment','neutral')} sentiment."
        )
    if financials.get('roce_pct') or financials.get('roce'):
        roce = financials.get('roce_pct') or financials.get('roce')
        parts.append(f"ROCE stands at {roce}% — confirm the trend before sizing.")
    parts.append("Read the methodology link for how this signal was scored.")
    return " ".join(parts)


def _template_risks(forensic: List, catalysts: List) -> str:
    parts = []
    bears = [c for c in catalysts if (c.get('sentiment') or '') == 'bearish']
    if bears:
        parts.append(
            f"{len(bears)} bearish catalyst(s) in the last 90d — review them "
            f"before going long."
        )
    if forensic:
        parts.append(
            f"{len(forensic)} forensic flag(s) on record — promoter pledge, "
            f"insider exit, or accounting anomaly. Open the forensics tab."
        )
    if not parts:
        parts.append("No specific red flags surfaced in our scraped data, but "
                     "sector, macro, and liquidity risks always apply.")
    parts.append("Position-size accordingly.")
    return " ".join(parts)


# ── Top-level builder + cache ──────────────────────────────────────────

def _cache_lookup(db, ticker: str) -> Optional[Dict[str, Any]]:
    try:
        cur = db.conn.cursor()
        cutoff = (datetime.utcnow() - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
        row = cur.execute(
            "SELECT report_json, generated_at FROM equity_reports "
            "WHERE ticker = ? AND generated_at >= ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (ticker, cutoff),
        ).fetchone()
        if not row:
            return None
        return json.loads(row[0])
    except Exception:
        return None


def _cache_store(db, ticker: str, report: Dict[str, Any]) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO equity_reports (ticker, report_json) VALUES (?, ?)",
            (ticker, json.dumps(report, default=str)),
        )
        db.conn.commit()
    except Exception as e:
        logger.debug("equity_reports cache store failed: %s", e)


def build_report(db, ticker: str, *, include_narrative: bool = True) -> Dict[str, Any]:
    """Compose the full report. include_narrative=False is faster (no Groq).

    Returns a dict shaped:
        {
          'ticker': str,
          'cached': bool,
          'generated_at': iso,
          'sections': { snapshot, catalysts, financials, forensic, peers,
                        thesis, risks, methodology }
        }
    """
    _ensure_schema(db)
    ticker = (ticker or '').upper().strip()
    if not ticker:
        return {'error': 'ticker required'}

    cached = _cache_lookup(db, ticker)
    if cached:
        cached['cached'] = True
        return cached

    snapshot = _snapshot(db, ticker)
    catalysts = _catalysts(db, ticker, days=90, limit=10)
    financials = _financials(db, ticker)
    forensic = _forensic(db, ticker)
    peers = _peers(db, ticker)

    if include_narrative:
        nar = _narrative(db, ticker, snapshot, catalysts, financials, forensic, peers)
        if not nar.get('thesis'):
            nar['thesis'] = _template_thesis(snapshot, catalysts, financials)
        if not nar.get('risks'):
            nar['risks'] = _template_risks(forensic, catalysts)
    else:
        nar = {
            'thesis': _template_thesis(snapshot, catalysts, financials),
            'risks':  _template_risks(forensic, catalysts),
        }

    report = {
        'ticker': ticker,
        'cached': False,
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'sections': {
            'snapshot':    snapshot,
            'catalysts':   catalysts,
            'financials':  financials,
            'forensic':    forensic,
            'peers':       peers,
            'thesis':      nar['thesis'],
            'risks':       nar['risks'],
            'methodology': {
                'url': '/app/methodology.html',
                'disclaimer': (
                    "Informational only — not investment advice. AlphaEvent / "
                    "Tickwave is not a SEBI-registered investment adviser. "
                    "Past performance does not guarantee future results."
                ),
            },
        },
    }
    _cache_store(db, ticker, report)
    return report


def apply_tier_visibility(report: Dict[str, Any], tier: str) -> Dict[str, Any]:
    """Mutates report.sections to mark locked sections + blur narrative
    for free users. Returns the modified dict in-place.

    Free users see:  snapshot, catalysts, methodology + thesis_preview (1st sentence)
    Starter+Pro:     all 8 sections
    """
    if tier in ('starter', 'pro'):
        report['tier_view'] = tier
        report['locked_sections'] = []
        return report

    # Free / anon — keep snapshot/catalysts/methodology, lock the rest.
    locked = ['financials', 'forensic', 'peers', 'risks']
    secs = report.get('sections') or {}
    for k in locked:
        if k in secs:
            secs[k] = None
    # Show the first sentence of the thesis as a teaser, blur the rest
    thesis = (secs.get('thesis') or '').strip()
    if thesis:
        preview = thesis.split('. ')[0].rstrip('.').strip()
        if preview:
            secs['thesis_preview'] = preview + '…'
        secs['thesis'] = None
    report['tier_view'] = tier
    report['locked_sections'] = locked + ['thesis']
    report['upgrade_url'] = '/app/pricing.html'
    return report


def count_today(db, user_id: str) -> int:
    try:
        cur = db.conn.cursor()
        n = cur.execute(
            "SELECT COUNT(*) FROM research_usage WHERE user_id = ? "
            "AND used_at >= datetime('now', 'start of day')",
            (user_id,),
        ).fetchone()[0]
        return int(n or 0)
    except Exception:
        return 0


def record_usage(db, user_id: str, ticker: str) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO research_usage (user_id, ticker) VALUES (?, ?)",
            (user_id, ticker),
        )
        db.conn.commit()
    except Exception:
        pass
