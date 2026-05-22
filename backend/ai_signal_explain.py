"""Per-signal "why this matters" explanation.

Each new signal in signals table gets a paragraph-length explanation written
by Groq, falling back to a programmatic template when the budget is tight.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, Optional

from backend.ai_groq import groq_chat

logger = logging.getLogger(__name__)

_SCHEMA_ENSURED = False


def _ensure_schema(db) -> None:
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return
    try:
        cur = db.conn.cursor()
        for stmt in (
            "ALTER TABLE signals ADD COLUMN explanation TEXT",
            "ALTER TABLE signals ADD COLUMN explanation_at TIMESTAMP",
            "ALTER TABLE signals ADD COLUMN explanation_source TEXT",  # 'groq' | 'template'
        ):
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError:
                pass
        db.conn.commit()
        _SCHEMA_ENSURED = True
    except Exception as e:
        logger.warning("ai_signal_explain schema ensure failed: %s", e)


def _load_signal(db, *, signal_id: Optional[int] = None,
                 event_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        cur = db.conn.cursor()
        if event_id is not None:
            row = cur.execute(
                "SELECT id, ticker, company, event_type, sentiment, alpha_score, "
                "       confidence, regime, headline, COALESCE(explanation, '') AS explanation "
                "FROM signals WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        else:
            row = cur.execute(
                "SELECT id, ticker, company, event_type, sentiment, alpha_score, "
                "       confidence, regime, headline, COALESCE(explanation, '') AS explanation "
                "FROM signals WHERE id = ?",
                (signal_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return dict(row)
        except (TypeError, ValueError):
            return {
                "id": row[0], "ticker": row[1], "company": row[2],
                "event_type": row[3], "sentiment": row[4],
                "alpha_score": row[5], "confidence": row[6],
                "regime": row[7], "headline": row[8], "explanation": row[9],
            }
    except Exception as e:
        logger.warning("ai_signal_explain load %s failed: %s", signal_id, e)
        return None


def _template_explain(sig: Dict[str, Any]) -> str:
    """Programmatic fallback — no LLM needed."""
    ticker = sig.get("ticker") or "—"
    et = (sig.get("event_type") or "event").replace("_", " ")
    sent = (sig.get("sentiment") or "neutral").lower()
    alpha = float(sig.get("alpha_score") or 0)
    regime = (sig.get("regime") or "").replace("_", " ").lower()
    sent_word = {
        "bullish": "supportive tailwind",
        "bearish": "negative headwind",
        "neutral": "watch-only signal",
    }.get(sent, "watch-only signal")
    band = ("high-conviction" if alpha >= 75 else
            "medium-conviction" if alpha >= 60 else "low-conviction")
    parts = [
        f"{ticker} flagged on a {et} catalyst with alpha {alpha:.0f}/100 "
        f"({band} {sent_word})."
    ]
    if regime:
        parts.append(f"Current regime: {regime}.")
    parts.append("Confirm with chart and position-size accordingly.")
    return " ".join(parts)


SYSTEM_PROMPT = (
    "You explain Indian stock market alpha signals to retail traders. "
    "2-3 sentences max. Explain (1) what triggered the signal, "
    "(2) why an experienced trader would care, (3) one specific risk. "
    "No hedging boilerplate, no disclaimers — those live in the footer."
)


def explain_signal_handler(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_schema(db)
    signal_id = payload.get("signal_id")
    event_id = payload.get("event_id")
    if not signal_id and not event_id:
        return {"ok": False, "error": "missing signal_id or event_id"}

    sig = _load_signal(
        db,
        signal_id=int(signal_id) if signal_id else None,
        event_id=str(event_id) if event_id else None,
    )
    if not sig:
        return {"ok": False, "error": f"signal not found (signal_id={signal_id}, event_id={event_id})"}
    if sig.get("explanation"):
        return {"ok": True, "skipped": "already_explained"}
    signal_id = sig["id"]

    user_prompt = (
        f"Ticker: {sig.get('ticker')}\n"
        f"Company: {sig.get('company') or ''}\n"
        f"Event type: {sig.get('event_type')}\n"
        f"Sentiment: {sig.get('sentiment')}\n"
        f"Alpha score: {float(sig.get('alpha_score') or 0):.0f}/100\n"
        f"Confidence: {float(sig.get('confidence') or 0):.2f}\n"
        f"Regime: {sig.get('regime')}\n"
        f"Headline: {(sig.get('headline') or '')[:300]}"
    )
    text = groq_chat(
        module="explain",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=160,
        est_tokens=600,
    )
    source = "groq"
    if not text:
        # Template fallback so every signal has SOMETHING
        text = _template_explain(sig)
        source = "template"

    try:
        cur = db.conn.cursor()
        cur.execute(
            "UPDATE signals SET explanation = ?, explanation_at = CURRENT_TIMESTAMP, "
            "explanation_source = ? WHERE id = ?",
            (text.strip(), source, signal_id),
        )
        db.conn.commit()
        return {"ok": True, "signal_id": signal_id, "source": source}
    except Exception as e:
        logger.warning("ai_signal_explain persist %s failed: %s", signal_id, e)
        return {"ok": False, "error": str(e)}
