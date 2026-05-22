"""Fundamental-analysis AI explainer + peer rank + score trajectory.

The Analyze button on stock pages calls /api/fundamentals/score first
(headline + components), then this module is hit for the value-added
layers that make the analysis feel like a desk report instead of a ratio
dump:

    /api/fundamentals/explain/<ticker>     — Groq-written narrative
    /api/fundamentals/peer-rank/<ticker>   — sector percentile
    /api/fundamentals/history/<ticker>     — score trajectory (sparkline)

All three are cached server-side. The explainer is the heaviest (one
Groq call); peer-rank and history are pure DB reads.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from backend.ai_groq import groq_chat

logger = logging.getLogger(__name__)

EXPLAIN_TTL_HOURS = 24

_SCHEMA_ENSURED = False


def _ensure_schema(db) -> None:
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return
    try:
        cur = db.conn.cursor()
        # AI explainer cache
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fundamentals_explain_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                score_at_compute INTEGER,
                explainer_json TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_fund_explain_ticker "
            "ON fundamentals_explain_cache(ticker, generated_at)"
        )
        # Score history (one row per ticker per call — bounded by API cache TTL)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fundamentals_score_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                score INTEGER NOT NULL,
                tier TEXT,
                snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_fund_history_ticker "
            "ON fundamentals_score_history(ticker, snapshot_at)"
        )
        db.conn.commit()
        _SCHEMA_ENSURED = True
    except Exception as e:
        logger.warning("fundamentals_ai schema ensure failed: %s", e)


# ── AI Explainer ───────────────────────────────────────────────────────

_EXPLAINER_SYSTEM = (
    "You explain fundamental-analysis scores for Indian retail traders. "
    "Output STRICT JSON ONLY (no prose around it) with EXACTLY these keys:\n"
    "{\n"
    '  "bottom_line": "1-2 sentence plain-English verdict",\n'
    '  "components": {\n'
    '    "valuation":      "1-sentence why it scored what it did",\n'
    '    "profitability":  "...",\n'
    '    "growth":         "...",\n'
    '    "leverage":       "...",\n'
    '    "cashflow":       "...",\n'
    '    "promoter_pledge":"...",\n'
    '    "institutional":  "..."\n'
    "  },\n"
    '  "risk_callout":  "1-sentence specific risk",\n'
    '  "action":        "1 concrete suggestion in plain English"\n'
    "}\n\n"
    "No hedging boilerplate. No 'consult your advisor'. Cite concrete numbers "
    "from the input. If a component has no data, write \"insufficient data\"."
)


def _cache_lookup(db, ticker: str, score: int) -> Optional[Dict[str, Any]]:
    try:
        cur = db.conn.cursor()
        cutoff = (datetime.utcnow() - timedelta(hours=EXPLAIN_TTL_HOURS)).isoformat()
        row = cur.execute(
            "SELECT explainer_json, generated_at, score_at_compute "
            "FROM fundamentals_explain_cache "
            "WHERE ticker = ? AND generated_at >= ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (ticker.upper(), cutoff),
        ).fetchone()
        if not row:
            return None
        # If the score drifted significantly since cache, invalidate.
        cached_score = int(row[2] or 0)
        if abs(cached_score - int(score or 0)) > 5:
            return None
        obj = json.loads(row[0])
        obj['cached'] = True
        obj['generated_at'] = str(row[1])
        return obj
    except Exception:
        return None


def _cache_store(db, ticker: str, score: int, obj: Dict[str, Any]) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO fundamentals_explain_cache "
            "(ticker, score_at_compute, explainer_json) VALUES (?, ?, ?)",
            (ticker.upper(), int(score or 0), json.dumps(obj, default=str)),
        )
        db.conn.commit()
    except Exception as e:
        logger.debug("explain cache store failed: %s", e)


def _template_explainer(score_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Programmatic fallback when Groq refuses. Reads the score components
    directly and writes a deterministic explainer."""
    comp = score_payload.get('components') or {}
    score = int(score_payload.get('score') or 0)
    tier = score_payload.get('tier') or 'unknown'
    label = score_payload.get('label') or ''
    positives = score_payload.get('positives') or []
    red_flags = score_payload.get('red_flags') or []

    def comp_line(key: str, fallback: str = "insufficient data") -> str:
        block = comp.get(key) or {}
        s = block.get('score')
        if s is None:
            return fallback
        if s > 5:
            return f"Strong contribution (+{s}). {fallback}"
        if s > 0:
            return f"Mildly positive (+{s})."
        if s == 0:
            return "Neutral — no data or right at sector average."
        if s > -5:
            return f"Mildly negative ({s})."
        return f"Drag on the score ({s})."

    bottom = (
        f"{score_payload.get('ticker','')} scored {score}/100 ({tier}). "
        f"{label}."
    )
    risk = (
        f"{red_flags[0]}." if red_flags
        else "No data-driven red flags from the score components, but sector and macro risks always apply."
    )
    action = (
        f"{positives[0]} — consider deeper diligence." if positives
        else "Treat this as a watch-only signal until the next data refresh."
    )
    return {
        'bottom_line': bottom,
        'components': {
            'valuation':       comp_line('valuation', 'Valuation reflects current PE/PB vs sector.'),
            'profitability':   comp_line('profitability', 'Profitability reflects ROE + operating margin.'),
            'growth':          comp_line('growth', 'Growth reflects revenue + EPS growth trend.'),
            'leverage':        comp_line('leverage', 'Leverage reflects debt-to-equity stance.'),
            'cashflow':        comp_line('cashflow', 'Cashflow reflects FCF generation.'),
            'promoter_pledge': comp_line('promoter_pledge', 'Promoter pledge data not yet ingested.'),
            'institutional':   comp_line('institutional', 'Institutional holding share trend.'),
        },
        'risk_callout': risk,
        'action':       action,
        'source':       'template',
    }


def explain(db, ticker: str, score_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Top-level: cache → Groq → template fallback. Always returns a valid
    JSON-shaped explainer (never raises)."""
    _ensure_schema(db)
    ticker = (ticker or '').upper().strip()
    score = int(score_payload.get('score') or 0)

    cached = _cache_lookup(db, ticker, score)
    if cached:
        return cached

    # Build the user prompt from the score payload
    comp = score_payload.get('components') or {}
    lines = [
        f"Ticker: {ticker}",
        f"Score: {score}/100 ({score_payload.get('tier') or 'unknown'} — {score_payload.get('label') or ''})",
        "",
        "Components:",
    ]
    for key in ('valuation', 'profitability', 'growth', 'leverage',
                'cashflow', 'promoter_pledge', 'institutional'):
        c = comp.get(key) or {}
        s = c.get('score')
        extras = {k: v for k, v in c.items() if k != 'score' and v is not None}
        extras_str = " · ".join(f"{k}={v}" for k, v in list(extras.items())[:4])
        lines.append(f"  {key}: {'+' if (s or 0) > 0 else ''}{s if s is not None else '—'}   {extras_str}")

    pos = score_payload.get('positives') or []
    if pos:
        lines.append("")
        lines.append("Positives observed:")
        for p in pos[:6]:
            lines.append(f"  - {p}")
    flags = score_payload.get('red_flags') or []
    if flags:
        lines.append("")
        lines.append("Red flags observed:")
        for f in flags[:6]:
            lines.append(f"  - {f}")
    user_prompt = "\n".join(lines)

    raw = groq_chat(
        module='reasoning',
        messages=[
            {'role': 'system', 'content': _EXPLAINER_SYSTEM},
            {'role': 'user',   'content': user_prompt},
        ],
        max_tokens=500,
        est_tokens=1600,
        temperature=0.3,
    )

    obj = None
    if raw:
        # Trim any prose around the JSON
        try:
            start = raw.find('{')
            end = raw.rfind('}')
            if start >= 0 and end > start:
                obj = json.loads(raw[start:end + 1])
        except Exception as e:
            logger.debug("explainer JSON parse failed: %s", e)

    if not obj or not isinstance(obj, dict) or 'bottom_line' not in obj:
        obj = _template_explainer(score_payload)
    else:
        obj.setdefault('source', 'groq')

    obj['cached'] = False
    obj['generated_at'] = datetime.utcnow().isoformat() + 'Z'
    _cache_store(db, ticker, score, obj)
    return obj


# ── Peer rank (sector percentile) ──────────────────────────────────────

def peer_rank(db, ticker: str, score: int) -> Dict[str, Any]:
    """Return where this score sits vs other tickers in the same sector
    based on cached fundamentals_score_history. Computed cheap — pure DB.
    """
    _ensure_schema(db)
    ticker = (ticker or '').upper().strip()
    try:
        from stock_universe import STOCK_UNIVERSE  # type: ignore
        meta = STOCK_UNIVERSE.get(ticker) or {}
        sector = meta.get('sector') or meta.get('Sector')
    except Exception:
        sector = None
    if not sector:
        return {'sector': None, 'percentile': None, 'rank': None, 'total': 0, 'peer_avg': None}

    try:
        peer_tickers = [
            t for t, m in STOCK_UNIVERSE.items()
            if (m.get('sector') or m.get('Sector')) == sector
        ]
    except Exception:
        peer_tickers = []

    if not peer_tickers:
        return {'sector': sector, 'percentile': None, 'rank': None, 'total': 0, 'peer_avg': None}

    # Find each peer's most-recent cached score (within last 14d)
    cutoff = (datetime.utcnow() - timedelta(days=14)).isoformat()
    try:
        cur = db.conn.cursor()
        placeholders = ','.join(['?'] * len(peer_tickers))
        rows = cur.execute(
            f"""SELECT ticker, MAX(snapshot_at) AS latest, score
                  FROM fundamentals_score_history
                 WHERE ticker IN ({placeholders})
                   AND snapshot_at >= ?
              GROUP BY ticker""",
            (*peer_tickers, cutoff),
        ).fetchall()
    except Exception:
        rows = []

    peer_scores: List[int] = []
    for r in rows:
        try:
            peer_scores.append(int(r[2] if not isinstance(r, dict) else r['score']))
        except Exception:
            continue

    if not peer_scores:
        return {
            'sector': sector, 'percentile': None, 'rank': None,
            'total': len(peer_tickers), 'peer_avg': None,
            'note': 'Peer history thin — keep analyzing peers to populate the rank.',
        }

    higher = sum(1 for s in peer_scores if s < int(score or 0))
    total = len(peer_scores)
    percentile = round(100.0 * higher / max(total, 1), 1)
    rank = total - higher  # 1 = best
    peer_avg = round(sum(peer_scores) / total, 1)
    return {
        'sector':     sector,
        'percentile': percentile,
        'rank':       rank,
        'total':      total,
        'peer_avg':   peer_avg,
    }


# ── Score trajectory ───────────────────────────────────────────────────

def snapshot_history(db, ticker: str, score: int, tier: str) -> None:
    """Persist a single (ticker, score, tier) row for trajectory charts.
    Called from the fundamentals_score endpoint after successful compute."""
    _ensure_schema(db)
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO fundamentals_score_history (ticker, score, tier) "
            "VALUES (?, ?, ?)",
            ((ticker or '').upper(), int(score or 0), tier or ''),
        )
        db.conn.commit()
    except Exception as e:
        logger.debug("score history insert failed: %s", e)


def history(db, ticker: str, days: int = 90) -> List[Dict[str, Any]]:
    """Return last N days of snapshots for the ticker, ordered ASC for chart."""
    _ensure_schema(db)
    try:
        cur = db.conn.cursor()
        cutoff = (datetime.utcnow() - timedelta(days=int(days))).isoformat()
        rows = cur.execute(
            "SELECT score, tier, snapshot_at FROM fundamentals_score_history "
            "WHERE ticker = ? AND snapshot_at >= ? "
            "ORDER BY snapshot_at ASC LIMIT 250",
            ((ticker or '').upper(), cutoff),
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append({
                    'score': int(r[0] if not isinstance(r, dict) else r['score']),
                    'tier':  r[1] if not isinstance(r, dict) else r['tier'],
                    'at':    str(r[2] if not isinstance(r, dict) else r['snapshot_at']),
                })
            except Exception:
                continue
        return out
    except Exception:
        return []


# ── Route registration (mounted from backend/api.py) ───────────────────

def register(app, get_db: Callable):
    from flask import jsonify, request

    @app.route('/api/fundamentals/explain/<ticker>', methods=['POST', 'GET'])
    def fundamentals_explain(ticker):
        """POST body: {score_payload: {...full score response data...}}
        or GET: re-fetches the score and explains. POST is preferred since
        it avoids a duplicate yfinance call.
        """
        db = get_db()
        body = request.get_json(silent=True) or {}
        score_payload = body.get('score_payload')
        if not score_payload:
            # Best-effort: try to reuse the cached score from api_v3
            try:
                from backend.api_v3 import fundamentals_score as _fs
                resp = _fs(ticker)
                payload = resp.get_json() if hasattr(resp, 'get_json') else None
                if payload and payload.get('success'):
                    score_payload = payload.get('data') or {}
            except Exception:
                pass
        if not score_payload or score_payload.get('score') is None:
            return jsonify({'success': False, 'error': 'no score payload available'}), 400
        result = explain(db, ticker, score_payload)
        return jsonify({'success': True, 'data': result})

    @app.route('/api/fundamentals/peer-rank/<ticker>', methods=['GET'])
    def fundamentals_peer_rank(ticker):
        db = get_db()
        score = int(request.args.get('score', 0))
        return jsonify({'success': True, 'data': peer_rank(db, ticker, score)})

    @app.route('/api/fundamentals/history/<ticker>', methods=['GET'])
    def fundamentals_history(ticker):
        db = get_db()
        days = int(request.args.get('days', 90))
        return jsonify({'success': True, 'data': history(db, ticker, days=days)})

    logger.info("fundamentals_ai registered: /api/fundamentals/explain /peer-rank /history")
