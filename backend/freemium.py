"""Freemium gate: 3-tier subscription_tier + @requires_tier decorator + limits.

Tier ladder:
    'free'    — default. Top 5 signals/day, unlimited watchlist, 10 alerts,
                10 chat/day, event summaries + signal explanations, equity
                research preview only.
    'starter' — paid ~₹99/mo. Full signal feed, 50 alerts, 50 chat/day,
                daily briefing 08:30 IST, 1 deep-dive equity research/day,
                5 saved screeners.
    'pro'     — paid ~₹299/mo. Unlimited alerts, 200 chat/day, full equity
                research (20/day), pre-market briefing 07:30 IST, priority
                email (high-alpha + forensic + earnings call summaries),
                25 saved screeners.

Razorpay integration is stubbed; the /api/billing/checkout endpoint
returns {status: 'coming_soon'} until UPI/PAN/GST onboarding is done.
"""
from __future__ import annotations

import functools
import logging
import sqlite3
from typing import Any, Dict, Optional

from flask import g, jsonify

logger = logging.getLogger(__name__)

# ── Tier hierarchy ─────────────────────────────────────────────────────
# Higher rank = more access. Used by requires_tier() for inheritance.
TIER_RANK = {
    'free': 0,
    'starter': 1,
    'pro': 2,
}

# ── Watchlist: unlimited for all tiers (decision 2026-05-18) ───────────
# Kept here as None to signal "no cap". check_watchlist_limit always returns
# None now but the function stays in place for backward compat.
FREE_WATCHLIST_MAX = None
STARTER_WATCHLIST_MAX = None
PRO_WATCHLIST_MAX = None

# ── Alerts ─────────────────────────────────────────────────────────────
FREE_ALERTS_MAX = 10
STARTER_ALERTS_MAX = 50
PRO_ALERTS_MAX = None   # unlimited

# ── AI chat (Q&A) daily caps ───────────────────────────────────────────
FREE_CHAT_PER_DAY = 10
STARTER_CHAT_PER_DAY = 50
PRO_CHAT_PER_DAY = 200

# ── Signal feed teaser ─────────────────────────────────────────────────
# Free + anonymous users see the top N signals of the day (alpha-ordered).
# The rest are reported as locked_count so the UI can render an upgrade
# tile. Starter and Pro see the full feed.
FREE_SIGNALS_VISIBLE = 5
ANON_SIGNALS_VISIBLE = 5

# ── Equity research reports (legacy — UI unhooked 2026-05-18) ──────────
# Module kept compiled in case we re-enable the dedicated research view
# later, but pricing + sidebar no longer surface it. Fundamental analysis
# is the new headline product instead.
FREE_RESEARCH_PER_DAY = 0
STARTER_RESEARCH_PER_DAY = 1
PRO_RESEARCH_PER_DAY = 20

# ── Fundamental analysis (the headline product) ────────────────────────
# Count-based gating, not feature-gating. Free sees the full breakdown
# (positives, red flags, components, insider buys, earnings whisper,
# bulk-deal cross-ref, AI explainer) but only N unique tickers/day.
# Same-day same-ticker re-views do NOT count — encouraging deep work on
# a few names rather than spammy browsing.
FREE_ANALYSIS_PER_DAY = 3
STARTER_ANALYSIS_PER_DAY = 30
PRO_ANALYSIS_PER_DAY = None        # unlimited

# ── Priority mail (pro only) ───────────────────────────────────────────
# When a pro user's watchlist stock fires a signal with alpha_score >=
# this threshold, send an instant email. Daily cap prevents spam on
# volatile days.
PRO_PRIORITY_MAIL_MIN_ALPHA = 75
PRO_PRIORITY_MAIL_DAILY_CAP = 8

# ── Saved custom screeners ─────────────────────────────────────────────
FREE_SAVED_SCREENERS_MAX = 0       # 402 on every save
STARTER_SAVED_SCREENERS_MAX = 5
PRO_SAVED_SCREENERS_MAX = 25


_SCHEMA_ENSURED = False


def ensure_schema(db) -> None:
    """Add subscription_tier + tier_expires_at columns to users (idempotent)."""
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return
    try:
        cur = db.conn.cursor()
        for stmt in (
            "ALTER TABLE users ADD COLUMN subscription_tier TEXT DEFAULT 'free'",
            "ALTER TABLE users ADD COLUMN tier_expires_at TIMESTAMP",
        ):
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        db.conn.commit()
        _SCHEMA_ENSURED = True
    except Exception as e:
        logger.warning("freemium schema ensure failed: %s", e)


def get_user_tier(db, user_id: Any) -> str:
    """Resolve a user's tier. Returns 'free' by default and on lookup failure.

    Honors tier_expires_at — if the expiry is past, the user is treated as
    'free' regardless of the stored tier. This protects against the case
    where Razorpay webhook missed the cancellation event.
    """
    if not user_id or user_id == 'legacy':
        return 'free'
    ensure_schema(db)
    try:
        cur = db.conn.cursor()
        row = cur.execute(
            "SELECT subscription_tier, tier_expires_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return 'free'
        tier = (row[0] or 'free').lower()
        if tier not in TIER_RANK:
            tier = 'free'
        # Check expiry — if past, treat as free even if tier is paid
        if tier != 'free' and row[1]:
            try:
                from datetime import datetime
                exp_str = str(row[1]).replace('Z', '').rstrip('+00:00').strip()
                exp = datetime.fromisoformat(exp_str)
                if exp < datetime.utcnow():
                    return 'free'
            except Exception:
                pass
        return tier
    except Exception:
        return 'free'


def tier_rank(tier: str) -> int:
    return TIER_RANK.get((tier or 'free').lower(), 0)


def requires_tier(min_tier: str):
    """Flask decorator: refuse requests from users below min_tier with 402.

    Honors the hierarchy: @requires_tier('starter') allows both 'starter'
    and 'pro'. @requires_tier('pro') is exclusive to pro.

    Must be used AFTER @require_auth (needs g.user_id set).
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from backend.api import get_db  # late import to avoid cycle
            user_id = getattr(g, 'user_id', None)
            tier = get_user_tier(get_db(), user_id)
            if tier_rank(tier) >= tier_rank(min_tier):
                return fn(*args, **kwargs)
            return jsonify({
                'success': False,
                'error': 'upgrade_required',
                'feature': fn.__name__,
                'current_tier': tier,
                'required_tier': min_tier,
                'upgrade_url': '/app/pricing.html',
            }), 402
        return wrapper
    return decorator


# ── Per-feature limit checks ───────────────────────────────────────────

def check_watchlist_limit(db, user_id: Any) -> Optional[Dict[str, Any]]:
    """Watchlist is unlimited for all tiers as of 2026-05-18. Kept as a
    no-op for backward compat with callers that still invoke it."""
    return None


def _alerts_cap_for(tier: str) -> Optional[int]:
    if tier == 'pro':     return PRO_ALERTS_MAX
    if tier == 'starter': return STARTER_ALERTS_MAX
    return FREE_ALERTS_MAX


def check_alerts_limit(db, user_id: Any) -> Optional[Dict[str, Any]]:
    """Return a 402-style error dict if the user is at their tier's alert
    cap. None = allowed to add. Pro tier is uncapped."""
    tier = get_user_tier(db, user_id)
    cap = _alerts_cap_for(tier)
    if cap is None:
        return None
    try:
        cur = db.conn.cursor()
        n = cur.execute(
            "SELECT COUNT(*) FROM alerts WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        if int(n) >= cap:
            return {
                'success': False,
                'error': 'tier_limit',
                'feature': 'alerts',
                'tier': tier,
                'limit': cap,
                'current': int(n),
                'upgrade_url': '/app/pricing.html',
            }
    except Exception:
        pass
    return None


def chat_daily_cap(tier: str) -> int:
    if tier == 'pro':     return PRO_CHAT_PER_DAY
    if tier == 'starter': return STARTER_CHAT_PER_DAY
    return FREE_CHAT_PER_DAY


def signal_feed_cap(tier: str) -> Optional[int]:
    """Return None for unlimited (starter+pro), else the visible row cap."""
    if tier in ('starter', 'pro'):
        return None
    return FREE_SIGNALS_VISIBLE


def research_daily_cap(tier: str) -> int:
    if tier == 'pro':     return PRO_RESEARCH_PER_DAY
    if tier == 'starter': return STARTER_RESEARCH_PER_DAY
    return FREE_RESEARCH_PER_DAY


def saved_screeners_cap(tier: str) -> int:
    if tier == 'pro':     return PRO_SAVED_SCREENERS_MAX
    if tier == 'starter': return STARTER_SAVED_SCREENERS_MAX
    return FREE_SAVED_SCREENERS_MAX


def analysis_daily_cap(tier: str):
    """Returns the daily cap (int) or None for unlimited."""
    if tier == 'pro':     return PRO_ANALYSIS_PER_DAY
    if tier == 'starter': return STARTER_ANALYSIS_PER_DAY
    return FREE_ANALYSIS_PER_DAY


_ANALYSIS_SCHEMA_ENSURED = False


def _ensure_analysis_schema(db) -> None:
    global _ANALYSIS_SCHEMA_ENSURED
    if _ANALYSIS_SCHEMA_ENSURED:
        return
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_analysis_usage_user_day "
            "ON analysis_usage(user_id, used_at)"
        )
        db.conn.commit()
        _ANALYSIS_SCHEMA_ENSURED = True
    except Exception as e:
        logger.warning("analysis_usage schema ensure failed: %s", e)


def analysis_count_today(db, user_id: str) -> int:
    """Return DISTINCT tickers analyzed today by this user. Same-day
    same-ticker re-views don't count."""
    if not user_id:
        return 0
    _ensure_analysis_schema(db)
    try:
        cur = db.conn.cursor()
        n = cur.execute(
            "SELECT COUNT(DISTINCT ticker) FROM analysis_usage "
            "WHERE user_id = ? AND used_at >= datetime('now', 'start of day')",
            (user_id,),
        ).fetchone()[0]
        return int(n or 0)
    except Exception:
        return 0


def record_analysis(db, user_id: str, ticker: str) -> None:
    if not user_id or not ticker:
        return
    _ensure_analysis_schema(db)
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO analysis_usage (user_id, ticker) VALUES (?, ?)",
            (user_id, (ticker or '').upper()),
        )
        db.conn.commit()
    except Exception:
        pass


def check_analysis_quota(db) -> Optional[Dict[str, Any]]:
    """Returns 429-style dict if the request's user is at their daily cap.
    None = allowed. Anon users share a single bucket per IP — call from
    a route that has request-context access.

    Designed for: at the START of /api/fundamentals/score/<ticker>:
        err = check_analysis_quota(get_db())
        if err: return jsonify(err), 429
        # ... full analysis ...
        from backend.freemium import record_analysis
        record_analysis(get_db(), <user>, ticker)

    Anonymous users get the FREE cap, keyed by IP hash so they can't just
    refresh forever.
    """
    user_id = _resolve_user_id()
    tier = get_user_tier(db, user_id) if user_id else 'free'
    cap = analysis_daily_cap(tier)
    if cap is None:
        return None  # pro = unlimited

    # Build the per-user bucket key (auth'd) or per-IP bucket key (anon)
    bucket = user_id
    if not bucket:
        try:
            from flask import request
            import hashlib, os
            ip = request.headers.get('CF-Connecting-IP') or request.headers.get('X-Real-IP') or request.remote_addr or '0.0.0.0'
            salt = os.getenv('SECRET_KEY', 'dev')
            bucket = 'anon:' + hashlib.sha256((ip + salt).encode()).hexdigest()[:24]
        except Exception:
            bucket = 'anon:unknown'

    used = analysis_count_today(db, bucket)
    if used >= cap:
        return {
            'success': False,
            'error': 'analysis_quota_exceeded',
            'tier': tier,
            'used_today': used,
            'limit': cap,
            'message': (f"You've used {used}/{cap} analyses today. "
                        f"Upgrade for {STARTER_ANALYSIS_PER_DAY}/day on Starter "
                        f"or unlimited on Pro."),
            'upgrade_url': '/app/pricing.html',
        }
    return None


def current_analysis_bucket(db) -> str:
    """Return the bucket key (user_id or anon:hash) that check_analysis_quota
    would resolve. Used by record_analysis at the end of a successful call."""
    user_id = _resolve_user_id()
    if user_id:
        return user_id
    try:
        from flask import request
        import hashlib, os
        ip = request.headers.get('CF-Connecting-IP') or request.headers.get('X-Real-IP') or request.remote_addr or '0.0.0.0'
        salt = os.getenv('SECRET_KEY', 'dev')
        return 'anon:' + hashlib.sha256((ip + salt).encode()).hexdigest()[:24]
    except Exception:
        return 'anon:unknown'


def check_saved_screeners_limit(db, user_id: Any) -> Optional[Dict[str, Any]]:
    tier = get_user_tier(db, user_id)
    cap = saved_screeners_cap(tier)
    if cap == 0:
        return {
            'success': False,
            'error': 'upgrade_required',
            'feature': 'saved_screeners',
            'tier': tier,
            'required_tier': 'starter',
            'upgrade_url': '/app/pricing.html',
        }
    try:
        cur = db.conn.cursor()
        n = cur.execute(
            "SELECT COUNT(*) FROM saved_screeners WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        if int(n) >= cap:
            return {
                'success': False,
                'error': 'tier_limit',
                'feature': 'saved_screeners',
                'tier': tier,
                'limit': cap,
                'current': int(n),
                'upgrade_url': '/app/pricing.html',
            }
    except Exception:
        pass
    return None


def _resolve_user_id() -> Optional[str]:
    """Resolve the current request's user_id. Tries flask.g (set by
    @optional_auth / @require_auth in api.py) first; falls back to
    decoding the Authorization: Bearer <jwt> header inline so blueprint
    routes that don't use the decorator still get tier-aware treatment.
    """
    try:
        from flask import g
        uid = getattr(g, 'user_id', None)
        if uid and uid != 'legacy':
            return str(uid)
    except Exception:
        pass
    # Inline JWT decode fallback
    try:
        import os
        import jwt as pyjwt  # type: ignore
        from flask import request, current_app
        auth = request.headers.get('Authorization', '') or ''
        if not auth.startswith('Bearer '):
            return None
        token = auth[7:].strip()
        if not token:
            return None
        # Prefer the Flask app's SECRET_KEY if available; else read env.
        secret = None
        try:
            secret = current_app.config.get('SECRET_KEY')
        except Exception:
            pass
        secret = secret or os.getenv('SECRET_KEY') or 'dev-secret'
        # Try Supabase first (audience='authenticated'); fall back to local
        sb_secret = os.getenv('SUPABASE_JWT_SECRET')
        if sb_secret:
            try:
                payload = pyjwt.decode(token, sb_secret, algorithms=['HS256'],
                                       audience='authenticated')
                sub = payload.get('sub')
                if sub:
                    return str(sub)
            except Exception:
                pass
        try:
            payload = pyjwt.decode(token, secret, algorithms=['HS256'])
            uid = payload.get('user_id') or payload.get('sub')
            if uid:
                return str(uid)
        except Exception:
            return None
    except Exception:
        pass
    return None


def gate_paid(db, *, feature: str = 'fundamental_analysis') -> Optional[Dict[str, Any]]:
    """Return a 402-style dict if the request's user is below Starter,
    else None. Designed to be called as the first line of a Flask route:

        err = gate_paid(get_db())
        if err: return jsonify(err), 402

    Works on routes WITH and WITHOUT @optional_auth — resolves user_id from
    flask.g first, then falls back to inline JWT decode.
    """
    user_id = _resolve_user_id()
    tier = get_user_tier(db, user_id) if user_id else 'free'
    if tier in ('starter', 'pro'):
        return None
    msgs = {
        'fundamental_analysis': 'Deep fundamental analysis is a paid feature. Starter from ₹99/mo.',
        'edge_intel':            'Edge intelligence (insider buys, earnings whisper, bulk deals) is paid. Starter from ₹99/mo.',
    }
    return {
        'success': False,
        'error': 'upgrade_required',
        'feature': feature,
        'current_tier': tier,
        'required_tier': 'starter',
        'message': msgs.get(feature, 'This feature requires Starter or Pro.'),
        'upgrade_url': '/app/pricing.html',
    }


def is_paid_request(db) -> bool:
    """Quick boolean check used by routes that return a partial preview
    for free and the full payload for paid. Resolves the same way as
    gate_paid (g.user_id first, then inline JWT decode)."""
    user_id = _resolve_user_id()
    if not user_id:
        return False
    return get_user_tier(db, user_id) in ('starter', 'pro')


def get_user_features(db, user_id: Any) -> Dict[str, Any]:
    """Single-call snapshot for the frontend. Used by /api/me/tier."""
    tier = get_user_tier(db, user_id) if user_id and user_id != 'legacy' else (
        'free' if user_id == 'legacy' else 'anon'
    )
    is_pro = tier == 'pro'
    is_starter = tier == 'starter'
    is_paid = is_pro or is_starter
    return {
        'tier': tier,
        'is_pro': is_pro,
        'is_starter': is_starter,
        'is_paid': is_paid,
        'limits': {
            'watchlist':       None,  # unlimited everywhere
            'alerts':          _alerts_cap_for(tier if tier != 'anon' else 'free'),
            'chat_per_day':    chat_daily_cap(tier if tier != 'anon' else 'free'),
            'signal_feed':     signal_feed_cap(tier if tier != 'anon' else 'free'),
            'saved_screeners': saved_screeners_cap(tier if tier != 'anon' else 'free'),
            # Fundamental analysis is the headline product — count-based gate.
            # None = unlimited. Returned to UI so it can render "3/5 today".
            'analysis_per_day': analysis_daily_cap(tier if tier != 'anon' else 'free'),
        },
        'features': {
            # Tier-gated features
            'full_signal_feed':       is_paid,
            'daily_briefing':         is_paid,
            'pre_market_briefing':    is_pro,
            'priority_mail_signal':   is_pro,
            'priority_mail_forensic': is_pro,
            'earnings_call_summary':  is_pro,
            'forensic_full':          is_paid,
            'saved_screeners':        is_paid,
            # Available to everyone with a daily-count cap (the new model)
            'fundamental_analysis':   True,
            'ai_explainer':           True,
            'peer_rank':              True,
            # Available to everyone (everyone-level feature flags)
            'event_summaries':        True,
            'signal_explanations':    True,
            'forensic_summary':       True,
            'chat_qa':                tier != 'anon',
        },
        'upgrade_url': '/app/pricing.html' if not is_pro else None,
    }
