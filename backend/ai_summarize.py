"""Per-event 1-line AI summarization.

Drained from job_queue by the worker daemon. Writes the summary back to
events.ai_summary (column auto-added on first call).

Wire-up:
    On every event insert in api*.py / scrapers, call:
        from backend.scrape_runner import enqueue
        enqueue(db, 'summarize', {'event_id': new_event_id})

    register_queue_handler('summarize', summarize_event_handler) is done
    by scrape_runner._daemon_loop on startup.
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
        # Try to ADD COLUMN; ignore if already exists. SQLite < 3.35 has no
        # IF NOT EXISTS for ALTER, so catch and swallow the OperationalError.
        for stmt in (
            "ALTER TABLE events ADD COLUMN ai_summary TEXT",
            "ALTER TABLE events ADD COLUMN ai_summary_at TIMESTAMP",
        ):
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column exists
        db.conn.commit()
        _SCHEMA_ENSURED = True
    except Exception as e:
        logger.warning("ai_summarize schema ensure failed: %s", e)


def _load_event(db, *, event_pk: Optional[int] = None,
                event_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Load by integer PK (`events.id`) OR by business key (`events.event_id`)."""
    try:
        cur = db.conn.cursor()
        if event_id is not None:
            row = cur.execute(
                "SELECT id, ticker, headline, source, event_type, sentiment, "
                "       COALESCE(ai_summary, '') AS ai_summary "
                "FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        else:
            row = cur.execute(
                "SELECT id, ticker, headline, source, event_type, sentiment, "
                "       COALESCE(ai_summary, '') AS ai_summary "
                "FROM events WHERE id = ?",
                (event_pk,),
            ).fetchone()
        if not row:
            return None
        try:
            return dict(row)
        except (TypeError, ValueError):
            return {
                "id": row[0], "ticker": row[1], "headline": row[2],
                "source": row[3], "event_type": row[4],
                "sentiment": row[5], "ai_summary": row[6],
            }
    except Exception as e:
        logger.warning("ai_summarize load event %s failed: %s", event_id, e)
        return None


SYSTEM_PROMPT = (
    "You write 1-sentence plain-English summaries of Indian stock market events "
    "for retail traders. No hedging, no jargon, no headlines style. "
    "Maximum 22 words. Explain what happened AND why it matters."
)


def summarize_event_handler(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Queue handler: produces and persists ai_summary for one event.

    Accepts either:
      {"event_pk": int}    — integer PK from events.id
      {"event_id": "str"}  — business key from events.event_id (preferred — stable)
    """
    _ensure_schema(db)
    event_pk = payload.get("event_pk")
    event_id = payload.get("event_id")
    if not event_pk and not event_id:
        return {"ok": False, "error": "missing event_pk or event_id"}

    ev = _load_event(
        db,
        event_pk=int(event_pk) if event_pk else None,
        event_id=str(event_id) if event_id else None,
    )
    if not ev:
        return {"ok": False, "error": f"event not found (event_pk={event_pk}, event_id={event_id})"}
    if ev.get("ai_summary"):
        return {"ok": True, "skipped": "already_summarized"}
    event_pk = ev["id"]

    headline = (ev.get("headline") or "")[:600]
    user_prompt = (
        f"Ticker: {ev.get('ticker') or 'N/A'}\n"
        f"Type: {ev.get('event_type') or 'news'}\n"
        f"Sentiment: {ev.get('sentiment') or 'neutral'}\n"
        f"Source: {ev.get('source') or 'unknown'}\n"
        f"Headline: {headline}"
    )
    summary = groq_chat(
        module="summarize",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=80,
        est_tokens=300,
        prefer_fast=True,  # 8b model is plenty for 1-line summaries
    )
    if not summary:
        # Don't error — leave ai_summary NULL and let the queue retry once
        # the governor frees up.
        return {"ok": False, "error": "groq refused / no key"}

    try:
        cur = db.conn.cursor()
        cur.execute(
            "UPDATE events SET ai_summary = ?, ai_summary_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (summary.strip().strip('"'), event_pk),
        )
        db.conn.commit()
        return {"ok": True, "event_pk": event_pk, "len": len(summary)}
    except Exception as e:
        logger.warning("ai_summarize persist %s failed: %s", event_pk, e)
        return {"ok": False, "error": str(e)}
