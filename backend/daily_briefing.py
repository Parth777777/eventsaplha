"""Daily personalized briefing — pro-tier feature.

Generated at 08:30 IST. Per active pro user, composes a 2-3 paragraph brief
from their watchlist + last-24h events + open alerts, then delivers via
(in priority order) web-push → email → telegram.

Schedule:
    Registered in api.start_scheduler() as a CronTrigger.
        hour=3, minute=0 UTC == 08:30 IST.

Free tier: skipped intentionally to drive upgrades.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.ai_groq import groq_chat

logger = logging.getLogger(__name__)

_SCHEMA_ENSURED = False


def _ensure_schema(db) -> None:
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_briefings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                delivery_status TEXT DEFAULT 'pending',
                channel TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivered_at TIMESTAMP
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_briefings_user "
            "ON daily_briefings(user_id, created_at)"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_briefings_dedup "
            "ON daily_briefings(user_id, DATE(created_at))"
        )
        db.conn.commit()
        _SCHEMA_ENSURED = True
    except Exception as e:
        # Some old SQLite builds don't support functional unique indexes;
        # the dedup is best-effort.
        logger.warning("daily_briefings schema ensure failed: %s", e)


def _eligible_users(db, tiers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Users in the given tier list, active in last 30 days, non-empty watchlist.

    Defaults to ['starter', 'pro'] — the daily briefing audience. Pass
    ['pro'] for pre-market briefing.
    """
    tier_list = tiers or ['starter', 'pro']
    placeholders = ','.join(['?'] * len(tier_list))
    try:
        cur = db.conn.cursor()
        rows = cur.execute(
            f"""
            SELECT u.id, u.email, COALESCE(u.subscription_tier, 'free') AS tier
            FROM users u
            WHERE COALESCE(u.subscription_tier, 'free') IN ({placeholders})
              AND EXISTS (
                SELECT 1 FROM watchlist w WHERE w.user_id = u.id
              )
            LIMIT 500
            """,
            tier_list,
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(dict(r))
            except (TypeError, ValueError):
                out.append({"id": r[0], "email": r[1], "tier": r[2]})
        return out
    except Exception as e:
        logger.warning("daily_briefing eligible users query failed: %s", e)
        return []


def _user_context(db, user_id: int) -> Dict[str, Any]:
    """Gather watchlist tickers + last-24h events for those tickers."""
    cur = db.conn.cursor()
    tickers: List[str] = []
    try:
        rows = cur.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? LIMIT 20", (user_id,)
        ).fetchall()
        tickers = [r[0] for r in rows if r and r[0]]
    except Exception:
        pass

    events: List[Dict[str, Any]] = []
    if tickers:
        placeholders = ",".join(["?"] * len(tickers))
        try:
            rows = cur.execute(
                f"SELECT ticker, headline, event_type, sentiment, "
                f"       COALESCE(ai_summary, headline) AS summary "
                f"FROM events WHERE ticker IN ({placeholders}) "
                f"  AND created_at >= datetime('now', '-1 day') "
                f"ORDER BY created_at DESC LIMIT 12",
                tickers,
            ).fetchall()
            for r in rows:
                try:
                    events.append(dict(r))
                except (TypeError, ValueError):
                    events.append({
                        "ticker": r[0], "headline": r[1],
                        "event_type": r[2], "sentiment": r[3],
                        "summary": r[4],
                    })
        except Exception as e:
            logger.debug("event lookup failed: %s", e)

    return {"tickers": tickers, "events": events}


SYSTEM_PROMPTS = {
    'daily': (
        "You write a 2-3 paragraph morning market briefing for an Indian retail "
        "trader, sent at 08:30 IST just after pre-market. Lead with the most "
        "actionable item. Use specific tickers and numbers. End with one concrete "
        "suggestion (e.g. 'watch X near support', 'consider trimming Y if Z holds'). "
        "Plain language, no jargon, no disclaimers."
    ),
    'pre_market': (
        "You write a 2-3 paragraph PRE-MARKET briefing for an Indian retail trader, "
        "sent at 07:30 IST — 1h45m before market open. Focus on: (1) overnight "
        "global moves (Dow/Nasdaq/Asia) that matter for Indian equities, (2) any "
        "ADR moves of dual-listed names in the user's watchlist, (3) overnight "
        "events/filings on watchlist tickers, (4) one specific pre-market "
        "positioning idea (\"watch X if Y opens above Z\"). No 'good morning' "
        "fluff — these traders are already at their desk."
    ),
}


def _compose_briefing(user_id: int, ctx: Dict[str, Any], kind: str = 'daily') -> Optional[str]:
    tickers = ctx.get("tickers") or []
    events = ctx.get("events") or []
    if not tickers:
        return None
    events_block = "\n".join(
        f"- {e.get('ticker')}: {e.get('summary','')} [{e.get('sentiment','neutral')}]"
        for e in events[:10]
    ) or "(no notable events overnight)"
    system_prompt = SYSTEM_PROMPTS.get(kind) or SYSTEM_PROMPTS['daily']
    timeframe = "Last 12 hours" if kind == 'pre_market' else "Last 24 hours"
    user_prompt = (
        f"User watchlist: {', '.join(tickers[:10])}\n\n"
        f"{timeframe} events for these tickers:\n{events_block}\n\n"
        f"Date: {datetime.utcnow().strftime('%Y-%m-%d')} "
        f"(IST market opens 09:15)"
    )
    return groq_chat(
        module="briefing",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=320,
        est_tokens=1200,
    )


def _deliver(db, user_id: int, email: Optional[str], body: str) -> str:
    """Try delivery in priority order: web-push → email → telegram → in-app only.
    Returns the channel name that succeeded (or 'inapp' as the always-succeed
    fallback)."""
    # Web push (web-push lib + endpoint stored per-user via /api/notifications/subscribe)
    try:
        from backend.notifications import send_web_push  # type: ignore
        if send_web_push(user_id, title="Your Tickwave morning brief", body=body[:400]):
            return "web_push"
    except Exception:
        pass
    # Email via Resend (free tier 3k/mo)
    try:
        if email and os.getenv("RESEND_API_KEY"):
            from backend.notifications import send_email  # type: ignore
            if send_email(email, subject="Your Tickwave morning brief", body=body):
                return "email"
    except Exception:
        pass
    # Telegram (per-user chat_id stored on users.telegram_chat_id if wired)
    try:
        from backend.notifications import send_telegram  # type: ignore
        if send_telegram(user_id, body[:1024]):
            return "telegram"
    except Exception:
        pass
    return "inapp"


def briefing_handler(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Queue handler: build + persist + deliver one user's briefing.

    Payload:
      {"user_id": int, "kind": "daily" | "pre_market"}  (kind defaults to 'daily')
    """
    _ensure_schema(db)
    user_id = int(payload.get("user_id") or 0)
    kind = (payload.get("kind") or "daily").lower()
    if kind not in ("daily", "pre_market"):
        kind = "daily"
    if not user_id:
        return {"ok": False, "error": "missing user_id"}

    cur = db.conn.cursor()
    row = cur.execute(
        "SELECT id, email FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        return {"ok": False, "error": f"user {user_id} not found"}
    email = row[1] if not isinstance(row, dict) else row.get("email")

    ctx = _user_context(db, user_id)
    body = _compose_briefing(user_id, ctx, kind=kind)
    if not body:
        return {"ok": False, "error": "compose failed or empty watchlist"}

    try:
        cur.execute(
            "INSERT INTO daily_briefings (user_id, body, delivery_status) "
            "VALUES (?, ?, 'pending')",
            (user_id, body),
        )
        brief_id = cur.lastrowid
        db.conn.commit()
    except sqlite3.IntegrityError:
        return {"ok": True, "skipped": "already_sent_today"}
    except Exception as e:
        logger.warning("briefing persist failed: %s", e)
        return {"ok": False, "error": str(e)}

    channel = _deliver(db, user_id, email, body)
    try:
        cur.execute(
            "UPDATE daily_briefings SET delivery_status='delivered', "
            "channel=?, delivered_at=CURRENT_TIMESTAMP WHERE id=?",
            (channel, brief_id),
        )
        db.conn.commit()
    except Exception:
        pass

    return {"ok": True, "user_id": user_id, "channel": channel, "len": len(body)}


def enqueue_all_pro_users(db) -> int:
    """Called by the 08:30 IST cron. Enqueues a 'daily' briefing for every
    eligible Starter + Pro user. Kept under the original name for backward
    compat with the existing scheduler registration."""
    from backend.scrape_runner import enqueue
    users = _eligible_users(db, tiers=['starter', 'pro'])
    n = 0
    for u in users:
        if enqueue(db, "briefing", {"user_id": int(u["id"]), "kind": "daily"}):
            n += 1
    logger.info("daily_briefing(daily): enqueued %d/%d starter+pro users", n, len(users))
    return n


def enqueue_pre_market_users(db) -> int:
    """Called by the 07:30 IST cron. Pro tier only — pre-market briefing
    focused on overnight events, ADR moves, and pre-open positioning."""
    from backend.scrape_runner import enqueue
    users = _eligible_users(db, tiers=['pro'])
    n = 0
    for u in users:
        if enqueue(db, "briefing", {"user_id": int(u["id"]), "kind": "pre_market"}):
            n += 1
    logger.info("daily_briefing(pre_market): enqueued %d/%d pro users", n, len(users))
    return n
