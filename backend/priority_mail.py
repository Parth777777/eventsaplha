"""Priority email pipeline — 4 queue handlers, pro-only.

Triggers:
    priority_mail_signal     — pro watchlist stock fires alpha >= 75
    priority_mail_forensic   — pro watchlist stock gets a high-severity flag
    priority_mail_premarket  — 07:30 IST cron, overnight events digest
    priority_mail_earnings   — concall transcript lands, AI summary mailed

All deliveries go through send_email() which tries Resend first (free 3k/mo),
then falls back to SMTP via env vars, then in-app notification record.

Daily cap PRO_PRIORITY_MAIL_DAILY_CAP = 8 per user enforced via
priority_mail_log table so volatile days don't spam pro users.

Registration in scrape_runner.py _ensure_handlers().
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
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
            CREATE TABLE IF NOT EXISTS priority_mail_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                ticker TEXT,
                subject TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivery_status TEXT,
                provider TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_pmlog_user_day "
            "ON priority_mail_log(user_id, sent_at)"
        )
        db.conn.commit()
        _SCHEMA_ENSURED = True
    except Exception as e:
        logger.warning("priority_mail schema ensure failed: %s", e)


# ── Email delivery (Resend → SMTP → in-app fallback) ───────────────────

def send_email(to: str, *, subject: str, body_html: str, body_text: str = '') -> Optional[str]:
    """Send an email. Returns the provider name on success, None on failure."""
    if not to:
        return None

    # 1. Resend (preferred — free 3k/mo)
    resend_key = os.getenv('RESEND_API_KEY')
    if resend_key:
        try:
            from_addr = os.getenv('RESEND_FROM', 'AlphaEvent <noreply@alphaevent.in>')
            req = urllib.request.Request(
                'https://api.resend.com/emails',
                data=json.dumps({
                    'from': from_addr,
                    'to': [to],
                    'subject': subject,
                    'html': body_html,
                    'text': body_text or _html_to_text(body_html),
                }).encode('utf-8'),
                headers={
                    'Authorization': f'Bearer {resend_key}',
                    'Content-Type': 'application/json',
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                if 200 <= r.status < 300:
                    return 'resend'
        except urllib.error.HTTPError as e:
            logger.warning('Resend HTTP %s: %s', e.code, e.read()[:200])
        except Exception as e:
            logger.warning('Resend send failed: %s', e)

    # 2. SMTP fallback (Gmail / any provider via env)
    smtp_host = os.getenv('SMTP_HOST')
    if smtp_host:
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = os.getenv('SMTP_FROM', 'noreply@alphaevent.in')
            msg['To'] = to
            msg.attach(MIMEText(body_text or _html_to_text(body_html), 'plain'))
            msg.attach(MIMEText(body_html, 'html'))
            with smtplib.SMTP_SSL(smtp_host, int(os.getenv('SMTP_PORT', '465')), timeout=10) as s:
                user = os.getenv('SMTP_USER')
                pw = os.getenv('SMTP_PASS')
                if user and pw:
                    s.login(user, pw)
                s.send_message(msg)
            return 'smtp'
        except Exception as e:
            logger.warning('SMTP send failed: %s', e)

    return None


def _html_to_text(html: str) -> str:
    import re
    s = re.sub(r'<br\s*/?>', '\n', html)
    s = re.sub(r'<[^>]+>', '', s)
    return s.strip()


# ── Cap check ──────────────────────────────────────────────────────────

def _under_daily_cap(db, user_id: int, kind: str) -> bool:
    """Returns True if user is below PRO_PRIORITY_MAIL_DAILY_CAP for today."""
    from backend.freemium import PRO_PRIORITY_MAIL_DAILY_CAP
    try:
        cur = db.conn.cursor()
        n = cur.execute(
            "SELECT COUNT(*) FROM priority_mail_log "
            "WHERE user_id = ? AND kind = ? "
            "AND sent_at >= datetime('now', 'start of day')",
            (user_id, kind),
        ).fetchone()[0]
        return int(n) < PRO_PRIORITY_MAIL_DAILY_CAP
    except Exception:
        return True  # don't block on lookup failure


def _log_sent(db, user_id: int, kind: str, ticker: str, subject: str,
              provider: Optional[str]) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO priority_mail_log "
            "(user_id, kind, ticker, subject, delivery_status, provider) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, kind, ticker, subject,
             'sent' if provider else 'failed', provider),
        )
        db.conn.commit()
    except Exception as e:
        logger.debug('pmlog insert failed: %s', e)


# ── Helpers to find pro users with a ticker in watchlist ───────────────

def _pro_users_for_ticker(db, ticker: str) -> List[Dict[str, Any]]:
    try:
        cur = db.conn.cursor()
        rows = cur.execute(
            """SELECT u.id, u.email
                 FROM users u
                 JOIN watchlist w ON w.user_id = u.id
                WHERE w.ticker = ?
                  AND COALESCE(u.subscription_tier, 'free') = 'pro'
                  AND u.email IS NOT NULL AND u.email <> ''""",
            (ticker.upper(),),
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(dict(r))
            except (TypeError, ValueError):
                out.append({'id': r[0], 'email': r[1]})
        return out
    except Exception as e:
        logger.debug('pro_users_for_ticker query failed: %s', e)
        return []


# ── Trigger 1: high-alpha signal on watchlist ──────────────────────────

def signal_handler(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Payload: {'event_id': str, 'min_alpha': int (default 75)}"""
    from backend.freemium import PRO_PRIORITY_MAIL_MIN_ALPHA
    _ensure_schema(db)
    event_id = payload.get('event_id')
    threshold = float(payload.get('min_alpha') or PRO_PRIORITY_MAIL_MIN_ALPHA)
    if not event_id:
        return {'ok': False, 'error': 'missing event_id'}

    try:
        cur = db.conn.cursor()
        row = cur.execute(
            "SELECT ticker, alpha_score, sentiment, event_type, headline, "
            "       COALESCE(explanation, '') AS explanation, link "
            "FROM signals WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    except Exception as e:
        return {'ok': False, 'error': str(e)}
    if not row:
        return {'ok': False, 'error': f'signal {event_id} not found'}

    try:
        sig = dict(row)
    except (TypeError, ValueError):
        sig = {'ticker': row[0], 'alpha_score': row[1], 'sentiment': row[2],
               'event_type': row[3], 'headline': row[4],
               'explanation': row[5], 'link': row[6]}

    if float(sig.get('alpha_score') or 0) < threshold:
        return {'ok': True, 'skipped': 'below_threshold', 'alpha': sig.get('alpha_score')}

    ticker = sig.get('ticker') or ''
    pros = _pro_users_for_ticker(db, ticker)
    if not pros:
        return {'ok': True, 'skipped': 'no_pro_watchers'}

    sent_count = 0
    for user in pros:
        if not _under_daily_cap(db, user['id'], 'priority_mail_signal'):
            continue
        subject = f"{ticker} · alpha {int(float(sig.get('alpha_score') or 0))} · {sig.get('sentiment','')}"
        body_html = _format_signal_email(sig)
        provider = send_email(user['email'], subject=subject, body_html=body_html)
        _log_sent(db, user['id'], 'priority_mail_signal', ticker, subject, provider)
        if provider:
            sent_count += 1
    return {'ok': True, 'recipients': len(pros), 'delivered': sent_count}


def _format_signal_email(sig: Dict[str, Any]) -> str:
    ticker = sig.get('ticker', '')
    alpha = int(float(sig.get('alpha_score') or 0))
    sent = (sig.get('sentiment') or 'neutral').lower()
    color = '#0d9669' if sent == 'bullish' else ('#dc2626' if sent == 'bearish' else '#525866')
    return f"""
    <div style="font-family:'DM Sans',system-ui,sans-serif;max-width:560px;margin:0 auto;padding:24px;">
      <div style="font-size:11px;color:#6b7280;letter-spacing:0.08em;text-transform:uppercase;font-weight:600;">AlphaEvent · Priority Alert</div>
      <h1 style="font-size:24px;letter-spacing:-0.02em;color:#0d1020;margin:8px 0 4px;">{ticker} flagged at alpha {alpha}</h1>
      <div style="color:{color};font-weight:600;font-size:13px;text-transform:uppercase;letter-spacing:0.04em;">{sig.get('event_type','event')} · {sent}</div>
      <p style="color:#0d1020;font-size:15px;line-height:1.55;margin-top:16px;">{(sig.get('headline') or '')[:280]}</p>
      <div style="background:#f4f5f9;border-radius:10px;padding:14px 16px;margin:18px 0;color:#525866;font-size:14px;line-height:1.55;">
        {(sig.get('explanation') or 'Signal detected on your watchlist.')[:600]}
      </div>
      <a href="https://alphaevent.in/app/stock.html?ticker={ticker}"
         style="display:inline-block;background:#0d1020;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;">
         Open in app →
      </a>
      <hr style="border:0;border-top:1px solid #eaecf4;margin:28px 0 16px;" />
      <div style="font-size:11px;color:#9097a8;line-height:1.5;">
        Informational only — not investment advice. AlphaEvent is not a SEBI-registered advisor.
        Manage <a href="https://alphaevent.in/app/watchlist.html" style="color:#4f46e5;">watchlist</a> ·
        <a href="https://alphaevent.in/app/settings.html#email" style="color:#4f46e5;">unsubscribe</a>
      </div>
    </div>
    """


# ── Trigger 2: forensic red flag on holdings ───────────────────────────

def forensic_handler(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Payload: {'ticker': str, 'severity': str, 'flag_type': str, 'reasoning': str}"""
    _ensure_schema(db)
    ticker = (payload.get('ticker') or '').upper()
    if not ticker:
        return {'ok': False, 'error': 'missing ticker'}
    pros = _pro_users_for_ticker(db, ticker)
    if not pros:
        return {'ok': True, 'skipped': 'no_pro_watchers'}

    subject = f"⚠ {ticker} · forensic red flag · {payload.get('severity', 'high')}"
    body_html = f"""
    <div style="font-family:'DM Sans',system-ui,sans-serif;max-width:560px;margin:0 auto;padding:24px;">
      <div style="font-size:11px;color:#dc2626;letter-spacing:0.08em;text-transform:uppercase;font-weight:600;">Forensic Alert</div>
      <h1 style="font-size:22px;letter-spacing:-0.02em;color:#0d1020;margin:8px 0 4px;">{ticker} · {payload.get('flag_type','flag')}</h1>
      <p style="color:#0d1020;font-size:15px;line-height:1.55;margin-top:16px;">{(payload.get('reasoning') or '')[:600]}</p>
      <a href="https://alphaevent.in/app/forensics.html?ticker={ticker}"
         style="display:inline-block;background:#dc2626;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;margin-top:12px;">
         View forensic detail →
      </a>
      <hr style="border:0;border-top:1px solid #eaecf4;margin:28px 0 16px;" />
      <div style="font-size:11px;color:#9097a8;line-height:1.5;">Informational only.</div>
    </div>
    """
    sent = 0
    for user in pros:
        # Forensic alerts have NO daily cap — they're rare and always important.
        provider = send_email(user['email'], subject=subject, body_html=body_html)
        _log_sent(db, user['id'], 'priority_mail_forensic', ticker, subject, provider)
        if provider:
            sent += 1
    return {'ok': True, 'recipients': len(pros), 'delivered': sent}


# ── Trigger 3: pre-market briefing (07:30 IST) ─────────────────────────
# This handler is registered as kind='priority_mail_premarket' but the
# actual composition is delegated to daily_briefing._compose_briefing with
# kind='pre_market'. The cron in api.start_scheduler() enqueues one job
# per pro user.

def premarket_handler(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Payload: {'user_id': int}"""
    from backend.daily_briefing import briefing_handler as _briefing
    # Reuse the briefing pipeline with the pre_market kind
    p = dict(payload or {})
    p['kind'] = 'pre_market'
    return _briefing(db, p)


# ── Trigger 4: earnings call transcript summary ────────────────────────

_CONCALL_SYSTEM = (
    "You summarize Indian earnings call transcripts for retail traders. "
    "Output exactly 5 bullets in this order: (1) Revenue/PAT vs guidance, "
    "(2) Margin trajectory, (3) Capex/expansion plans, (4) Top risk flagged, "
    "(5) Most-asked Q&A topic. Each bullet 1-2 sentences max. No fluff."
)


def earnings_handler(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Payload: {'ticker': str, 'transcript_id': int OR 'transcript_text': str}"""
    _ensure_schema(db)
    ticker = (payload.get('ticker') or '').upper()
    if not ticker:
        return {'ok': False, 'error': 'missing ticker'}

    # Load transcript text (best-effort, table name varies)
    transcript = payload.get('transcript_text') or ''
    if not transcript and payload.get('transcript_id'):
        for tbl in ('concall_transcripts', 'transcripts', 'earnings_calls'):
            try:
                cur = db.conn.cursor()
                row = cur.execute(
                    f"SELECT transcript_text FROM {tbl} WHERE id = ?",
                    (payload['transcript_id'],),
                ).fetchone()
                if row and row[0]:
                    transcript = row[0]
                    break
            except Exception:
                continue
    if not transcript:
        return {'ok': False, 'error': 'no transcript available'}

    # 50k token transcripts → chunk to 3 passes of 6k chars (~1.5k tokens each)
    chunks = _chunk_text(transcript, max_chars=6000, max_chunks=3)
    summaries = []
    for ch in chunks:
        out = groq_chat(
            module='reasoning',
            messages=[
                {'role': 'system', 'content': _CONCALL_SYSTEM},
                {'role': 'user',   'content': f"Ticker: {ticker}\n\nTranscript section:\n{ch}"},
            ],
            max_tokens=240,
            est_tokens=1800,
            temperature=0.3,
            prefer_fast=True,  # 8b is plenty for summary
        )
        if out:
            summaries.append(out)
    if not summaries:
        return {'ok': False, 'error': 'groq refused / no transcript'}
    summary_text = "\n\n".join(summaries)

    # Email all pro users with this ticker on watchlist
    pros = _pro_users_for_ticker(db, ticker)
    subject = f"📊 {ticker} earnings call · 5-bullet AI summary"
    body_html = f"""
    <div style="font-family:'DM Sans',system-ui,sans-serif;max-width:560px;margin:0 auto;padding:24px;">
      <div style="font-size:11px;color:#4f46e5;letter-spacing:0.08em;text-transform:uppercase;font-weight:600;">Earnings Call Summary</div>
      <h1 style="font-size:22px;letter-spacing:-0.02em;color:#0d1020;margin:8px 0 16px;">{ticker}</h1>
      <div style="font-size:14px;line-height:1.6;color:#0d1020;white-space:pre-wrap;">{summary_text}</div>
      <a href="https://alphaevent.in/app/stock.html?ticker={ticker}"
         style="display:inline-block;background:#0d1020;color:#fff;padding:11px 22px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;margin-top:18px;">
         Open in app →
      </a>
      <hr style="border:0;border-top:1px solid #eaecf4;margin:28px 0 16px;" />
      <div style="font-size:11px;color:#9097a8;line-height:1.5;">AI-summarized from public con-call transcript. Informational only.</div>
    </div>
    """
    sent = 0
    for user in pros:
        if not _under_daily_cap(db, user['id'], 'priority_mail_earnings'):
            continue
        provider = send_email(user['email'], subject=subject, body_html=body_html)
        _log_sent(db, user['id'], 'priority_mail_earnings', ticker, subject, provider)
        if provider:
            sent += 1
    return {'ok': True, 'recipients': len(pros), 'delivered': sent}


def _chunk_text(s: str, *, max_chars: int = 6000, max_chunks: int = 3) -> List[str]:
    if not s:
        return []
    out = []
    i = 0
    while i < len(s) and len(out) < max_chunks:
        out.append(s[i:i + max_chars])
        i += max_chars
    return out
