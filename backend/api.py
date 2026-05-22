#!/usr/bin/env python3
"""
Tickwave Backend API
Flask server that serves the frontend, REST API, and runs the scraper on a schedule.
Supports PostgreSQL (Supabase) and SQLite (local dev).
"""

import os
import sys
import logging
import functools
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from flask import Flask, request, jsonify, send_from_directory, g
from flask_cors import CORS
import jwt as pyjwt

# Add scraper to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scraper'))

from database_schema import TickwaveDB
from notifications import process_signal_notifications, send_test_notification

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============ FLASK APP ============

# Phase D — Sentry (error tracking + performance). No-op when SENTRY_DSN
# env var is absent, so dev/CI envs don't need anything new. Must initialise
# BEFORE Flask() so the SDK can patch the framework on import.
try:
    _sentry_dsn = os.getenv('SENTRY_DSN', '').strip()
    if _sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0.05')),
            profiles_sample_rate=float(os.getenv('SENTRY_PROFILES_SAMPLE_RATE', '0.0')),
            environment=os.getenv('SENTRY_ENVIRONMENT', 'production'),
            release=os.getenv('SENTRY_RELEASE') or None,
            # Strip query strings + scrub PII automatically
            send_default_pii=False,
        )
        logger.info("Sentry initialised (env=%s)", os.getenv('SENTRY_ENVIRONMENT', 'production'))
except Exception as _sentry_err:
    # Never let observability tooling break the app boot
    logger.warning("Sentry init skipped: %s", _sentry_err)

# Serve frontend static files from ../app
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'app')

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='')
CORS(app)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')

# Phase D — Flask-Limiter for per-IP rate limiting. Storage backend is
# in-memory by default; if a REDIS_URL is set it auto-uses Redis (shared
# across gunicorn workers). Endpoints opt in via @limiter.limit("N/period").
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _redis_url = os.getenv('REDIS_URL', '').strip()
    limiter = Limiter(
        get_remote_address,
        app=app,
        storage_uri=(_redis_url or 'memory://'),
        # Conservative defaults across the API — endpoints can override.
        default_limits=['1000 per hour'],
        headers_enabled=True,  # send X-RateLimit-* response headers
    )
    logger.info(
        "Flask-Limiter initialised (storage=%s)",
        'redis' if _redis_url else 'memory',
    )
except Exception as _limiter_err:
    # Make limiter a no-op shim so @limiter.limit decorators still parse
    logger.warning("Flask-Limiter init skipped: %s", _limiter_err)
    class _NoopLimiter:
        def limit(self, *a, **k):
            def deco(fn): return fn
            return deco
        def exempt(self, fn): return fn
    limiter = _NoopLimiter()

# ── NaN-safe JSON ─────────────────────────────────────────────────────────
# Python's default json.dumps emits literal NaN / Infinity which the browser
# rejects ("Unexpected token 'N'…is not valid JSON"). We walk the response
# tree and replace NaN/Infinity with None before serialization, so /api/stock
# and friends produce strict-JSON-compliant responses.
import math
def _strict_json_clean(obj):
    if isinstance(obj, float):
        if obj != obj or obj == math.inf or obj == -math.inf:
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _strict_json_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strict_json_clean(v) for v in obj]
    return obj
try:
    from flask.json.provider import DefaultJSONProvider
    class _StrictJSONProvider(DefaultJSONProvider):
        def dumps(self, obj, **kwargs):
            return super().dumps(_strict_json_clean(obj), **kwargs)
    app.json = _StrictJSONProvider(app)
except Exception:
    # Older Flask (<2.2) — fall back to overriding json_encoder.
    import json as _json
    class _StrictJSONEncoder(_json.JSONEncoder):
        def iterencode(self, o, _one_shot=False):
            return super().iterencode(_strict_json_clean(o), _one_shot)
    app.json_encoder = _StrictJSONEncoder

# ============ MARKET CLOCK ============
# Single source of truth for "is the market live?" — used by the in-app banner
# and by any data-staleness UI ("last close · 15:30 IST" vs "live · streaming").
from market_clock import clock_payload as _market_clock_payload, is_market_open as _is_market_open

@app.route('/api/market/clock', methods=['GET'])
def market_clock():
    return jsonify({'success': True, 'data': _market_clock_payload()})

# ============ NOTIFICATION RETRY (admin visibility) ============

@app.route('/api/admin/notifications/queue', methods=['GET'])
def admin_notif_queue():
    try:
        from notification_retry import queue_status
        return jsonify({'success': True, 'data': queue_status()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/notifications/drain', methods=['POST'])
def admin_notif_drain():
    """Manually drain pending retries — handy for ops."""
    try:
        from notification_retry import process_pending
        stats = process_pending(limit=50)
        return jsonify({'success': True, 'data': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ F&O OPTION CHAIN ============
# Three endpoints for the F&O page — all read-only and cached briefly.

_OC_VALID = {'NIFTY', 'BANKNIFTY', 'FINNIFTY'}

@app.route('/api/fo/option-chain/<symbol>', methods=['GET'])
def fo_option_chain_summary(symbol):
    sym = (symbol or '').upper().strip()
    if sym not in _OC_VALID:
        return jsonify({'success': False, 'error': 'unsupported symbol'}), 400
    try:
        from nse_option_chain import latest_summary
        row = latest_summary(sym)
        if not row:
            return jsonify({'success': True, 'data': None, 'note': 'no snapshot yet'})
        return jsonify({'success': True, 'data': row})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/fo/option-chain/<symbol>/oi-shifts', methods=['GET'])
def fo_option_chain_shifts(symbol):
    sym = (symbol or '').upper().strip()
    if sym not in _OC_VALID:
        return jsonify({'success': False, 'error': 'unsupported symbol'}), 400
    try:
        from nse_option_chain import recent_oi_shifts
        top = max(3, min(30, int(request.args.get('top', '10'))))
        return jsonify({'success': True, 'data': recent_oi_shifts(sym, top=top)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/fo/option-chain/refresh', methods=['POST'])
def fo_option_chain_refresh():
    """Manual ingest trigger — useful when market just opened or for ops."""
    try:
        from nse_option_chain import ingest_all_symbols
        return jsonify({'success': True, 'data': ingest_all_symbols()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ DATA FRESHNESS MONITOR ============
# One-stop "is every data source healthy?" endpoint. Probes the freshest row
# from each table the trader actually depends on and reports staleness.

def _freshness_row(name: str, sql: str, ok_max_min: int, warn_max_min: int):
    """Run a SELECT MAX(<ts_col>) and bucket its age into status."""
    try:
        db = get_db()
        cur = db.conn.cursor()
        cur.execute(sql)
        r = cur.fetchone()
        ts = r[0] if r else None
    except Exception as e:
        return {'name': name, 'status': 'error', 'error': str(e)[:120],
                'last_row_at': None, 'age_min': None}
    if not ts:
        return {'name': name, 'status': 'empty', 'last_row_at': None, 'age_min': None}
    try:
        # SQLite returns string, Postgres returns datetime
        if isinstance(ts, str):
            ts_dt = datetime.fromisoformat(ts.replace('Z', '+00:00').split('+')[0])
        else:
            ts_dt = ts
        age = (datetime.utcnow() - ts_dt.replace(tzinfo=None)).total_seconds() / 60.0
    except Exception:
        return {'name': name, 'status': 'unknown', 'last_row_at': str(ts), 'age_min': None}
    status = 'ok' if age <= ok_max_min else 'warn' if age <= warn_max_min else 'stale'
    return {'name': name, 'status': status,
            'last_row_at': str(ts), 'age_min': round(age, 1),
            'ok_threshold_min': ok_max_min, 'warn_threshold_min': warn_max_min}


@app.route('/api/admin/data-status', methods=['GET'])
def admin_data_status():
    """Returns a freshness probe per data source."""
    probes = [
        _freshness_row('events',     "SELECT MAX(created_at) FROM events",                       60,   240),
        _freshness_row('signals',    "SELECT MAX(created_at) FROM signals",                      60,   240),
        _freshness_row('predictions', "SELECT MAX(created_at) FROM predictions",                240,  1440),
        _freshness_row('articles',   "SELECT MAX(first_seen_at) FROM scraped_articles",          30,   180),
        _freshness_row('notifications', "SELECT MAX(sent_at) FROM notification_log",            1440, 10080),
    ]
    # Also probe non-main-DB sources (separate sqlite files)
    fo_probe = {'name': 'option_chain', 'status': 'empty', 'last_row_at': None, 'age_min': None}
    try:
        import sqlite3 as _s
        oc_db = os.path.join(os.path.dirname(__file__), '..', 'fo.db')
        if os.path.exists(oc_db):
            c = _s.connect(oc_db, timeout=5); c.row_factory = _s.Row
            row = c.execute("SELECT MAX(fetched_at) AS m FROM oc_snapshot").fetchone()
            c.close()
            if row and row['m']:
                ts_dt = datetime.fromisoformat(str(row['m']).split('+')[0])
                age = (datetime.utcnow() - ts_dt).total_seconds() / 60.0
                fo_probe.update({'last_row_at': row['m'], 'age_min': round(age, 1),
                                 'status': 'ok' if age <= 10 else 'warn' if age <= 60 else 'stale',
                                 'ok_threshold_min': 10, 'warn_threshold_min': 60})
    except Exception as e:
        fo_probe['status'] = 'error'; fo_probe['error'] = str(e)[:120]
    probes.append(fo_probe)
    waitlist_probe = {'name': 'waitlist', 'status': 'empty', 'last_row_at': None, 'age_min': None}
    try:
        with _WL_LOCK:
            c = _wl_conn()
            row = c.execute("SELECT MAX(joined_at) AS m, COUNT(*) AS n FROM waitlist").fetchone()
            c.close()
        if row and row['m']:
            waitlist_probe.update({'last_row_at': row['m'], 'n': row['n'], 'status': 'ok'})
    except Exception as e:
        waitlist_probe['status'] = 'error'; waitlist_probe['error'] = str(e)[:120]
    probes.append(waitlist_probe)

    overall = 'ok'
    for p in probes:
        if p['status'] in ('error', 'stale'):
            overall = 'stale'; break
        if p['status'] in ('warn', 'unknown'):
            overall = 'warn'
    return jsonify({'success': True, 'overall': overall,
                    'data': probes, 'served_at': datetime.utcnow().isoformat() + 'Z'})


# ============ SCRAPE RUNNER ADMIN ENDPOINTS ============
# Wraps backend/scrape_runner.py. Lets ops trigger backfills + see job_runs
# log via API instead of grepping server logs. Both require ADMIN_TOKEN.

@app.route('/api/admin/jobs', methods=['GET'])
def admin_jobs_status():
    """Return last-run status per registered job + recent run history.

    Query params:
      limit: how many history rows to include (default 50, max 500)
      job:   filter to one job_name
      status: filter to 'ok' | 'partial' | 'error'
    """
    # require_admin is defined later in the file; bind lazily
    auth = request.headers.get('X-Admin-Token') or request.headers.get('X-Admin-Secret') or ''
    if not _ADMIN_TOKEN or _ADMIN_TOKEN in _ADMIN_TOKEN_INSECURE_DEFAULTS:
        return jsonify({'success': False, 'error': 'admin endpoints disabled (set ADMIN_TOKEN)'}), 503
    import hmac as _hmac
    if not _hmac.compare_digest(auth, _ADMIN_TOKEN):
        return jsonify({'success': False, 'error': 'invalid admin token'}), 401

    try:
        import scrape_runner
        db = get_db()
        limit = max(1, min(500, int(request.args.get('limit', 50))))
        job = (request.args.get('job') or '').strip() or None
        status = (request.args.get('status') or '').strip() or None
        return jsonify({
            'success': True,
            'health': scrape_runner.job_health(db),
            'recent': scrape_runner.recent_runs(db, limit=limit, job_name=job, status=status),
            'known_jobs': sorted(scrape_runner.JOBS.keys()),
        })
    except Exception as exc:
        logger.error('admin/jobs failed: %s', exc, exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/admin/health', methods=['GET'])
def admin_health():
    """Consolidated observability snapshot for the admin dashboard.

    Returns Groq budget per bucket, job_queue depth, RAG index stats,
    and last-N scraper run summaries. Same auth as /api/admin/jobs.
    """
    auth = request.headers.get('X-Admin-Token') or request.headers.get('X-Admin-Secret') or ''
    if not _ADMIN_TOKEN or _ADMIN_TOKEN in _ADMIN_TOKEN_INSECURE_DEFAULTS:
        return jsonify({'success': False, 'error': 'admin endpoints disabled (set ADMIN_TOKEN)'}), 503
    import hmac as _hmac
    if not _hmac.compare_digest(auth, _ADMIN_TOKEN):
        return jsonify({'success': False, 'error': 'invalid admin token'}), 401

    db = get_db()
    out = {'success': True}

    # Groq budget
    try:
        from groq_governor import governor
        out['groq'] = governor.status()
    except Exception as e:
        out['groq'] = {'error': str(e)}

    # Job queue
    try:
        import scrape_runner
        out['queue'] = scrape_runner.queue_depth(db)
    except Exception as e:
        out['queue'] = {'error': str(e)}

    # RAG / kb_chunks
    try:
        from backend.rag import stats as _rag_stats
        out['rag'] = _rag_stats(db)
    except Exception as e:
        out['rag'] = {'error': str(e)}

    # Scraper job health (last-run per job)
    try:
        import scrape_runner
        out['jobs'] = scrape_runner.job_health(db)
    except Exception as e:
        out['jobs'] = {'error': str(e)}

    return jsonify(out)


@app.route('/api/admin/scrape-now', methods=['POST'])
def admin_scrape_now():
    """Trigger a single scraper job (or 'all') on demand.

    Body: {"job": "<job_name>"}  or  {"job": "all"}
    Use sparingly — long-running jobs block the request. For 'all', expect
    30-120s. Each run writes a job_runs row regardless of outcome.
    """
    auth = request.headers.get('X-Admin-Token') or request.headers.get('X-Admin-Secret') or ''
    if not _ADMIN_TOKEN or _ADMIN_TOKEN in _ADMIN_TOKEN_INSECURE_DEFAULTS:
        return jsonify({'success': False, 'error': 'admin endpoints disabled (set ADMIN_TOKEN)'}), 503
    import hmac as _hmac
    if not _hmac.compare_digest(auth, _ADMIN_TOKEN):
        return jsonify({'success': False, 'error': 'invalid admin token'}), 401

    payload = request.get_json(silent=True) or {}
    job = (payload.get('job') or request.args.get('job') or '').strip()
    if not job:
        return jsonify({'success': False, 'error': "missing 'job' (use 'all' to run every scraper)"}), 400

    try:
        import scrape_runner
        db = get_db()
        if job == 'all':
            results = scrape_runner.run_all(db, triggered_by='admin')
            ok = all(r.get('ok') for r in results)
            return jsonify({'success': ok, 'job': 'all', 'results': results})
        if job not in scrape_runner.JOBS:
            return jsonify({'success': False,
                            'error': f'unknown job: {job}',
                            'known': sorted(scrape_runner.JOBS.keys())}), 400
        result = scrape_runner.run(job, db, triggered_by='admin')
        status_code = 200 if result.get('ok') else 500
        return jsonify({'success': result.get('ok', False), **result}), status_code
    except Exception as exc:
        logger.error('admin/scrape-now failed: %s', exc, exc_info=True)
        return jsonify({'success': False, 'error': str(exc)}), 500


# ============ TRUST: per-event-type hit-rate ============
# Lets the trust page show "earnings 64% (n=87) · policy 51% (n=42) · merger 71% (n=12)" etc.
# All horizons or filterable per horizon. Reads from signals JOIN predictions.

@app.route('/api/trust/hit-rate-by-event', methods=['GET'])
def trust_hit_rate_by_event():
    horizon = (request.args.get('horizon') or '').strip().upper()
    days = max(1, min(365, int(request.args.get('days', '180'))))
    db = get_db()
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    horizon_sql = ""
    params = [days]
    if horizon and horizon in ('1D', '3D', '5D', '10D', '20D'):
        horizon_sql = f" AND p.horizon = {p}"
        params.append(horizon)
    sql = f"""
        SELECT s.event_type,
               p.horizon,
               COUNT(*) AS resolved,
               SUM(CASE WHEN p.hit_target = 1 THEN 1 ELSE 0 END) AS hits,
               AVG(p.predicted_return_pct) AS avg_pred,
               AVG(p.actual_return_pct)    AS avg_actual
          FROM signals s
          JOIN predictions p ON s.event_id = p.signal_id
         WHERE p.actual_return_pct IS NOT NULL
           AND p.created_at >= datetime('now', '-{days} days')
           {horizon_sql}
         GROUP BY s.event_type, p.horizon
         HAVING COUNT(*) >= 3
         ORDER BY resolved DESC
    """
    # Postgres syntax slightly differs — use NOW() - INTERVAL when applicable.
    if getattr(db, "is_postgres", False):
        sql = sql.replace(
            f"datetime('now', '-{days} days')",
            f"NOW() - INTERVAL '{days} days'"
        )
    try:
        cur = db.conn.cursor()
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) if not isinstance(r, dict) else r for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"trust_hit_rate_by_event query failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    out = []
    for r in rows:
        resolved = r.get('resolved') or 0
        hits = r.get('hits') or 0
        out.append({
            'event_type':       r.get('event_type') or 'unknown',
            'horizon':          r.get('horizon'),
            'resolved':         resolved,
            'hits':             hits,
            'hit_rate_pct':     round(100.0 * hits / resolved, 1) if resolved else None,
            'avg_predicted_pct': round(float(r.get('avg_pred')   or 0), 2),
            'avg_actual_pct':    round(float(r.get('avg_actual') or 0), 2),
        })
    return jsonify({
        'success': True,
        'data': out,
        'days_window': days,
        'horizon_filter': horizon or 'ALL',
    })

# ============ WAITLIST (landing page) ============
# Self-contained block: own table, own routes. No coupling to the signal DB.
import sqlite3 as _wl_sqlite
import secrets as _wl_secrets
import re as _wl_re
import threading as _wl_threading
_WL_DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'waitlist.db')
_WL_LOCK = _wl_threading.Lock()
_WL_FOUNDER_CAP = 500

def _wl_conn():
    c = _wl_sqlite.connect(_WL_DB_PATH, timeout=10)
    c.row_factory = _wl_sqlite.Row
    return c

def _wl_init():
    with _WL_LOCK:
        c = _wl_conn()
        c.execute("""
            CREATE TABLE IF NOT EXISTS waitlist (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                email           TEXT UNIQUE NOT NULL,
                ref_code        TEXT UNIQUE NOT NULL,
                referred_by     TEXT,
                position        INTEGER NOT NULL,
                source          TEXT,
                ip_hash         TEXT,
                joined_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed_at    TIMESTAMP,
                referral_count  INTEGER DEFAULT 0,
                founder_locked  INTEGER DEFAULT 0
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_wl_email    ON waitlist(email)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_wl_ref      ON waitlist(ref_code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_wl_position ON waitlist(position)")
        c.commit()
        c.close()
_wl_init()

_WL_EMAIL_RE = _wl_re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
_WL_BLACKLIST = {
    'mailinator.com', 'tempmail.com', '10minutemail.com', 'guerrillamail.com',
    'throwaway.email', 'yopmail.com', 'trashmail.com', 'getnada.com',
}
def _wl_email_ok(email: str) -> bool:
    if not email or len(email) > 254 or not _WL_EMAIL_RE.match(email):
        return False
    domain = email.rsplit('@', 1)[-1].lower()
    return domain not in _WL_BLACKLIST

def _wl_ip_hash(req) -> str:
    ip = (req.headers.get('X-Forwarded-For') or req.remote_addr or '').split(',')[0].strip()
    return hashlib.sha256((ip + os.getenv('SECRET_KEY', 'dev-secret')).encode()).hexdigest()[:24]

@app.route('/api/waitlist/join', methods=['POST'])
def waitlist_join():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    ref   = (data.get('ref')   or '').strip() or None
    source = (data.get('source') or 'unknown')[:32]
    if not _wl_email_ok(email):
        return jsonify({'success': False, 'error': 'Invalid email.'}), 400
    ip_hash = _wl_ip_hash(request)
    with _WL_LOCK:
        c = _wl_conn()
        # If email already exists, return their position (idempotent).
        row = c.execute("SELECT * FROM waitlist WHERE email = ?", (email,)).fetchone()
        if row:
            c.close()
            return jsonify({
                'success': True,
                'position': row['position'],
                'ref_code': row['ref_code'],
                'share_url': f"{request.url_root.rstrip('/')}/?ref={row['ref_code']}",
                'founder_locked': bool(row['founder_locked']),
                'already': True,
            })
        # Validate ref (optional)
        ref_row = c.execute("SELECT id FROM waitlist WHERE ref_code = ?", (ref,)).fetchone() if ref else None
        referred_by = ref if ref_row else None
        # Assign position = next sequential
        total = c.execute("SELECT COUNT(*) AS n FROM waitlist").fetchone()['n']
        position = total + 1
        founder_locked = 1 if position <= _WL_FOUNDER_CAP else 0
        # Unique ref code
        ref_code = _wl_secrets.token_urlsafe(6)[:8]
        while c.execute("SELECT 1 FROM waitlist WHERE ref_code = ?", (ref_code,)).fetchone():
            ref_code = _wl_secrets.token_urlsafe(6)[:8]
        try:
            c.execute("""
                INSERT INTO waitlist (email, ref_code, referred_by, position, source, ip_hash, founder_locked)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (email, ref_code, referred_by, position, source, ip_hash, founder_locked))
            if referred_by:
                c.execute("UPDATE waitlist SET referral_count = referral_count + 1 WHERE ref_code = ?", (referred_by,))
            c.commit()
        except _wl_sqlite.IntegrityError as e:
            c.close()
            logger.warning(f"waitlist insert failed: {e}")
            return jsonify({'success': False, 'error': 'Could not save — try again.'}), 500
        c.close()
    share_url = f"{request.url_root.rstrip('/')}/?ref={ref_code}"
    logger.info(f"waitlist join: position={position} founder={bool(founder_locked)} source={source}")
    return jsonify({
        'success': True,
        'position': position,
        'ref_code': ref_code,
        'share_url': share_url,
        'founder_locked': bool(founder_locked),
    })

@app.route('/api/waitlist/count', methods=['GET'])
def waitlist_count():
    with _WL_LOCK:
        c = _wl_conn()
        total   = c.execute("SELECT COUNT(*) AS n FROM waitlist").fetchone()['n']
        claimed = c.execute("SELECT COUNT(*) AS n FROM waitlist WHERE founder_locked = 1").fetchone()['n']
        c.close()
    return jsonify({
        'success': True,
        'total': total,
        'founder_claimed': claimed,
        'founder_cap': _WL_FOUNDER_CAP,
        'founder_remaining': max(0, _WL_FOUNDER_CAP - claimed),
    })

@app.route('/api/waitlist/leaderboard', methods=['GET'])
def waitlist_leaderboard():
    with _WL_LOCK:
        c = _wl_conn()
        rows = c.execute("""
            SELECT email, ref_code, referral_count, position
            FROM waitlist
            WHERE referral_count > 0
            ORDER BY referral_count DESC, position ASC
            LIMIT 10
        """).fetchall()
        c.close()
    def anon(email):
        try:
            local, dom = email.split('@', 1)
            return local[0] + '***' + local[-1] + '@' + dom
        except Exception:
            return '***'
    return jsonify({
        'success': True,
        'data': [
            {'name': anon(r['email']), 'referrals': r['referral_count'], 'position': r['position']}
            for r in rows
        ]
    })

# ============ AUTH CONFIG ============
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_JWT_SECRET = os.getenv('SUPABASE_JWT_SECRET', '')
AUTH_MODE = 'supabase' if SUPABASE_URL else 'local'

# ============ DATABASE ============

db = None

def get_db() -> TickwaveDB:
    """Get or create database connection"""
    global db
    if db is None:
        db = TickwaveDB()
        db.init_schema()
    return db


# ============ RATE LIMITER ============
# In-memory IP -> deque of recent request timestamps. Sized for ~10k unique IPs/day.
import threading
import time as _time
from collections import defaultdict, deque

_RL_LOCK = threading.Lock()
_RL_HITS = defaultdict(deque)  # ip -> deque[float]
RL_WINDOW_SECS = int(os.getenv('RL_WINDOW_SECS', '60'))
# Each reco card fans out to ~6 edge-feature enrichment endpoints (sector,
# leak, whisper, insider, smart-money, hit-rate) on top of the base stock data
# fetch. With 5 cards × 6 calls + base loads + SSE handshakes the per-minute
# budget needs ~300+ for a normal home-page render. 500/min keeps local dev
# usable; remote deployments can override via env var.
RL_MAX_HITS = int(os.getenv('RL_MAX_HITS', '500'))


def _client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'


@app.after_request
def _cache_and_compress(resp):
    """Smart caching + gzip compression for snappier page loads.

    HTML keeps `no-cache` so deploys are visible immediately. CSS/JS get a
    short revalidate window (5 min) so repeat tab-switches don't refetch.
    JSON responses are gzipped when the client accepts it — the unified
    /api/feed payload typically shrinks 4-6x.
    """
    p = (request.path or '')
    # Browser caching policy
    if p.endswith('.html') or p in ('/', ''):
        resp.headers.setdefault('Cache-Control', 'no-cache, must-revalidate')
        resp.headers.setdefault('Pragma', 'no-cache')
    elif p.endswith(('.css', '.js')):
        # 5-min must-revalidate: instant repeat hits, but new deploys propagate fast.
        resp.headers.setdefault('Cache-Control', 'public, max-age=300, must-revalidate')
    elif p.endswith(('.png', '.jpg', '.jpeg', '.svg', '.webp', '.ico', '.woff', '.woff2', '.ttf')):
        resp.headers.setdefault('Cache-Control', 'public, max-age=86400')

    # gzip-compress JSON / HTML / JS / CSS when the client supports it.
    # Skips already-encoded responses, streamed responses, and small payloads.
    try:
        accept_enc = request.headers.get('Accept-Encoding', '')
        if (
            'gzip' in accept_enc.lower()
            and resp.status_code < 300
            and 'Content-Encoding' not in resp.headers
            and not resp.direct_passthrough
            and (resp.mimetype or '').split(';')[0] in (
                'application/json', 'text/html', 'application/javascript',
                'text/javascript', 'text/css', 'text/event-stream',
            )
            and (resp.mimetype or '').split(';')[0] != 'text/event-stream'
            and resp.content_length is not None and resp.content_length > 512
        ):
            import gzip
            gz = gzip.compress(resp.get_data(), compresslevel=5)
            resp.set_data(gz)
            resp.headers['Content-Encoding'] = 'gzip'
            resp.headers['Vary'] = 'Accept-Encoding'
            resp.headers['Content-Length'] = str(len(gz))
    except Exception:
        # If compression fails, return the uncompressed body as-is.
        pass

    return resp


# Shared TTL cache decorator (reused by api_v2, api_v3, api_ext)
from cache_util import ttl_cache  # noqa: E402  (import after Flask app exists)


@app.before_request
def _rate_limit():
    # Only throttle public /api/* endpoints — static files and the health endpoint pass through.
    if not request.path.startswith('/api/'):
        return None
    if request.path == '/api/health':
        return None
    ip = _client_ip()
    # Loopback bypass — local dev should never hit the limiter
    if ip in ('127.0.0.1', '::1', 'localhost') or ip.startswith('192.168.') or ip.startswith('10.'):
        return None
    now = _time.time()
    cutoff = now - RL_WINDOW_SECS
    with _RL_LOCK:
        dq = _RL_HITS[ip]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= RL_MAX_HITS:
            retry = int(dq[0] + RL_WINDOW_SECS - now) + 1
            resp = jsonify({'success': False, 'error': 'rate limited',
                            'retry_after_secs': retry})
            resp.status_code = 429
            resp.headers['Retry-After'] = str(retry)
            return resp
        dq.append(now)
        # GC: every ~5k inserts, prune empty deques to bound memory.
        if len(_RL_HITS) > 5000:
            for k in [k for k, v in _RL_HITS.items() if not v]:
                _RL_HITS.pop(k, None)
    return None


# ============ AUTH MIDDLEWARE ============

def require_auth(f):
    """Decorator: require valid JWT token. Sets g.user_id."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'success': False, 'error': 'Authentication required'}), 401
        try:
            if AUTH_MODE == 'supabase' and SUPABASE_JWT_SECRET:
                payload = pyjwt.decode(token, SUPABASE_JWT_SECRET,
                                       algorithms=['HS256'], audience='authenticated')
                g.user_id = payload['sub']
                g.user_email = payload.get('email', '')
            else:
                payload = pyjwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
                g.user_id = str(payload['user_id'])
                g.user_email = payload.get('email', '')
        except pyjwt.ExpiredSignatureError:
            return jsonify({'success': False, 'error': 'Token expired'}), 401
        except pyjwt.InvalidTokenError:
            return jsonify({'success': False, 'error': 'Invalid token'}), 401
        return f(*args, **kwargs)
    return decorated


def optional_auth(f):
    """Decorator: extract user if token present, but don't require it."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        g.user_id = 'legacy'
        g.user_email = ''
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if token:
            try:
                if AUTH_MODE == 'supabase' and SUPABASE_JWT_SECRET:
                    payload = pyjwt.decode(token, SUPABASE_JWT_SECRET,
                                           algorithms=['HS256'], audience='authenticated')
                    g.user_id = payload['sub']
                    g.user_email = payload.get('email', '')
                else:
                    payload = pyjwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
                    g.user_id = str(payload['user_id'])
                    g.user_email = payload.get('email', '')
            except Exception:
                pass  # Invalid token = treat as anonymous
        return f(*args, **kwargs)
    return decorated


# ============ ADMIN AUTH ============
# Admin endpoints (calibrate, resolve_predictions, backfill_forensics,
# seed_promoter, init_ext_schema) bypass user JWT and authenticate via a
# single shared admin token. The token MUST be set in production via the
# ADMIN_TOKEN env var; if unset the endpoints refuse all calls.

_ADMIN_TOKEN = (os.getenv('ADMIN_TOKEN') or os.getenv('ADMIN_SECRET') or '').strip()
# Reject the previous default placeholder explicitly — its presence anywhere
# in env vars or config means the operator forgot to set a real token.
_ADMIN_TOKEN_INSECURE_DEFAULTS = {'change-me-admin', 'changeme', 'admin', ''}


def require_admin(f):
    """Decorator: require admin token in `X-Admin-Token` (preferred) or
    `X-Admin-Secret` (legacy) header, matched against ADMIN_TOKEN/ADMIN_SECRET.

    Refuses all calls if the env var is unset OR set to a known insecure
    default (closed by default — prevents accidental exposure). Uses constant-
    time comparison to avoid timing oracles. Every call is audit-logged.
    """
    import hmac as _hmac
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not _ADMIN_TOKEN or _ADMIN_TOKEN in _ADMIN_TOKEN_INSECURE_DEFAULTS:
            logger.warning("admin endpoint blocked: ADMIN_TOKEN not configured (or insecure default)")
            return jsonify({'success': False,
                            'error': 'admin endpoints disabled (set ADMIN_TOKEN env var)'}), 503
        provided = (request.headers.get('X-Admin-Token')
                    or request.headers.get('X-Admin-Secret') or '')
        if not provided or not _hmac.compare_digest(provided, _ADMIN_TOKEN):
            try:
                audit_log('admin_denied', request.path,
                          {'method': request.method, 'remote': request.remote_addr})
            except Exception:
                pass
            return jsonify({'success': False, 'error': 'invalid admin token'}), 401
        try:
            audit_log('admin_call', request.path,
                      {'method': request.method,
                       'args': dict(request.args),
                       'remote': request.remote_addr})
        except Exception:
            pass
        return f(*args, **kwargs)
    return decorated


# ============ AUDIT LOG ============
# Lightweight audit trail for security-relevant actions. Logs to the
# `audit_log` table (created lazily on first write). Calls never raise —
# audit failures must not break the hot path.

_AUDIT_TABLE_READY = False


def _ensure_audit_table():
    global _AUDIT_TABLE_READY
    if _AUDIT_TABLE_READY:
        return
    try:
        d = get_db()
        cur = d.conn.cursor()
        if getattr(d, 'is_postgres', False):
            cur.execute("""CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT,
                action TEXT NOT NULL,
                resource TEXT,
                ip TEXT,
                payload JSONB
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS audit_ts_idx ON audit_log(ts DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS audit_user_idx ON audit_log(user_id, ts DESC)")
        else:
            cur.execute("""CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT,
                action TEXT NOT NULL,
                resource TEXT,
                ip TEXT,
                payload TEXT
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS audit_ts_idx ON audit_log(ts DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS audit_user_idx ON audit_log(user_id, ts DESC)")
        d.conn.commit()
        _AUDIT_TABLE_READY = True
    except Exception as exc:
        logger.warning(f"audit table init failed (logging disabled): {exc}")


def audit_log(action: str, resource: str = '', payload=None):
    """Append an audit entry. Best-effort — never raises."""
    import json as _json
    try:
        _ensure_audit_table()
        if not _AUDIT_TABLE_READY:
            return
        d = get_db()
        cur = d.conn.cursor()
        p = "%s" if getattr(d, 'is_postgres', False) else "?"
        user_id = getattr(g, 'user_id', None) if request else None
        ip = request.remote_addr if request else None
        cur.execute(
            f"INSERT INTO audit_log (user_id, action, resource, ip, payload) VALUES ({p},{p},{p},{p},{p})",
            (user_id, action, resource or '', ip,
             _json.dumps(payload) if payload is not None else None),
        )
        d.conn.commit()
    except Exception as exc:
        logger.debug(f"audit_log write failed: {exc}")


# ============ PER-USER THROTTLE ============
# Simple in-memory token bucket keyed by user_id (or remote IP for anon).
# Designed to protect expensive endpoints (simulator, screeners) without
# adding Redis as a dep. State is in-process so multiple workers each get
# their own bucket — acceptable since the goal is to prevent runaway loops,
# not enforce a strict global cap.

import threading as _threading
_THROTTLE_LOCK = _threading.Lock()
_THROTTLE_BUCKETS: dict = {}


def throttle(name: str, max_calls: int, window_secs: int):
    """Decorator: cap calls per (endpoint × user_id-or-IP) within window."""
    import time as _time
    def deco(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            uid = getattr(g, 'user_id', None) or (request.remote_addr if request else 'anon')
            key = f"{name}:{uid}"
            now = _time.time()
            with _THROTTLE_LOCK:
                hits = _THROTTLE_BUCKETS.get(key, [])
                hits = [t for t in hits if (now - t) < window_secs]
                if len(hits) >= max_calls:
                    retry_after = int(window_secs - (now - hits[0]))
                    return jsonify({
                        'success': False,
                        'error': f'rate limit exceeded for {name}',
                        'retry_after_secs': max(retry_after, 1),
                    }), 429
                hits.append(now)
                _THROTTLE_BUCKETS[key] = hits
            return f(*args, **kwargs)
        return decorated
    return deco


# ============ SCRAPER SCHEDULER ============

scraper_status = {
    'last_run': None,
    'last_result': None,
    'running': False,
    'error': None
}


def run_scraper_job():
    """Run the scraper pipeline (called by scheduler).

    Watchdog: if the previous run has been "running" for more than 5 minutes,
    force-release the lock. Protects against a single pipeline hang blocking
    all subsequent cycles indefinitely.
    """
    # Watchdog: auto-release stale lock (previous run hung past 5 min)
    stale_cutoff_secs = 300
    started = scraper_status.get('started_at')
    if scraper_status.get('running') and started:
        try:
            age = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
        except Exception:
            age = stale_cutoff_secs + 1
        if age > stale_cutoff_secs:
            logger.warning(f"Scraper lock STALE ({age:.0f}s) — force-releasing to unblock scheduler")
            scraper_status['running'] = False
            scraper_status['error'] = f'watchdog released lock after {age:.0f}s'

    if scraper_status['running']:
        logger.info("Scraper already running, skipping")
        return

    scraper_status['running'] = True
    scraper_status['started_at'] = datetime.now().isoformat()
    scraper_status['error'] = None

    try:
        from hybrid_scraper import DataPipeline
        pipeline = DataPipeline()
        output = pipeline.run()

        scraper_status['last_run'] = datetime.now().isoformat()
        scraper_status['last_result'] = output.get('summary', {})

        # Also save JSON backup
        pipeline.save_output(output)

        # Process notifications for new high-alpha signals
        signals = output.get('signals', [])
        if signals:
            try:
                process_signal_notifications(signals, get_db())
            except Exception as e:
                logger.error(f"Notification processing failed: {e}")

        # Push fresh signals + events to SSE subscribers (fast user-facing path)
        try:
            import realtime
            for ev in (output.get('events') or [])[:50]:
                realtime.broadcast_raw({
                    'event_id': ev.get('event_id'),
                    'title': ev.get('title'),
                    'summary': ev.get('summary'),
                    'tickers': ev.get('companies') or [],
                    'sentiment': ev.get('sentiment'),
                    'source_name': ev.get('source'),
                    'link': ev.get('link'),
                    'published_at': ev.get('published') or ev.get('timestamp'),
                    'event_type': ev.get('event_type'),
                    'magnitude': ev.get('magnitude'),
                })
            for sg in (signals or [])[:30]:
                realtime.broadcast_scored({
                    'event_id': sg.get('event_id'),
                    'ticker': sg.get('ticker'),
                    'company': sg.get('company'),
                    'alpha_score': sg.get('alpha_score'),
                    'sentiment': sg.get('sentiment'),
                    'event_type': sg.get('event_type'),
                    'headline': sg.get('headline'),
                    'link': sg.get('link'),
                    'forensic_band': sg.get('forensic_band'),
                    'predictions': sg.get('predictions'),
                })
        except Exception as e:
            logger.warning(f"SSE broadcast failed: {e}")

        # Smart-alert evaluation (volume / news velocity / forensic flip / alpha threshold)
        try:
            _evaluate_smart_alerts(signals, articles=output.get('events'))
        except Exception:
            pass

        logger.info(f"Scraper completed: {output.get('summary', {})}")

    except Exception as e:
        scraper_status['error'] = str(e)
        logger.error(f"Scraper job failed: {e}", exc_info=True)
    finally:
        scraper_status['running'] = False
        scraper_status['started_at'] = None


def run_prediction_tracker_job():
    """Check expired predictions against actual prices (runs daily)"""
    try:
        from prediction_tracker import PredictionTracker
        tracker = PredictionTracker(get_db())
        result = tracker.run()
        logger.info(f"Prediction tracker: {result}")
    except Exception as e:
        logger.error(f"Prediction tracker failed: {e}", exc_info=True)


def run_signal_expiry_job():
    """Mark stale active signals as expired so the active feed stays actionable.

    Default: anything older than 7 days. Signals pile up as 'active' forever
    otherwise; with cooldown dedup also enabled, this keeps the active set bounded.
    """
    try:
        days = int(os.getenv('SIGNAL_EXPIRY_DAYS', '7'))
        n = get_db().expire_stale_signals(max_age_days=days)
        if n:
            logger.info(f"Signal expiry: marked {n} signals expired (older than {days}d)")
    except Exception as e:
        logger.error(f"Signal expiry failed: {e}", exc_info=True)


def start_scheduler():
    """Start the background scraper scheduler"""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        interval = int(os.getenv('SCRAPER_INTERVAL', '3'))

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            run_scraper_job,
            'interval',
            minutes=interval,
            id='scraper_job',
            max_instances=1
        )
        # Prediction tracker: daily at 10:30 UTC (4 PM IST, after NSE close)
        scheduler.add_job(
            run_prediction_tracker_job,
            'cron',
            hour=10, minute=30,
            id='prediction_tracker_job',
            max_instances=1
        )
        # Signal expiry: hourly — cheap UPDATE, keeps active feed bounded.
        scheduler.add_job(
            run_signal_expiry_job,
            'interval',
            hours=1,
            id='signal_expiry_job',
            max_instances=1
        )

        # Register extension jobs (promoter, forensic, overnight, handle scorer, DQ)
        try:
            from api_ext import register_jobs
            register_jobs(scheduler, get_db)
        except Exception as ext_exc:
            logger.error(f"Failed to register extension jobs: {ext_exc}")

        # ----- New scheduled jobs (bulk-deals, SEBI bans, manipulation, F&O) -----
        def _bulk_deals_daily():
            try:
                from bulk_deals import fetch_nse_bulk_deals, fetch_nse_block_deals, fetch_sebi_bans
                db_ = get_db()
                a = fetch_nse_bulk_deals(db_)
                b = fetch_nse_block_deals(db_)
                c = fetch_sebi_bans(db_)
                logger.info(f"bulk-deals job: bulk={a} block={b} sebi_bans={c}")
            except Exception as e:
                logger.warning(f"bulk_deals job failed: {e}")

        def _manipulation_daily():
            try:
                from forensics.manipulation_detectors import (
                    detect_circular_pattern, detect_insider_exit_pattern,
                )
                db_ = get_db()
                cp = detect_circular_pattern(db_)
                ie = detect_insider_exit_pattern(db_)
                logger.info(f"manipulation job: circular={len(cp)} insider_exit={len(ie)}")
            except Exception as e:
                logger.warning(f"manipulation job failed: {e}")

        def _fo_intraday():
            try:
                # Snapshot the F&O top 10 universe (heuristic: tickers with options activity)
                from fo_signals import snapshot_and_persist
                top_universe = ['NIFTY', 'BANKNIFTY', 'RELIANCE', 'TCS', 'INFY',
                                'HDFCBANK', 'ICICIBANK', 'SBIN', 'TATAMOTORS',
                                'AXISBANK', 'BHARTIARTL', 'LT']
                # NIFTY/BANKNIFTY are indices — caller passes is_index=False here
                # but our function tries the equity URL first; for indices we'd
                # split. Keep simple: equity-only for now.
                equities = [t for t in top_universe if t not in ('NIFTY', 'BANKNIFTY')]
                result = snapshot_and_persist(get_db(), equities, is_index=False)
                logger.info(f"fo intraday: {result}")
            except Exception as e:
                logger.warning(f"fo intraday failed: {e}")

        scheduler.add_job(_bulk_deals_daily, 'cron', hour=12, minute=0,
                          id='bulk_deals_daily', max_instances=1)
        scheduler.add_job(_manipulation_daily, 'cron', hour=11, minute=30,
                          id='manipulation_daily', max_instances=1)
        scheduler.add_job(_fo_intraday, 'cron',
                          day_of_week='mon-fri', hour='4-10', minute=15,
                          id='fo_intraday', max_instances=1,
                          # Skip if previous run still going
                          coalesce=True)

        # Volume anomalies: daily after market close (10:30 UTC = 4 PM IST).
        # Powers the `vol_divergence` factor in scraper/premover.py — without
        # this the premover top-50 board has a dead factor.
        def _volume_anomalies_daily():
            try:
                import scrape_runner
                scrape_runner.run('volume_anomalies', get_db(), triggered_by='scheduler')
            except Exception as e:
                logger.warning(f"volume_anomalies job failed: {e}")
        scheduler.add_job(_volume_anomalies_daily, 'cron',
                          day_of_week='mon-fri', hour=10, minute=45,
                          id='volume_anomalies_daily', max_instances=1, coalesce=True)

        # FII/DII derivatives: nightly at 1 AM UTC (NSE publishes T+1).
        def _fo_fii_dii_nightly():
            try:
                import scrape_runner
                scrape_runner.run('fo_fii_dii', get_db(), triggered_by='scheduler')
            except Exception as e:
                logger.warning(f"fo_fii_dii job failed: {e}")
        scheduler.add_job(_fo_fii_dii_nightly, 'cron',
                          day_of_week='tue-sat', hour=1, minute=0,
                          id='fo_fii_dii_nightly', max_instances=1, coalesce=True)

        # Social hub.
        #
        # All three sources (X, Reddit, Telegram) ingest — but Reddit + Telegram
        # only surface SERIOUS, HIGH-QUALITY buzz. The hub-level quality gate
        # (seriousness keywords + min upvote / engagement + pump-dump
        # rejection) does the filtering; signals from these still never enter
        # the curated recommendations (get_active_signals filters them out at
        # SQL level). The point is to surface them in a dedicated social tab.
        SOCIAL_ENABLE_REDDIT   = os.getenv('SOCIAL_ENABLE_REDDIT',   '1') == '1'
        SOCIAL_ENABLE_TELEGRAM = os.getenv('SOCIAL_ENABLE_TELEGRAM', '1') == '1'
        SOCIAL_ENABLE_X        = os.getenv('SOCIAL_ENABLE_X',        '1') == '1'

        def _social_quick():
            if not SOCIAL_ENABLE_X:
                return
            try:
                from social.hub import collect_x, seed_default_sources
                from stock_universe import UNIVERSE_TICKERS
                db_ = get_db()
                seed_default_sources(db_)
                n = collect_x(db_, UNIVERSE_TICKERS)
                if n:
                    logger.info(f"social X (verified): {n} new posts")
            except Exception as e:
                logger.warning(f"social X failed: {e}")

        def _social_serious():
            if not (SOCIAL_ENABLE_REDDIT or SOCIAL_ENABLE_TELEGRAM):
                return
            try:
                from stock_universe import UNIVERSE_TICKERS
                db_ = get_db()
                a = b = 0
                if SOCIAL_ENABLE_REDDIT:
                    from social.hub import collect_reddit_serious
                    a = collect_reddit_serious(db_, UNIVERSE_TICKERS)
                if SOCIAL_ENABLE_TELEGRAM:
                    from social.hub import collect_telegram_serious
                    b = collect_telegram_serious(db_, UNIVERSE_TICKERS)
                if a or b:
                    logger.info(f"social serious: reddit={a} telegram={b}")
            except Exception as e:
                logger.warning(f"social serious failed: {e}")

        if SOCIAL_ENABLE_X:
            scheduler.add_job(_social_quick, 'interval', minutes=5,
                              id='social_quick', max_instances=1, coalesce=True)
        if SOCIAL_ENABLE_REDDIT or SOCIAL_ENABLE_TELEGRAM:
            scheduler.add_job(_social_serious, 'interval', minutes=15,
                              id='social_serious', max_instances=1, coalesce=True)
        logger.info(f"social pipeline: X={SOCIAL_ENABLE_X} reddit={SOCIAL_ENABLE_REDDIT} telegram={SOCIAL_ENABLE_TELEGRAM}")

        # Notification retry drain — every 60s, picks up due-for-retry items
        # from notifications.db and re-attempts Discord/Telegram sends.
        def _notif_retry_drain():
            try:
                from notification_retry import process_pending
                process_pending(limit=20)
            except Exception as e:
                logger.warning(f"notif retry drain failed: {e}")
        scheduler.add_job(_notif_retry_drain, 'interval', seconds=60,
                          id='notif_retry_drain', max_instances=1, coalesce=True)

        # NSE option-chain ingestion — every 5 min during market hours.
        # The job is cheap (3 HTTP requests), and the writer is gated on
        # is_market_open() so we don't hammer NSE on weekends.
        def _oc_ingest():
            try:
                from market_clock import is_market_open
                if not is_market_open():
                    return
                from nse_option_chain import ingest_all_symbols
                ingest_all_symbols()
            except Exception as e:
                logger.warning(f"option-chain ingest failed: {e}")
        scheduler.add_job(_oc_ingest, 'interval', minutes=5,
                          id='oc_ingest', max_instances=1, coalesce=True)

        # Daily personalized briefing — 08:30 IST (= 03:00 UTC).
        # Starter + Pro tier. Enqueues one job per eligible user; worker
        # drains them throughout the morning rate-limited by Groq budget.
        def _briefing_dispatch():
            try:
                from backend.daily_briefing import enqueue_all_pro_users
                n = enqueue_all_pro_users(get_db())
                logger.info(f"daily briefing: enqueued {n} starter+pro users")
            except Exception as e:
                logger.warning(f"daily briefing dispatch failed: {e}")
        scheduler.add_job(_briefing_dispatch, 'cron',
                          day_of_week='mon-fri', hour=3, minute=0,
                          id='daily_briefing_dispatch',
                          max_instances=1, coalesce=True)

        # Pre-market briefing — 07:30 IST (= 02:00 UTC). Pro tier only.
        # Earlier than daily, focused on overnight global moves + ADR
        # shifts + pre-open positioning ideas.
        def _premarket_dispatch():
            try:
                from backend.daily_briefing import enqueue_pre_market_users
                n = enqueue_pre_market_users(get_db())
                logger.info(f"pre-market briefing: enqueued {n} pro users")
            except Exception as e:
                logger.warning(f"pre-market briefing dispatch failed: {e}")
        scheduler.add_job(_premarket_dispatch, 'cron',
                          day_of_week='mon-fri', hour=2, minute=0,
                          id='pre_market_briefing_dispatch',
                          max_instances=1, coalesce=True)

        scheduler.start()
        logger.info(f"Scheduler started: scraper every {interval}min, prediction tracker daily 10:30 UTC")

        # Run scraper once on startup
        scheduler.add_job(run_scraper_job, 'date',
                          run_date=datetime.now(),
                          id='scraper_initial')

    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")


# ============ FRONTEND SERVING ============

@app.route('/')
def serve_index():
    return send_from_directory(FRONTEND_DIR, 'index.html')


# Legacy redirect: forecast.html was merged into earnings.html on 2026-05-19.
# Preserve old bookmarks / cached links.
@app.route('/forecast.html')
def serve_forecast_redirect():
    from flask import redirect
    hash_part = request.query_string.decode('utf-8', errors='ignore')
    target = '/earnings.html'
    if hash_part:
        target += '?' + hash_part
    return redirect(target, code=301)


@app.route('/<path:filename>')
def serve_static(filename):
    """Serve frontend static files. Skip /api/ paths — those are handled by API routes."""
    if filename.startswith('api/'):
        return jsonify({'success': False, 'error': 'Endpoint not found'}), 404
    file_path = os.path.join(FRONTEND_DIR, filename)
    if os.path.isfile(file_path):
        return send_from_directory(FRONTEND_DIR, filename)
    return send_from_directory(FRONTEND_DIR, 'index.html')


# ============ AUTH ENDPOINTS ============

@app.route('/api/auth/signup', methods=['POST'])
def auth_signup():
    """Sign up (local SQLite auth only; Supabase uses client-side SDK)"""
    if AUTH_MODE == 'supabase':
        return jsonify({'success': False, 'error': 'Use Supabase JS SDK for signup',
                        'auth_mode': 'supabase', 'supabase_url': SUPABASE_URL}), 400
    try:
        import bcrypt
        data = request.get_json()
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''
        if not email or not password:
            return jsonify({'success': False, 'error': 'Email and password required'}), 400
        if len(password) < 6:
            return jsonify({'success': False, 'error': 'Password must be at least 6 characters'}), 400

        existing = get_db().get_user_by_email(email)
        if existing:
            return jsonify({'success': False, 'error': 'Email already registered'}), 409

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        success = get_db().create_user(email, pw_hash, display_name=email.split('@')[0])
        if not success:
            return jsonify({'success': False, 'error': 'Failed to create account'}), 500

        user = get_db().get_user_by_email(email)
        token = pyjwt.encode({
            'user_id': user['id'], 'email': email,
            'exp': datetime.utcnow() + timedelta(days=30)
        }, app.config['SECRET_KEY'], algorithm='HS256')

        return jsonify({'success': True, 'token': token,
                        'user': {'id': user['id'], 'email': email}}), 201
    except Exception as e:
        logger.error(f"Signup error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """Login (local SQLite auth only)"""
    if AUTH_MODE == 'supabase':
        return jsonify({'success': False, 'error': 'Use Supabase JS SDK for login',
                        'auth_mode': 'supabase', 'supabase_url': SUPABASE_URL}), 400
    try:
        import bcrypt
        data = request.get_json()
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''

        user = get_db().get_user_by_email(email)
        if not user:
            return jsonify({'success': False, 'error': 'Invalid email or password'}), 401

        if not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
            return jsonify({'success': False, 'error': 'Invalid email or password'}), 401

        token = pyjwt.encode({
            'user_id': user['id'], 'email': email,
            'exp': datetime.utcnow() + timedelta(days=30)
        }, app.config['SECRET_KEY'], algorithm='HS256')

        return jsonify({'success': True, 'token': token,
                        'user': {'id': user['id'], 'email': email}})
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/auth/me', methods=['GET'])
@require_auth
def auth_me():
    """Get current user info"""
    return jsonify({'success': True, 'user': {'id': g.user_id, 'email': g.user_email}})


# ============ HEALTH CHECK ============

@app.route('/api/health', methods=['GET'])
def health():
    """Liveness + scraper freshness + Groq budget.

    `status` is "ok" when the scraper has produced a signal in the last
    HEALTH_STALE_MINUTES; "stale" otherwise. Use this for an external watchdog.
    """
    HEALTH_STALE_MINUTES = int(os.getenv('HEALTH_STALE_MINUTES', '120'))
    # SQLite/Postgres CURRENT_TIMESTAMP is UTC; compare in UTC so IST-deployed
    # backends don't permanently report 5h30m of false staleness.
    now_utc = datetime.utcnow()
    last_signal_age_secs = None
    overall = 'ok'

    try:
        cursor = get_db().conn.cursor()
        cursor.execute('SELECT MAX(created_at) FROM signals')
        row = cursor.fetchone()
        if row and row[0]:
            raw = row[0] if isinstance(row[0], str) else str(row[0])
            try:
                last = datetime.fromisoformat(raw.replace('Z', '+00:00').split('.')[0])
                age = (now_utc - last.replace(tzinfo=None)).total_seconds()
                last_signal_age_secs = int(age)
                if age > HEALTH_STALE_MINUTES * 60:
                    overall = 'stale'
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"health: signal freshness check failed: {e}")

    groq = None
    try:
        from groq_governor import governor
        groq = governor.status()
    except Exception:
        pass

    return jsonify({
        'status': overall,
        'timestamp_utc': now_utc.isoformat() + 'Z',
        'database': 'postgresql' if os.getenv('DATABASE_URL', '').startswith('postgres') else 'sqlite',
        'scraper': scraper_status,
        'last_signal_age_secs': last_signal_age_secs,
        'stale_threshold_secs': HEALTH_STALE_MINUTES * 60,
        'groq_budget': groq,
    })


# ============ DASHBOARD ============

@app.route('/api/dashboard', methods=['GET'])
@ttl_cache(seconds=30)
def dashboard():
    """Aggregated dashboard stats (single call for home page)"""
    try:
        stats = get_db().get_dashboard_stats()
        return jsonify({'success': True, 'data': stats})
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return jsonify({'success': True, 'data': {
            'active_signals': 0, 'avg_alpha': 0, 'avg_confidence': 0,
            'events_today': 0, 'total_events': 0, 'current_regime': 'unknown',
            'regime_strength': 0, 'watchlist_count': 0,
            'bullish_count': 0, 'bearish_count': 0, 'neutral_count': 0
        }})


# ============ SIGNALS ============

@app.route('/api/signals', methods=['GET'])
@optional_auth
def get_signals():
    """Get active trading signals — joined with events.summary so the
    client-side subject-mismatch filter has more text to chew on (not just
    the 80-char title).

    Tier-aware: free + anonymous users see the top FREE_SIGNALS_VISIBLE (5)
    by alpha_score; starter + pro see the full feed. Locked count is
    returned in the response meta so the UI can render an upgrade tile.

    Note: the previous @ttl_cache(seconds=30) decorator was removed because
    the response now varies by tier. The underlying get_active_signals()
    can still be cached at the DB layer if needed.
    """
    try:
        limit = request.args.get('limit', 50, type=int)
        signals = get_db().get_active_signals(limit)
        if not signals:
            return jsonify({'success': True, 'count': 0, 'data': [],
                           'total_available': 0, 'locked_count': 0,
                           'tier': 'free'})
        # Enrich with the originating event's RSS summary (1-3 sentence preview).
        # Signal event_id = "TICKER_TYPE_HASH"; events.event_id is just "HASH".
        # We strip prefixes and match on the trailing hash component.
        try:
            def _hash_part(eid):
                if not eid: return None
                parts = str(eid).rsplit('_', 1)
                return parts[-1] if parts else eid
            hash_to_full = {}
            for s in signals:
                eid = s.get('event_id')
                h = _hash_part(eid)
                if h:
                    hash_to_full.setdefault(h, []).append(s)
            hashes = list(hash_to_full.keys())
            if hashes:
                placeholders = ','.join(['?'] * len(hashes))
                cur = get_db().conn.cursor()
                cur.execute(
                    f"SELECT event_id, summary, title FROM events WHERE event_id IN ({placeholders})",
                    hashes)
                rows = cur.fetchall()
                summary_by_hash = {}
                title_by_hash = {}
                for r in rows:
                    if isinstance(r, dict):
                        h = r.get('event_id')
                        summary_by_hash[h] = r.get('summary') or ''
                        title_by_hash[h]   = r.get('title')   or ''
                    else:
                        summary_by_hash[r[0]] = r[1] or ''
                        title_by_hash[r[0]]   = r[2] or ''
                for h, sig_list in hash_to_full.items():
                    summ = summary_by_hash.get(h, '')
                    title = title_by_hash.get(h, '')
                    for s in sig_list:
                        s['summary'] = summ
                        # If signal.headline is empty, backfill from events.title
                        if not s.get('headline') and title:
                            s['headline'] = title
        except Exception as e:
            # Non-fatal — just don't enrich
            logger.debug(f"signals summary-join skipped: {e}")
            for s in signals:
                s.setdefault('summary', '')

        # Enrich each signal with the ticker's sector (from STOCK_UNIVERSE) so
        # the curated card can render a sector chip + the curation logic can
        # apply MAX_PER_SECTOR diversity.
        try:
            from stock_universe import STOCK_UNIVERSE
            for s in signals:
                tk = (s.get('ticker') or '').upper()
                if tk and not s.get('sector'):
                    meta = STOCK_UNIVERSE.get(tk) or {}
                    sec = meta.get('sector') or meta.get('Sector') or ''
                    if sec: s['sector'] = sec
        except Exception as e:
            logger.debug(f"signals sector-enrich skipped: {e}")

        # ── Addendum 2026-05-18 — multi-source enrichment ─────────────────
        # JOIN to event_clusters so each signal carries:
        #   cluster_size, sources (list), canonical_headline, first_seen_source
        # Then run signal_synthesis to produce a copyright-safe one-line
        # factual summary (replaces the publisher's RSS blurb in the UI).
        # For signals without a cluster row (the common case until the RSS
        # persister has run a full cycle) we degrade gracefully: cluster_size
        # defaults to 1, sources contains the signal's own source, and the
        # synthesized summary still renders from event_type/ticker fields.
        try:
            hashes = [s.get('cluster_hash') for s in signals if s.get('cluster_hash')]
            cluster_by_hash = {}
            if hashes:
                cur = get_db().conn.cursor()
                placeholders = ','.join(['?'] * len(hashes))
                cur.execute(
                    f"""SELECT cluster_hash, canonical_headline, member_count,
                              sources, first_seen_source, first_seen_at
                         FROM event_clusters WHERE cluster_hash IN ({placeholders})""",
                    hashes,
                )
                for row in cur.fetchall() or []:
                    if isinstance(row, dict):
                        cluster_by_hash[row['cluster_hash']] = row
                    else:
                        cluster_by_hash[row[0]] = {
                            'cluster_hash':       row[0],
                            'canonical_headline': row[1],
                            'member_count':       row[2],
                            'sources':            row[3] or '',
                            'first_seen_source':  row[4],
                            'first_seen_at':      row[5],
                        }
            for s in signals:
                cluster = cluster_by_hash.get(s.get('cluster_hash'))
                if cluster:
                    s['cluster_size']       = int(cluster.get('member_count') or 1)
                    s['canonical_headline'] = cluster.get('canonical_headline')
                    s['first_seen_source']  = (s.get('first_seen_source')
                                              or cluster.get('first_seen_source'))
                    # De-dup comma-separated sources list (the upsert can
                    # double-add an outlet if the same article re-scrapes).
                    raw_sources = (cluster.get('sources') or '').split(',')
                    seen, ordered = set(), []
                    for src in raw_sources:
                        src = (src or '').strip()
                        if src and src not in seen:
                            seen.add(src)
                            ordered.append(src)
                    s['sources'] = ordered
                else:
                    # Orphan signal — no cluster row yet. Honest defaults.
                    s['cluster_size'] = int(s.get('cluster_size') or 1)
                    s.setdefault('canonical_headline', s.get('headline'))
                    src = s.get('source') or s.get('first_seen_source')
                    s['sources'] = [src] if src else []
            # Apply copyright-safe synthesized summary on top of EVERY signal.
            from signal_synthesis import synthesize_summary, synthesize_headline
            for s in signals:
                s['synthesized_summary']  = synthesize_summary(s)
                s['synthesized_headline'] = synthesize_headline(s)
        except Exception as e:
            logger.debug(f"signals cluster-enrich skipped: {e}")

        # Tier-aware slice: free + anon see only top N by alpha_score.
        # Starter + Pro see the full feed. Always include locked_count
        # so the client can render an upgrade CTA in the right slot.
        total_available = len(signals)
        try:
            from backend.freemium import get_user_tier, signal_feed_cap
            user_id = getattr(g, 'user_id', None)
            tier = get_user_tier(get_db(), user_id) if user_id else 'free'
        except Exception:
            tier = 'free'
        cap = None
        try:
            from backend.freemium import signal_feed_cap as _cap
            cap = _cap(tier)
        except Exception:
            pass
        if cap is not None and total_available > cap:
            # Sort by alpha_score desc to keep the top-N visible
            try:
                signals = sorted(
                    signals,
                    key=lambda s: float(s.get('alpha_score') or 0),
                    reverse=True,
                )
            except Exception:
                pass
            visible = signals[:cap]
            locked_count = total_available - len(visible)
        else:
            visible = signals
            locked_count = 0

        return jsonify({
            'success': True,
            'count': len(visible),
            'total_available': total_available,
            'locked_count': locked_count,
            'tier': tier,
            'upgrade_url': '/app/pricing.html' if locked_count > 0 else None,
            'data': visible,
        })
    except Exception as e:
        logger.error(f"Get signals error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': [],
                       'total_available': 0, 'locked_count': 0, 'tier': 'free'})


@app.route('/api/signals/<signal_id>', methods=['GET'])
def get_signal(signal_id):
    """Get single signal details"""
    try:
        signal = get_db().get_signal_by_id(signal_id)
        if signal:
            return jsonify({'success': True, 'data': signal})
    except Exception as e:
        logger.error(f"Get signal error: {e}")
    return jsonify({'success': False, 'error': 'Signal not found'}), 404


# ============ EVENTS ============

@app.route('/api/events', methods=['GET'])
@ttl_cache(seconds=30)
def get_events():
    """Get events from database with optional type filtering.

    Adds inline news-quality classification:
        - drops noise items by default (?include_noise=1 to override)
        - attaches `impact_tier`, `quality_score`, `kinds`,
          `market_implication` to every row
        - filters by `?min_quality=` tier name (noise|generic|meaningful|high|critical)
    Existing field shape preserved, new fields are additive.
    """
    try:
        limit = request.args.get('limit', 50, type=int)
        event_type = request.args.get('type')
        include_noise = (request.args.get('include_noise') or '').lower() in ('1', 'true', 'yes')
        min_quality_param = (request.args.get('min_quality') or '').strip().lower() or None

        # Over-fetch when we know we'll be filtering, so noise removal doesn't
        # leave the feed thin.
        raw_limit = limit * 3 if (not include_noise or min_quality_param) else limit

        if event_type:
            cur = get_db().conn.cursor()
            # 'corporate' covers buyback/dividend/split/bonus/rights
            if event_type == 'corporate':
                cur.execute(
                    """SELECT id, title, summary, event_type, impact_score, companies, published_at
                       FROM events
                       WHERE event_type IN ('buyback','dividend','split','bonus','rights')
                       ORDER BY published_at DESC LIMIT ?""",
                    (raw_limit,))
            else:
                cur.execute(
                    """SELECT id, title, summary, event_type, impact_score, companies, published_at
                       FROM events WHERE event_type = ?
                       ORDER BY published_at DESC LIMIT ?""",
                    (event_type, raw_limit))
            rows = cur.fetchall()
            events = [{
                'id': r[0], 'title': r[1], 'summary': r[2], 'event_type': r[3],
                'impact_score': float(r[4]) if r[4] else 0,
                'companies': r[5], 'published_at': str(r[6]) if r[6] else None
            } for r in rows]
        else:
            events = get_db().get_recent_events(raw_limit)

        # ---- News-quality enrichment + noise filtering ----
        # Cheap rule-based classifier; ~50µs per row.
        try:
            from news_quality import classify_dict as _classify_news_dict
            _TIER_ORDER = {"noise": 0, "generic": 1, "meaningful": 2,
                           "high": 3, "critical": 4}
            min_tier_idx = _TIER_ORDER.get(min_quality_param, -1) if min_quality_param else -1
            try:
                from causal_map import event_implication as _causal_lookup
            except Exception:
                _causal_lookup = None
            enriched = []
            for ev in events:
                q = _classify_news_dict(ev)
                ev["impact_tier"] = q.impact_tier
                ev["quality_score"] = q.quality_score
                if q.kinds:
                    ev["kinds"] = q.kinds
                if q.market_implication:
                    ev["market_implication"] = q.market_implication
                # Macro causal lookup adds beneficiary/victim sectors when the
                # event text matches a known macro/policy trigger.
                if _causal_lookup is not None:
                    try:
                        blob = (ev.get("title") or "") + "  " + (ev.get("summary") or "")
                        impl = _causal_lookup(blob)
                        if impl:
                            ev["beneficiary_sectors"] = impl.get("helps") or []
                            ev["affected_sectors"] = impl.get("hurts") or []
                            # Causal summary wins over generic kind-hint
                            ev["market_implication"] = impl.get("summary") or ev.get("market_implication")
                    except Exception:
                        pass
                if not include_noise and q.is_noise:
                    continue
                if min_tier_idx >= 0 and _TIER_ORDER.get(q.impact_tier, 0) < min_tier_idx:
                    continue
                enriched.append(ev)
            events = enriched
        except Exception as _enrich_e:
            logger.warning(f"events quality enrichment skipped: {_enrich_e}")

        events = events[:limit]

        return jsonify({
            'success': True,
            'count': len(events),
            'data': events
        })
    except Exception as e:
        logger.error(f"Get events error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


@app.route('/api/events/<event_id>', methods=['GET'])
def get_event(event_id):
    """Get single event"""
    try:
        event = get_db().get_event_by_id(event_id)
        if event:
            return jsonify({'success': True, 'data': event})
    except Exception as e:
        logger.error(f"Get event error: {e}")
    return jsonify({'success': False, 'error': 'Event not found'}), 404


# ============ PREDICTIONS ============

@app.route('/api/predictions/<signal_id>', methods=['GET'])
def get_predictions(signal_id):
    """Get predictions for a signal"""
    try:
        predictions = get_db().get_predictions_for_signal(signal_id)
        return jsonify({
            'success': True,
            'count': len(predictions),
            'data': predictions
        })
    except Exception as e:
        logger.error(f"Get predictions error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


# ============ GEO EVENTS ============

@app.route('/api/geoevents', methods=['GET'])
def get_geo_events():
    """Get geopolitical events from database"""
    try:
        limit = request.args.get('limit', 200, type=int)
        events = get_db().get_geo_events(limit)
        return jsonify({
            'success': True,
            'count': len(events),
            'data': events
        })
    except Exception as e:
        logger.error(f"Get geo events error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


# ============ STOCK DETAIL (popup data) ============

@app.route('/api/stock/<ticker>', methods=['GET'])
def get_stock_detail(ticker):
    """Get full detail for a stock: signal + predictions + live price. Used by popup."""
    ticker = ticker.strip().upper()
    try:
        # Use fresh DB connection (avoids SQLite threading issues)
        from database_schema import TickwaveDB, _execute_query
        local_db = TickwaveDB()
        placeholder = '%s' if os.getenv('DATABASE_URL', '').startswith('postgres') else '?'

        # Best signal for this ticker
        signals = _execute_query(local_db.conn, f"""
            SELECT * FROM signals WHERE ticker = {placeholder} AND status = 'active'
            ORDER BY alpha_score DESC LIMIT 1
        """, (ticker,), fetch=True)
        signal = signals[0] if signals else None

        # Predictions for best signal
        predictions = {}
        if signal:
            preds = _execute_query(local_db.conn, f"""
                SELECT horizon, predicted_return_pct, target_price, confidence,
                       actual_return_pct, hit_target
                FROM predictions WHERE signal_id = {placeholder}
                ORDER BY horizon
            """, (signal['event_id'],), fetch=True)
            for p in preds:
                predictions[p['horizon']] = {
                    'predicted_return_pct': p['predicted_return_pct'],
                    'target_price': p['target_price'],
                    'confidence': p['confidence'],
                    'actual_return_pct': p.get('actual_return_pct'),
                    'hit_target': p.get('hit_target'),
                }
        local_db.close()

        # Live price from yfinance — use fast_info (much faster than info)
        price_data = {}
        try:
            import yfinance as yf
            t = yf.Ticker(f"{ticker}.NS")
            fi = t.fast_info
            price_data = {
                'price': float(fi.get('lastPrice', 0) or fi.get('regularMarketPrice', 0)),
                'change_pct': float(fi.get('regularMarketChangePercent', 0) or 0) if hasattr(fi, 'get') else 0,
                'day_open': float(getattr(fi, 'open', 0) or 0),
                'day_high': float(getattr(fi, 'dayHigh', 0) or 0),
                'day_low': float(getattr(fi, 'dayLow', 0) or 0),
                'prev_close': float(getattr(fi, 'previousClose', 0) or getattr(fi, 'regularMarketPreviousClose', 0) or 0),
                'volume': int(getattr(fi, 'lastVolume', 0) or getattr(fi, 'regularMarketVolume', 0) or 0),
                'market_cap': int(getattr(fi, 'marketCap', 0) or 0),
                'fifty_two_week_high': float(getattr(fi, 'yearHigh', 0) or 0),
                'fifty_two_week_low': float(getattr(fi, 'yearLow', 0) or 0),
            }
            # If fast_info missing data, try history for OHLCV + 52w bounds
            if (not price_data['price'] or not price_data.get('fifty_two_week_high')
                    or not price_data['day_open'] or not price_data['volume']):
                hist = t.history(period='1y')
                if not hist.empty:
                    if not price_data['price']:
                        price_data['price'] = float(hist['Close'].iloc[-1])
                    price_data['fifty_two_week_high'] = price_data['fifty_two_week_high'] or float(hist['Close'].max())
                    price_data['fifty_two_week_low']  = price_data['fifty_two_week_low']  or float(hist['Close'].min())
                    last = hist.iloc[-1]
                    if not price_data['day_open']:  price_data['day_open']  = float(last['Open'])
                    if not price_data['day_high']:  price_data['day_high']  = float(last['High'])
                    if not price_data['day_low']:   price_data['day_low']   = float(last['Low'])
                    if not price_data['volume']:    price_data['volume']    = int(last['Volume'])
                    if len(hist) >= 2 and not price_data.get('change_pct'):
                        prev = float(hist['Close'].iloc[-2])
                        curr = float(hist['Close'].iloc[-1])
                        price_data['change_pct'] = round((curr - prev) / prev * 100, 2) if prev else 0
                        if not price_data['prev_close']: price_data['prev_close'] = prev
        except Exception as e:
            logger.warning(f"Price fetch for {ticker}: {e}")
            # Use entry_price from signal as fallback
            if signal and signal.get('entry_price'):
                price_data = {'price': float(signal['entry_price'])}


        # Company name from universe
        try:
            from stock_universe import STOCK_UNIVERSE
            company = STOCK_UNIVERSE.get(ticker, {}).get('name', ticker)
        except Exception:
            company = ticker

        return jsonify({
            'success': True,
            'data': {
                'ticker': ticker,
                'company': company,
                'price': price_data,
                'signal': signal,
                'predictions': predictions,
            }
        })
    except Exception as e:
        logger.error(f"Stock detail error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ SECTOR HEATMAP ============

@app.route('/api/sectors', methods=['GET'])
@ttl_cache(seconds=120)
def get_sectors():
    """Get sector-level aggregated data for heatmap"""
    try:
        from database_schema import TickwaveDB, _execute_query
        from stock_universe import STOCK_UNIVERSE
        local_db = TickwaveDB()

        # Get all active signals grouped by sector
        signals = _execute_query(local_db.conn, """
            SELECT ticker, alpha_score, sentiment, confidence, entry_price, event_type
            FROM signals WHERE status = 'active'
            ORDER BY alpha_score DESC
        """, fetch=True)
        local_db.close()

        # Build sector data from stock universe + signals
        sectors = {}
        for sig in signals:
            ticker = sig['ticker']
            info = STOCK_UNIVERSE.get(ticker, {})
            sector = info.get('sector', 'OTHER')
            if sector not in sectors:
                sectors[sector] = {
                    'sector': sector, 'stocks': [], 'total_signals': 0,
                    'bullish': 0, 'bearish': 0, 'neutral': 0,
                    'avg_alpha': 0, 'top_stock': None, 'alpha_sum': 0
                }
            s = sectors[sector]
            s['total_signals'] += 1
            s['alpha_sum'] += (sig['alpha_score'] or 0)
            sent = sig['sentiment'] or 'neutral'
            s[sent] = s.get(sent, 0) + 1

            if len(s['stocks']) < 5:  # Top 5 stocks per sector
                s['stocks'].append({
                    'ticker': ticker,
                    'company': info.get('name', ticker),
                    'alpha': round(sig['alpha_score'] or 0, 1),
                    'sentiment': sent,
                    'price': sig['entry_price'] or 0,
                })

        # Compute averages and sentiment score
        result = []
        for sec in sectors.values():
            total = sec['total_signals'] or 1
            sec['avg_alpha'] = round(sec['alpha_sum'] / total, 1)
            sec['sentiment_score'] = round((sec['bullish'] - sec['bearish']) / total * 100, 1)
            sec['top_stock'] = sec['stocks'][0] if sec['stocks'] else None
            del sec['alpha_sum']
            result.append(sec)

        result.sort(key=lambda x: abs(x['sentiment_score']), reverse=True)
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        logger.error(f"Sectors error: {e}")
        return jsonify({'success': True, 'data': []})


@app.route('/api/sectors/<sector>/stocks', methods=['GET'])
def get_sector_stocks(sector):
    """Get all signals for a specific sector"""
    try:
        from database_schema import TickwaveDB, _execute_query
        from stock_universe import STOCK_UNIVERSE
        local_db = TickwaveDB()

        signals = _execute_query(local_db.conn, """
            SELECT * FROM signals WHERE status = 'active'
            ORDER BY alpha_score DESC
        """, fetch=True)
        local_db.close()

        result = []
        for sig in signals:
            info = STOCK_UNIVERSE.get(sig['ticker'], {})
            if info.get('sector', '').upper() == sector.upper():
                result.append({**sig, 'company': info.get('name', sig['ticker']), 'sector': sector})

        return jsonify({'success': True, 'count': len(result), 'data': result})
    except Exception as e:
        logger.error(f"Sector stocks error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


# ============ PORTFOLIO SIMULATOR ============

@app.route('/api/simulator', methods=['GET'])
@throttle('simulator', max_calls=10, window_secs=60)
def portfolio_simulator():
    """Simulate returns from following top alpha signals.
    Shows what would happen if you invested equally in top N signals."""
    try:
        from database_schema import TickwaveDB, _execute_query
        local_db = TickwaveDB()

        min_alpha = request.args.get('min_alpha', 40, type=float)
        top_n = request.args.get('top', 10, type=int)

        # Get signals that have prediction outcomes
        sigs = _execute_query(local_db.conn, """
            SELECT s.ticker, s.alpha_score, s.sentiment, s.entry_price,
                   s.event_type, s.created_at, s.company,
                   p.horizon, p.predicted_return_pct, p.actual_return_pct, p.hit_target
            FROM signals s
            JOIN predictions p ON s.event_id = p.signal_id
            WHERE s.alpha_score >= ? AND p.actual_return_pct IS NOT NULL
            ORDER BY s.alpha_score DESC
        """.replace('?', '%s') if os.getenv('DATABASE_URL', '').startswith('postgres') else """
            SELECT s.ticker, s.alpha_score, s.sentiment, s.entry_price,
                   s.event_type, s.created_at, s.company,
                   p.horizon, p.predicted_return_pct, p.actual_return_pct, p.hit_target
            FROM signals s
            JOIN predictions p ON s.event_id = p.signal_id
            WHERE s.alpha_score >= ? AND p.actual_return_pct IS NOT NULL
            ORDER BY s.alpha_score DESC
        """, (min_alpha,), fetch=True)

        # Also get signals WITHOUT outcomes yet (for "current positions")
        active = _execute_query(local_db.conn, """
            SELECT s.ticker, s.alpha_score, s.sentiment, s.entry_price,
                   s.event_type, s.created_at, s.company,
                   p.horizon, p.predicted_return_pct
            FROM signals s
            JOIN predictions p ON s.event_id = p.signal_id
            WHERE s.alpha_score >= ? AND p.actual_return_pct IS NULL AND p.horizon = '3D'
            ORDER BY s.alpha_score DESC
        """.replace('?', '%s') if os.getenv('DATABASE_URL', '').startswith('postgres') else """
            SELECT s.ticker, s.alpha_score, s.sentiment, s.entry_price,
                   s.event_type, s.created_at, s.company,
                   p.horizon, p.predicted_return_pct
            FROM signals s
            JOIN predictions p ON s.event_id = p.signal_id
            WHERE s.alpha_score >= ? AND p.actual_return_pct IS NULL AND p.horizon = '3D'
            ORDER BY s.alpha_score DESC
        """, (min_alpha,), fetch=True)
        local_db.close()

        # Deduplicate by ticker (keep best alpha)
        seen = set()
        completed = []
        for s in sigs:
            key = f"{s['ticker']}_{s['horizon']}"
            if key not in seen:
                seen.add(key)
                completed.append(s)

        seen2 = set()
        current = []
        for s in active:
            if s['ticker'] not in seen2:
                seen2.add(s['ticker'])
                current.append(s)

        # Calculate portfolio performance
        total_trades = len(completed)
        if total_trades > 0:
            wins = sum(1 for s in completed if s.get('hit_target'))
            total_return = sum(s.get('actual_return_pct', 0) or 0 for s in completed)
            avg_return = total_return / total_trades
            win_rate = wins / total_trades
        else:
            wins = 0
            total_return = 0
            avg_return = 0
            win_rate = 0

        return jsonify({
            'success': True,
            'data': {
                'summary': {
                    'total_trades': total_trades,
                    'wins': wins,
                    'win_rate': round(win_rate * 100, 1),
                    'total_return': round(total_return, 2),
                    'avg_return_per_trade': round(avg_return, 2),
                    'min_alpha_used': min_alpha,
                },
                'completed_trades': completed[:top_n],
                'current_positions': current[:top_n],
            }
        })
    except Exception as e:
        logger.error(f"Simulator error: {e}")
        return jsonify({'success': True, 'data': {'summary': {}, 'completed_trades': [], 'current_positions': []}})


# ============ SIMULATOR EQUITY CURVE ============

@app.route('/api/simulator/equity-curve', methods=['GET'])
@throttle('equity_curve', max_calls=10, window_secs=60)
def simulator_equity_curve():
    """Return time-series data for cumulative PnL chart and drawdown chart."""
    try:
        from database_schema import TickwaveDB, _execute_query
        local_db = TickwaveDB()

        min_alpha = request.args.get('min_alpha', 40, type=float)

        # Fetch all resolved signals ordered by resolution date (created_at + horizon days)
        rows = _execute_query(local_db.conn, """
            SELECT s.ticker, s.alpha_score, s.sentiment, s.created_at,
                   p.horizon, p.predicted_return_pct, p.actual_return_pct, p.hit_target
            FROM signals s
            JOIN predictions p ON s.event_id = p.signal_id
            WHERE s.alpha_score >= ? AND p.actual_return_pct IS NOT NULL
            ORDER BY s.created_at ASC
        """.replace('?', '%s') if os.getenv('DATABASE_URL', '').startswith('postgres') else """
            SELECT s.ticker, s.alpha_score, s.sentiment, s.created_at,
                   p.horizon, p.predicted_return_pct, p.actual_return_pct, p.hit_target
            FROM signals s
            JOIN predictions p ON s.event_id = p.signal_id
            WHERE s.alpha_score >= ? AND p.actual_return_pct IS NOT NULL
            ORDER BY s.created_at ASC
        """, (min_alpha,), fetch=True)
        local_db.close()

        # Only keep 3D horizon for equity curve (consistent horizon)
        trades = [r for r in rows if r.get('horizon') == '3D']

        # Deduplicate: one entry per ticker per day
        seen = set()
        unique = []
        for t in trades:
            key = f"{t['ticker']}_{(t['created_at'] or '')[:10]}"
            if key not in seen:
                seen.add(key)
                unique.append(t)

        # Build cumulative PnL curve
        cumulative = 0.0
        peak = 0.0
        points = []
        for t in unique:
            ret = t.get('actual_return_pct') or 0
            cumulative += ret
            peak = max(peak, cumulative)
            drawdown = cumulative - peak  # Always <= 0
            points.append({
                'date': (t['created_at'] or '')[:10],
                'ticker': t['ticker'],
                'return': round(ret, 2),
                'cumulative': round(cumulative, 2),
                'drawdown': round(drawdown, 2),
                'hit': bool(t.get('hit_target')),
            })

        # Summary stats
        total = len(points)
        wins = sum(1 for p in points if p['hit'])
        max_dd = min((p['drawdown'] for p in points), default=0)
        peak_gain = max((p['cumulative'] for p in points), default=0)

        return jsonify({
            'success': True,
            'data': {
                'points': points,
                'summary': {
                    'total_trades': total,
                    'wins': wins,
                    'win_rate': round(wins / total * 100, 1) if total else 0,
                    'final_return': round(points[-1]['cumulative'], 2) if points else 0,
                    'peak_gain': round(peak_gain, 2),
                    'max_drawdown': round(max_dd, 2),
                }
            }
        })
    except Exception as e:
        logger.error(f"Equity curve error: {e}")
        return jsonify({'success': True, 'data': {'points': [], 'summary': {}}})


# ============ EARNINGS CALENDAR ============

@app.route('/api/earnings', methods=['GET'])
@ttl_cache(seconds=600)
def get_earnings_calendar():
    """Upcoming earnings dates via yfinance.

    Filters:
      - drops past dates (yfinance returns the *last* call when no next is
        scheduled; that's why the calendar appeared "stuck" on the same names
        for days)
      - drops dates further out than `?days_ahead=` (default 45 days) so the
        calendar is actionable, not a quarterly forecast
      - sweeps a broader universe than the old 20-ticker monitored list so
        new names rotate in as their earnings dates approach
    """
    from datetime import date, timedelta
    try:
        days_ahead = max(7, min(120, int(request.args.get('days_ahead', '45'))))
    except (TypeError, ValueError):
        days_ahead = 45
    try:
        limit = max(5, min(60, int(request.args.get('limit', '20'))))
    except (TypeError, ValueError):
        limit = 20

    today = date.today()
    cutoff = today + timedelta(days=days_ahead)

    try:
        import yfinance as yf
        from stock_universe import STOCK_UNIVERSE
        try:
            from config import MONITORED_STOCKS
            monitored = list(MONITORED_STOCKS)
        except Exception:
            monitored = []

        # Broaden the candidate set: monitored stocks first (high signal),
        # then the rest of STOCK_UNIVERSE. Cap the iteration to keep request
        # under 5s — yfinance calendar fetch is ~150-300ms per ticker.
        seen = set()
        candidates = []
        for t in monitored:
            if t and t not in seen:
                candidates.append(t); seen.add(t)
        for t in STOCK_UNIVERSE.keys():
            if t and t not in seen:
                candidates.append(t); seen.add(t)
            if len(candidates) >= 120:
                break

        results = []
        for ticker in candidates:
            try:
                t = yf.Ticker(f"{ticker}.NS")
                cal = t.calendar
                if cal is None:
                    continue
                if hasattr(cal, 'empty') and cal.empty:
                    continue
                # cal can be a dict or DataFrame
                earn_date = None
                if isinstance(cal, dict):
                    val = cal.get('Earnings Date')
                    if isinstance(val, list) and val:
                        earn_date = str(val[0])
                    elif val:
                        earn_date = str(val)
                else:
                    if 'Earnings Date' in cal.columns:
                        earn_date = str(cal['Earnings Date'].iloc[0]) if len(cal) > 0 else None
                    elif 'Earnings Date' in cal.index:
                        v = cal.loc['Earnings Date']
                        earn_date = str(v.iloc[0]) if hasattr(v, 'iloc') else str(v)

                if not earn_date or earn_date in ('None', 'NaT'):
                    continue
                earn_date = earn_date[:10]  # YYYY-MM-DD

                # Hard filter: must be today-or-later AND inside the window.
                # This is the fix for the "same list for days" symptom — past
                # dates were leaking through and pinned to the top of the sort.
                try:
                    ed = date.fromisoformat(earn_date)
                except ValueError:
                    continue
                if ed < today or ed > cutoff:
                    continue

                info = STOCK_UNIVERSE.get(ticker, {})
                results.append({
                    'ticker': ticker,
                    'company': info.get('name', ticker),
                    'sector': info.get('sector', ''),
                    'earnings_date': earn_date,
                    'days_until': (ed - today).days,
                })
                if len(results) >= limit * 2:  # over-fetch then trim post-sort
                    break
            except Exception:
                continue

        results.sort(key=lambda x: x.get('earnings_date', '9999'))
        results = results[:limit]
        return jsonify({
            'success': True,
            'count': len(results),
            'data': results,
            'as_of': today.isoformat(),
            'window_days': days_ahead,
        })
    except Exception as e:
        logger.error(f"Earnings calendar error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


# ============ EVENT TIMELINE ============

@app.route('/api/stock/<ticker>/timeline', methods=['GET'])
def get_stock_timeline(ticker):
    """Get events + signals for a specific stock over time (for timeline view)"""
    ticker = ticker.strip().upper()
    try:
        from database_schema import TickwaveDB, _execute_query
        local_db = TickwaveDB()
        placeholder = '%s' if os.getenv('DATABASE_URL', '').startswith('postgres') else '?'

        # Get all signals for this ticker
        signals = _execute_query(local_db.conn, f"""
            SELECT event_id, event_type, alpha_score, confidence, sentiment,
                   headline, created_at, entry_price
            FROM signals WHERE ticker = {placeholder}
            ORDER BY created_at DESC LIMIT 20
        """, (ticker,), fetch=True)

        # Get predictions for these signals
        for sig in signals:
            preds = _execute_query(local_db.conn, f"""
                SELECT horizon, predicted_return_pct, actual_return_pct, hit_target
                FROM predictions WHERE signal_id = {placeholder}
            """, (sig['event_id'],), fetch=True)
            sig['predictions'] = {p['horizon']: {
                'predicted': p['predicted_return_pct'],
                'actual': p.get('actual_return_pct'),
                'hit': p.get('hit_target')
            } for p in preds}

        local_db.close()
        return jsonify({'success': True, 'ticker': ticker, 'data': signals})
    except Exception as e:
        logger.error(f"Timeline error: {e}")
        return jsonify({'success': True, 'data': []})


# ============ STOCK CHART ============

@app.route('/api/stock/<ticker>/chart', methods=['GET'])
def get_stock_chart(ticker):
    """Get 30-day price history for chart rendering"""
    ticker = ticker.strip().upper()
    period = request.args.get('period', '30d')
    try:
        import yfinance as yf
        t = yf.Ticker(f"{ticker}.NS")
        hist = t.history(period=period)
        if hist.empty:
            return jsonify({'success': False, 'error': 'No data'}), 404

        points = []
        for date, row in hist.iterrows():
            points.append({
                'date': date.strftime('%Y-%m-%d'),
                'close': round(float(row['Close']), 2),
                'high': round(float(row['High']), 2),
                'low': round(float(row['Low']), 2),
                'volume': int(row['Volume']) if 'Volume' in row else 0
            })

        return jsonify({
            'success': True,
            'ticker': ticker,
            'period': period,
            'data': points
        })
    except Exception as e:
        logger.error(f"Chart error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ STOCK SEARCH ============

@app.route('/api/search', methods=['GET'])
@ttl_cache(seconds=300)
def search_stock():
    """Search stocks. Returns instant matches from universe + live price for exact match."""
    query = (request.args.get('q', '') or '').strip()
    if not query or len(query) < 2:
        return jsonify({'success': False, 'error': 'Query too short'}), 400
    try:
        from stock_universe import search_stocks, STOCK_UNIVERSE

        # Instant search from local universe (no API call)
        matches = search_stocks(query)

        # If exact ticker match, also fetch live price from yfinance
        exact = query.upper()
        if exact in STOCK_UNIVERSE:
            try:
                import yfinance as yf
                symbol = f"{exact}.NS"
                ticker = yf.Ticker(symbol)
                info = ticker.info or {}
                price = info.get('currentPrice') or info.get('regularMarketPrice', 0)
                change = info.get('regularMarketChangePercent', 0)
                for m in matches:
                    if m['ticker'] == exact:
                        m['price'] = float(price) if price else 0
                        m['change_pct'] = float(change) if change else 0
                        m['industry'] = info.get('industry', '')
                        m['market_cap'] = info.get('marketCap', 0)
                        break
            except Exception:
                pass  # Price fetch failed, return matches without price

        # If no matches in universe, try yfinance directly (for unlisted/new stocks)
        if not matches:
            try:
                import yfinance as yf
                for suffix, exchange in [('.NS', 'NSE'), ('.BO', 'BSE')]:
                    symbol = f"{exact}{suffix}"
                    ticker_obj = yf.Ticker(symbol)
                    info = ticker_obj.info or {}
                    name = info.get('longName') or info.get('shortName', '')
                    price = info.get('currentPrice') or info.get('regularMarketPrice', 0)
                    if name or price:
                        matches.append({
                            'ticker': exact, 'company': name,
                            'sector': info.get('sector', ''), 'exchange': exchange,
                            'price': float(price) if price else 0,
                            'change_pct': float(info.get('regularMarketChangePercent', 0)),
                            'industry': info.get('industry', ''),
                            'market_cap': info.get('marketCap', 0),
                        })
                        break
            except Exception:
                pass

        if matches:
            return jsonify({'success': True, 'data': matches})
        return jsonify({'success': False, 'error': f'No stock found for {query}'}), 404
    except Exception as e:
        logger.error(f"Search error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/stocks/list', methods=['GET'])
@ttl_cache(seconds=900)
def get_stock_list():
    """Get full stock universe for frontend autocomplete"""
    try:
        from stock_universe import STOCK_UNIVERSE
        stocks = [{'ticker': k, 'company': v['name'], 'sector': v['sector']}
                  for k, v in STOCK_UNIVERSE.items()]
        return jsonify({'success': True, 'count': len(stocks), 'data': stocks})
    except Exception as e:
        logger.error(f"Stock list error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


# ============ PREDICTION ACCURACY ============

@app.route('/api/accuracy', methods=['GET'])
def get_accuracy():
    """Get prediction hit rates by horizon"""
    try:
        rows = get_db().get_prediction_accuracy()
        data = {}
        for row in rows:
            total = row.get('total', 0)
            hits = row.get('hits', 0)
            data[row['horizon']] = {
                'total': total,
                'hits': hits,
                'hit_rate': round(hits / max(total, 1), 3),
                'avg_actual_return': row.get('avg_actual_return', 0),
                'avg_predicted_return': row.get('avg_predicted_return', 0)
            }
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        logger.error(f"Get accuracy error: {e}")
        return jsonify({'success': True, 'data': {}})


@app.route('/api/predictions/track', methods=['GET'])
def get_prediction_outcomes():
    """Get recent prediction outcomes"""
    try:
        limit = request.args.get('limit', 20, type=int)
        outcomes = get_db().get_recent_prediction_outcomes(limit)
        return jsonify({'success': True, 'count': len(outcomes), 'data': outcomes})
    except Exception as e:
        logger.error(f"Get prediction outcomes error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


@app.route('/api/predictions/check', methods=['POST'])
def trigger_prediction_check():
    """Manually trigger prediction verification"""
    import threading
    thread = threading.Thread(target=run_prediction_tracker_job)
    thread.daemon = True
    thread.start()
    return jsonify({'success': True, 'message': 'Prediction check started'})


# ============ ALERTS (per-user) ============

@app.route('/api/alerts', methods=['GET'])
@optional_auth
def get_alerts():
    """Get user alerts"""
    try:
        limit = request.args.get('limit', 20, type=int)
        alerts = get_db().get_alerts(limit, user_id=g.user_id)
        return jsonify({'success': True, 'count': len(alerts), 'data': alerts})
    except Exception as e:
        logger.error(f"Get alerts error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


@app.route('/api/alerts', methods=['POST'])
@optional_auth
def create_alert():
    """Create new alert. Free tier capped at 10 alerts."""
    try:
        if g.user_id and g.user_id != 'legacy':
            try:
                from backend.freemium import check_alerts_limit
                err = check_alerts_limit(get_db(), g.user_id)
                if err:
                    return jsonify(err), 402
            except Exception as _gate_exc:
                logger.debug(f"freemium alert gate skipped: {_gate_exc}")

        data = request.get_json()
        success = get_db().create_alert(
            ticker=data.get('ticker'),
            alert_type=data.get('alert_type', 'price'),
            condition=data.get('condition', 'above'),
            threshold=data.get('threshold', 0),
            user_id=g.user_id
        )
        if success:
            return jsonify({'success': True, 'message': 'Alert created'}), 201
        return jsonify({'success': False, 'error': 'Failed to create alert'}), 400
    except Exception as e:
        logger.error(f"Create alert error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/billing/checkout', methods=['POST'])
@optional_auth
def billing_checkout():
    """Razorpay checkout stub. Returns a structured 'coming_soon' so the
    pricing page can render a polite inline message. Real integration
    lands once Razorpay KYC (PAN/GST/bank) completes.

    Logs the intent so we can email these prospects when payments go live.
    """
    body = request.get_json(silent=True) or {}
    plan = (body.get('plan') or '').strip().lower()
    if plan not in ('starter_monthly', 'starter_annual',
                    'pro_monthly', 'pro_annual'):
        return jsonify({'success': False, 'error': 'unknown_plan'}), 400
    try:
        cur = get_db().conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS checkout_intents ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id TEXT, plan TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        uid = str(g.user_id) if g.user_id and g.user_id != 'legacy' else None
        cur.execute(
            "INSERT INTO checkout_intents (user_id, plan) VALUES (?, ?)",
            (uid, plan),
        )
        get_db().conn.commit()
    except Exception:
        pass
    return jsonify({
        'success': True,
        'status': 'coming_soon',
        'message': 'Razorpay launches next week. We logged your interest — email support@alphaevent.in to be notified.',
    })


@app.route('/api/me/tier', methods=['GET'])
@optional_auth
def me_tier():
    """Return the current user's subscription tier + concrete tier limits
    + feature flags so the frontend can render upgrade prompts and
    remaining-counts without each page guessing the numbers.

    Tier ladder: free | starter | pro. See backend/freemium.py.
    """
    from backend.freemium import get_user_features
    db = get_db()
    snapshot = get_user_features(db, g.user_id if g.user_id and g.user_id != 'legacy' else None)
    snapshot['success'] = True
    return jsonify(snapshot)


# ============ WATCHLIST (per-user) ============

@app.route('/api/watchlist', methods=['GET'])
@optional_auth
def get_watchlist():
    """Get user watchlist"""
    try:
        limit = request.args.get('limit', 50, type=int)
        watchlist = get_db().get_watchlist(limit, user_id=g.user_id)
        return jsonify({'success': True, 'count': len(watchlist), 'data': watchlist})
    except Exception as e:
        logger.error(f"Get watchlist error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


@app.route('/api/watchlist', methods=['POST'])
@optional_auth
def add_to_watchlist():
    """Add stock to watchlist. Unlimited for all tiers as of 2026-05-18."""
    try:
        data = request.get_json()
        success = get_db().add_to_watchlist(
            ticker=data.get('ticker'), notes=data.get('notes', ''),
            user_id=g.user_id
        )
        if success:
            return jsonify({'success': True, 'message': 'Added to watchlist'}), 201
        return jsonify({'success': False, 'error': 'Failed to add'}), 400
    except Exception as e:
        logger.error(f"Add to watchlist error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/watchlist', methods=['DELETE'])
@optional_auth
def remove_from_watchlist():
    """Remove stock from watchlist"""
    try:
        data = request.get_json()
        success = get_db().remove_from_watchlist(data.get('ticker'), user_id=g.user_id)
        if success:
            return jsonify({'success': True, 'message': 'Removed from watchlist'})
        return jsonify({'success': False, 'error': 'Failed to remove'}), 400
    except Exception as e:
        logger.error(f"Remove from watchlist error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400


# ============ NOTIFICATIONS CONFIG (per-user) ============

@app.route('/api/notifications/config', methods=['GET'])
@optional_auth
def get_notification_config():
    """Get notification configuration"""
    try:
        config = get_db().get_notification_config(user_id=g.user_id)
        if config:
            safe_config = dict(config)
            if safe_config.get('telegram_bot_token'):
                t = safe_config['telegram_bot_token']
                safe_config['telegram_bot_token'] = t[:8] + '...' if len(t) > 8 else '***'
            if safe_config.get('discord_webhook_url'):
                u = safe_config['discord_webhook_url']
                safe_config['discord_webhook_url'] = u[:40] + '...' if len(u) > 40 else '***'
            return jsonify({'success': True, 'data': safe_config})
        return jsonify({'success': True, 'data': None})
    except Exception as e:
        logger.error(f"Get notification config error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/notifications/config', methods=['POST'])
@optional_auth
def save_notification_config():
    """Save notification configuration"""
    try:
        data = request.get_json()
        success = get_db().save_notification_config(data, user_id=g.user_id)
        if success:
            return jsonify({'success': True, 'message': 'Config saved'})
        return jsonify({'success': False, 'error': 'Failed to save'}), 400
    except Exception as e:
        logger.error(f"Save notification config error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/notifications/test', methods=['POST'])
@optional_auth
def test_notification():
    """Send a test notification"""
    try:
        result = send_test_notification(get_db(), user_id=g.user_id)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Test notification error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ SCRAPER STATUS ============

@app.route('/api/scraper/status', methods=['GET'])
def get_scraper_status():
    """Get scraper run status"""
    try:
        db_status = get_db().get_scraper_status()
        return jsonify({
            'success': True,
            'data': {
                **scraper_status,
                **db_status,
                'interval_minutes': int(os.getenv('SCRAPER_INTERVAL', '10'))
            }
        })
    except Exception as e:
        logger.error(f"Scraper status error: {e}")
        return jsonify({'success': True, 'data': scraper_status})


@app.route('/api/scraper/run', methods=['POST'])
def trigger_scraper():
    """Manually trigger a scraper run"""
    if scraper_status['running']:
        return jsonify({'success': False, 'error': 'Scraper already running'}), 409

    import threading
    thread = threading.Thread(target=run_scraper_job)
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'message': 'Scraper started'})


# ============ MARKET INDICES ============

_indices_cache = {'data': None, 'time': 0}

@app.route('/api/sources/credibility', methods=['GET'])
@ttl_cache(seconds=3600)
def get_sources_credibility():
    """Public list of news outlets ranked by historical hit-rate.

    Addendum 2026-05-18: surfaces source_credibility rows so the frontend
    can show "62%" next to each source pill in the per-card "N sources"
    dropdown. Cached 1h since the backfill runs nightly.
    """
    try:
        limit = max(5, min(200, int(request.args.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50
    out = []
    try:
        cur = get_db().conn.cursor()
        cur.execute(
            "SELECT outlet, hit_rate, sample_size, credibility_score, last_computed_at "
            "  FROM source_credibility "
            " WHERE sample_size >= 5 "
            " ORDER BY credibility_score DESC, sample_size DESC "
            " LIMIT ?",
            (limit,),
        )
        for row in cur.fetchall() or []:
            if isinstance(row, dict):
                out.append({
                    'outlet':            row.get('outlet'),
                    'hit_rate':          float(row.get('hit_rate') or 0),
                    'sample_size':       int(row.get('sample_size') or 0),
                    'credibility_score': float(row.get('credibility_score') or 0),
                    'last_computed_at':  str(row.get('last_computed_at') or ''),
                })
            else:
                out.append({
                    'outlet':            row[0],
                    'hit_rate':          float(row[1] or 0),
                    'sample_size':       int(row[2] or 0),
                    'credibility_score': float(row[3] or 0),
                    'last_computed_at':  str(row[4] or ''),
                })
    except Exception as e:
        logger.debug(f"sources credibility query: {e}")
    return jsonify({'success': True, 'count': len(out), 'data': out})


@app.route('/api/market', methods=['GET'])
@ttl_cache(seconds=60)
def get_market_summary():
    """Lightweight market snapshot for the homepage hero tagline + KPIs.

    Returns vix, regime, summary, and a few headline metrics in the shape
    the index.html 'Today' panel consumes. Composed from:
      - /api/market-indices cache for India VIX
      - DB stats for fresh-signal count / hit rates
      - Simple regime heuristic from VIX band
    """
    import time as _time
    out = {'success': True, 'data': {
        'vix': None, 'regime': None, 'summary': '',
        'fresh_signals_24h': 0, 'nifty_change_pct': None,
    }}
    # 1. VIX + Nifty from the cached market-indices payload
    try:
        if _indices_cache.get('data'):
            for idx in _indices_cache['data'] or []:
                short = (idx.get('short') or '').upper()
                if short == 'INDIA VIX':
                    out['data']['vix'] = idx.get('price') or idx.get('last') or idx.get('value')
                elif short == 'NIFTY 50':
                    out['data']['nifty_change_pct'] = idx.get('change_pct') or idx.get('pct_change')
    except Exception as e:
        logger.debug(f"market summary indices read failed: {e}")

    # 2. Regime — derive from VIX band so we always have something to show
    try:
        vix = out['data']['vix']
        if vix is None:
            out['data']['regime'] = 'Unknown'
        elif vix < 13:    out['data']['regime'] = 'Calm'
        elif vix < 17:    out['data']['regime'] = 'Normal'
        elif vix < 22:    out['data']['regime'] = 'Elevated'
        else:             out['data']['regime'] = 'Stressed'
    except Exception:
        out['data']['regime'] = 'Unknown'

    # 3. Fresh-signal count for the summary tagline
    try:
        cur = get_db().conn.cursor()
        is_pg = getattr(get_db(), 'is_postgres', False)
        cutoff_clause = ("created_at >= NOW() - INTERVAL '24 hours'" if is_pg
                         else "created_at >= datetime('now', '-24 hours')")
        cur.execute(f"SELECT COUNT(*) FROM signals WHERE status='active' AND {cutoff_clause}")
        row = cur.fetchone()
        fresh = (row[0] if not isinstance(row, dict) else row.get('count(*)') or 0) if row else 0
        out['data']['fresh_signals_24h'] = int(fresh or 0)
    except Exception as e:
        logger.debug(f"market summary fresh-signal count failed: {e}")

    # 4. Build the tagline. Keep it short — fills hero subtitle.
    bits = []
    if out['data']['fresh_signals_24h']:
        bits.append(f"{out['data']['fresh_signals_24h']} fresh signal{'s' if out['data']['fresh_signals_24h'] != 1 else ''} in last 24h")
    if out['data']['nifty_change_pct'] is not None:
        sign = '+' if out['data']['nifty_change_pct'] >= 0 else ''
        bits.append(f"Nifty {sign}{out['data']['nifty_change_pct']:.2f}%")
    if out['data']['regime'] not in (None, 'Unknown'):
        bits.append(f"regime: {out['data']['regime'].lower()}")
    out['data']['summary'] = ' · '.join(bits) if bits else 'Live market data — refresh in a moment.'
    return jsonify(out)


@app.route('/api/market-indices', methods=['GET'])
@ttl_cache(seconds=60)
def get_market_indices():
    """Get live Nifty 50, Sensex, and other key Indian market indices via yfinance."""
    import time as _time
    cache_ttl = 300  # 5 minutes

    if _indices_cache['data'] and (_time.time() - _indices_cache['time']) < cache_ttl:
        return jsonify({'success': True, 'data': _indices_cache['data'], 'cached': True})

    try:
        import yfinance as yf
        # Expanded coverage — broad indexes + sector indexes + macro pair.
        # 'group' is used by the UI to split macro vs sectors.
        indices_meta = [
            {'symbol': '^NSEI',     'short': 'NIFTY 50',        'group': 'broad'},
            {'symbol': '^BSESN',    'short': 'SENSEX',          'group': 'broad'},
            {'symbol': '^NSEBANK',  'short': 'BANK NIFTY',      'group': 'broad'},
            {'symbol': '^CNXIT',    'short': 'NIFTY IT',        'group': 'sector'},
            {'symbol': '^CNXAUTO',  'short': 'NIFTY AUTO',      'group': 'sector'},
            {'symbol': '^CNXPHARMA','short': 'NIFTY PHARMA',    'group': 'sector'},
            {'symbol': '^CNXMETAL', 'short': 'NIFTY METAL',     'group': 'sector'},
            {'symbol': '^CNXENERGY','short': 'NIFTY ENERGY',    'group': 'sector'},
            {'symbol': '^CNXFMCG',  'short': 'NIFTY FMCG',      'group': 'sector'},
            {'symbol': '^CNXREALTY','short': 'NIFTY REALTY',    'group': 'sector'},
            {'symbol': '^CNXMEDIA', 'short': 'NIFTY MEDIA',     'group': 'sector'},
            {'symbol': '^CNXPSUBANK','short':'NIFTY PSU BANK',  'group': 'sector'},
            {'symbol': '^CNXFIN',   'short': 'NIFTY FIN SERV',  'group': 'sector'},
            {'symbol': 'NIFTY_MIDCAP_100.NS', 'short': 'NIFTY MIDCAP 100', 'group': 'broad'},
            {'symbol': '^CNXSC',    'short': 'NIFTY SMALLCAP 100','group':'broad'},
            {'symbol': '^NSMIDCP',  'short': 'NIFTY MIDCAP 50', 'group': 'broad'},
            {'symbol': '^CNX100',   'short': 'NIFTY 100',       'group': 'broad'},
            {'symbol': '^INDIAVIX', 'short': 'INDIA VIX',       'group': 'macro'},
            {'symbol': 'USDINR=X',  'short': 'USD/INR',         'group': 'macro'},
        ]

        result = []
        for meta in indices_meta:
            try:
                t = yf.Ticker(meta['symbol'])
                fi = t.fast_info
                price = float(fi.last_price or 0)
                prev  = float(fi.previous_close or price)
                chg   = price - prev
                chg_pct = (chg / prev * 100) if prev else 0
                result.append({
                    'symbol': meta['symbol'],
                    'short':  meta['short'],
                    'group':  meta.get('group', 'sector'),
                    'price':  round(price, 2),
                    'change': round(chg, 2),
                    'change_pct': round(chg_pct, 2),
                })
            except Exception as e:
                logger.warning(f"Indices fetch failed for {meta['symbol']}: {e}")
                result.append({'symbol': meta['symbol'], 'short': meta['short'],
                               'group': meta.get('group', 'sector'),
                               'price': 0, 'change': 0, 'change_pct': 0})

        _indices_cache['data'] = result
        _indices_cache['time'] = _time.time()
        return jsonify({'success': True, 'data': result, 'cached': False})
    except Exception as e:
        logger.error(f"Market indices error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


_index_detail_cache = {}

@app.route('/api/market/index/<path:symbol>', methods=['GET'])
def get_index_detail(symbol):
    """Detail popup for an index: 5d sparkline + 52w range + open/high/low + day range."""
    import time as _time
    key = f"idx:{symbol}"
    rec = _index_detail_cache.get(key)
    if rec and (_time.time() - rec['t']) < 120:
        return jsonify({'success': True, 'data': rec['v'], 'cached': True})

    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        fi = t.fast_info
        hist = t.history(period="5d", interval="15m")
        sparkline = []
        if hist is not None and not hist.empty:
            sparkline = [
                {'t': str(idx), 'close': float(c)}
                for idx, c in zip(hist.index[-60:], hist['Close'].iloc[-60:])
                if c == c  # filter NaN
            ]
        # 52w range from a daily pull (fast_info exposes it sometimes)
        try:
            hist52 = t.history(period="1y", interval="1d")
            high52 = float(hist52['High'].max()) if hist52 is not None and not hist52.empty else None
            low52 = float(hist52['Low'].min()) if hist52 is not None and not hist52.empty else None
        except Exception:
            high52, low52 = None, None
        out = {
            'symbol': symbol,
            'price': float(getattr(fi, 'last_price', 0) or 0),
            'prev_close': float(getattr(fi, 'previous_close', 0) or 0),
            'day_high': float(getattr(fi, 'day_high', 0) or 0),
            'day_low': float(getattr(fi, 'day_low', 0) or 0),
            'open': float(getattr(fi, 'open', 0) or 0),
            'high_52w': high52,
            'low_52w': low52,
            'sparkline': sparkline,
        }
        _index_detail_cache[key] = {'t': _time.time(), 'v': out}
        return jsonify({'success': True, 'data': out, 'cached': False})
    except Exception as e:
        logger.warning(f"Index detail failed for {symbol}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 200


# ============ ANALYTICS (Jane Street-grade edge analysis) ============

@app.route('/api/analytics', methods=['GET'])
@ttl_cache(seconds=120)
def get_analytics():
    """
    Comprehensive analytics — exposes the system's actual statistical edge.
    Returns hit rates by alpha bucket, event type, and horizon; EV per signal;
    IC estimate; sector concentration; and a signal leaderboard.
    """
    try:
        from database_schema import TickwaveDB, _execute_query
        local_db = TickwaveDB()
        is_pg = os.getenv('DATABASE_URL', '').startswith('postgres')
        ph = '%s' if is_pg else '?'

        # --- 1. Predictions with outcomes (resolved) ---
        resolved = _execute_query(local_db.conn, """
            SELECT s.ticker, s.alpha_score, s.sentiment, s.event_type,
                   s.confidence, s.entry_price,
                   p.horizon, p.predicted_return_pct,
                   p.actual_return_pct, p.hit_target
            FROM signals s
            JOIN predictions p ON s.event_id = p.signal_id
            WHERE p.actual_return_pct IS NOT NULL
            ORDER BY s.alpha_score DESC
        """, fetch=True)

        # --- 2. All active signals (for EV and concentration) ---
        active_raw = _execute_query(local_db.conn, """
            SELECT ticker, alpha_score, sentiment, event_type, confidence, entry_price
            FROM signals WHERE status = 'active'
            ORDER BY alpha_score DESC
        """, fetch=True)
        local_db.close()

        # Enrich active signals with sector from stock universe
        try:
            from stock_universe import STOCK_UNIVERSE
            active = []
            for sig in active_raw:
                info = STOCK_UNIVERSE.get(sig['ticker'], {})
                active.append({**sig, 'sector': info.get('sector', 'OTHER')})
        except Exception:
            active = [{**sig, 'sector': 'OTHER'} for sig in active_raw]

        total_resolved = len(resolved)

        # --- Alpha bucket analysis ---
        buckets = {
            '0-50':  {'label': '< 50',  'hits': 0, 'total': 0, 'returns': []},
            '50-65': {'label': '50-65', 'hits': 0, 'total': 0, 'returns': []},
            '65-80': {'label': '65-80', 'hits': 0, 'total': 0, 'returns': []},
            '80+':   {'label': '> 80',  'hits': 0, 'total': 0, 'returns': []},
        }
        for row in resolved:
            a = row.get('alpha_score') or 0
            bk = '80+' if a >= 80 else '65-80' if a >= 65 else '50-65' if a >= 50 else '0-50'
            b = buckets[bk]
            b['total'] += 1
            if row.get('hit_target'):
                b['hits'] += 1
            if row.get('actual_return_pct') is not None:
                b['returns'].append(float(row['actual_return_pct']))

        bucket_result = {}
        for k, b in buckets.items():
            t = b['total'] or 1
            returns = b['returns']
            hit_rate = b['hits'] / t
            avg_ret = sum(returns) / len(returns) if returns else 0
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r <= 0]
            avg_win = sum(wins) / len(wins) if wins else 0
            avg_loss = sum(losses) / len(losses) if losses else 0
            ev = hit_rate * avg_win + (1 - hit_rate) * avg_loss
            bucket_result[k] = {
                'label': b['label'],
                'total': b['total'],
                'hits': b['hits'],
                'hit_rate': round(hit_rate, 3),
                'avg_return': round(avg_ret, 2),
                'avg_win': round(avg_win, 2),
                'avg_loss': round(avg_loss, 2),
                'ev': round(ev, 2),
            }

        # --- Event type analysis ---
        ev_types = {}
        for row in resolved:
            et = row.get('event_type') or 'unknown'
            if et not in ev_types:
                ev_types[et] = {'hits': 0, 'total': 0, 'returns': []}
            ev_types[et]['total'] += 1
            if row.get('hit_target'):
                ev_types[et]['hits'] += 1
            if row.get('actual_return_pct') is not None:
                ev_types[et]['returns'].append(float(row['actual_return_pct']))

        event_type_result = {}
        for et, d in ev_types.items():
            t = d['total'] or 1
            returns = d['returns']
            hit_rate = d['hits'] / t
            avg_ret = sum(returns) / len(returns) if returns else 0
            event_type_result[et] = {
                'total': d['total'],
                'hits': d['hits'],
                'hit_rate': round(hit_rate, 3),
                'avg_return': round(avg_ret, 2),
            }

        # --- Horizon analysis ---
        horizons = {}
        for row in resolved:
            h = row.get('horizon') or 'unknown'
            if h not in horizons:
                horizons[h] = {'hits': 0, 'total': 0, 'sum_pred': 0, 'sum_actual': 0}
            horizons[h]['total'] += 1
            if row.get('hit_target'):
                horizons[h]['hits'] += 1
            horizons[h]['sum_pred'] += float(row.get('predicted_return_pct') or 0)
            horizons[h]['sum_actual'] += float(row.get('actual_return_pct') or 0)

        horizon_result = {}
        for h, d in horizons.items():
            t = d['total'] or 1
            horizon_result[h] = {
                'total': d['total'],
                'hits': d['hits'],
                'hit_rate': round(d['hits'] / t, 3),
                'avg_predicted': round(d['sum_pred'] / t, 2),
                'avg_actual': round(d['sum_actual'] / t, 2),
            }

        # --- IC estimate (rank correlation of alpha vs actual return) ---
        ic_estimate = None
        if len(resolved) >= 10:
            try:
                pairs = [(r.get('alpha_score') or 0, float(r.get('actual_return_pct') or 0))
                         for r in resolved if r.get('actual_return_pct') is not None]
                if len(pairs) >= 10:
                    # Spearman rank correlation (manual)
                    n = len(pairs)
                    alpha_ranks = sorted(range(n), key=lambda i: pairs[i][0])
                    ret_ranks   = sorted(range(n), key=lambda i: pairs[i][1])
                    alpha_r = [0] * n
                    ret_r   = [0] * n
                    for rank, idx in enumerate(alpha_ranks):
                        alpha_r[idx] = rank
                    for rank, idx in enumerate(ret_ranks):
                        ret_r[idx] = rank
                    d2 = sum((alpha_r[i] - ret_r[i]) ** 2 for i in range(n))
                    ic = 1 - (6 * d2) / (n * (n**2 - 1))
                    ic_estimate = round(ic, 3)
            except Exception:
                pass

        # --- Sector concentration (active signals) ---
        sector_counts = {}
        for sig in active:
            sec = sig.get('sector') or 'OTHER'
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
        total_active = len(active) or 1
        concentration = [
            {'sector': k, 'count': v, 'pct': round(v / total_active * 100, 1)}
            for k, v in sorted(sector_counts.items(), key=lambda x: -x[1])
        ]
        top_sector_pct = concentration[0]['pct'] if concentration else 0
        concentration_risk = 'HIGH' if top_sector_pct > 50 else 'MEDIUM' if top_sector_pct > 30 else 'LOW'

        # --- Portfolio metrics (all resolved, 3D horizon) ---
        # Key insight: for a long-short model, PnL = actual_return × sign(predicted).
        # Bearish call + bearish move = WIN (you'd have shorted and profited).
        # The old code counted raw `actual > 0` as a win, which conflates the
        # market's direction with the model's edge.
        resolved_3d = [r for r in resolved if r.get('horizon') == '3D']
        if resolved_3d:
            returns_3d = [float(r.get('actual_return_pct') or 0) for r in resolved_3d]
            predicted_3d = [float(r.get('predicted_return_pct') or 0) for r in resolved_3d]
            # Signal-adjusted PnL: what you'd earn following the model's direction
            pnl_3d = [actual * (1 if pred >= 0 else -1) for actual, pred in zip(returns_3d, predicted_3d)]
            hits_3d = sum(1 for r in resolved_3d if r.get('hit_target'))
            avg_ret_3d = sum(returns_3d) / len(returns_3d)        # market-raw avg
            avg_pnl_3d = sum(pnl_3d) / len(pnl_3d)                # signal-adjusted avg
            wins_3d = [p for p in pnl_3d if p > 0]
            losses_3d = [p for p in pnl_3d if p <= 0]
            avg_win_3d = sum(wins_3d) / len(wins_3d) if wins_3d else 0
            avg_loss_3d = abs(sum(losses_3d) / len(losses_3d)) if losses_3d else 0.01
            profit_factor = (avg_win_3d * len(wins_3d)) / max(avg_loss_3d * len(losses_3d), 0.01)
            variance = sum((p - avg_pnl_3d) ** 2 for p in pnl_3d) / len(pnl_3d)
            std_dev = variance ** 0.5
            # Annualized Sharpe on signal-adjusted returns
            sharpe = (avg_pnl_3d / std_dev * (252 ** 0.5 / 3)) if std_dev > 0 else 0
        else:
            hits_3d = 0; avg_ret_3d = 0; avg_pnl_3d = 0
            profit_factor = 0; sharpe = 0
            avg_win_3d = 0; avg_loss_3d = 0; wins_3d = []; losses_3d = []

        # --- Signal leaderboard (top resolved signals by actual return) ---
        leaderboard = sorted(
            [r for r in resolved if r.get('actual_return_pct') is not None and r.get('horizon') == '3D'],
            key=lambda x: float(x.get('actual_return_pct') or 0), reverse=True
        )[:10]

        return jsonify({
            'success': True,
            'data': {
                'summary': {
                    'total_resolved': total_resolved,
                    'ic_estimate': ic_estimate,
                    'concentration_risk': concentration_risk,
                    'top_sector_pct': top_sector_pct,
                    'active_signals': len(active),
                },
                'portfolio_3d': {
                    'total': len(resolved_3d),
                    'wins': hits_3d,
                    'hit_rate': round(hits_3d / max(len(resolved_3d), 1), 3),
                    'avg_return': round(avg_ret_3d, 2),                # raw market return
                    'avg_signal_pnl': round(avg_pnl_3d, 2),            # PnL if you followed the model
                    'signal_wins': len(wins_3d),
                    'signal_losses': len(losses_3d),
                    'avg_win': round(avg_win_3d, 2),
                    'avg_loss': round(avg_loss_3d, 2),
                    'profit_factor': round(profit_factor, 2),
                    'annualized_sharpe': round(sharpe, 2),
                },
                'alpha_buckets': bucket_result,
                'event_types': event_type_result,
                'horizons': horizon_result,
                'concentration': concentration[:8],
                'leaderboard': [
                    {
                        'ticker': r.get('ticker'),
                        'event_type': r.get('event_type'),
                        'alpha_score': round(r.get('alpha_score') or 0, 1),
                        'predicted': round(float(r.get('predicted_return_pct') or 0), 2),
                        'actual': round(float(r.get('actual_return_pct') or 0), 2),
                        'hit': bool(r.get('hit_target')),
                    }
                    for r in leaderboard
                ],
            }
        })
    except Exception as e:
        logger.error(f"Analytics error: {e}", exc_info=True)
        return jsonify({'success': True, 'data': {
            'summary': {'total_resolved': 0, 'ic_estimate': None, 'concentration_risk': 'LOW',
                        'top_sector_pct': 0, 'active_signals': 0},
            'portfolio_3d': {}, 'alpha_buckets': {}, 'event_types': {},
            'horizons': {}, 'concentration': [], 'leaderboard': []
        }})


# ============ QUANT PORTFOLIO ============

@app.route('/api/portfolio/longshort', methods=['GET'])
def portfolio_longshort():
    """
    Run full quant pipeline on active signals.
    Returns ranked universe + long-short portfolio with volatility-adjusted sizing.
    """
    try:
        import sys, os as _os
        sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', 'scraper'))
        from quant_layer import run_quant_pipeline
        from database_schema import TickwaveDB, _execute_query

        local_db = TickwaveDB()
        min_alpha = request.args.get('min_alpha', 30, type=float)

        # Fetch active signals with predictions
        rows = _execute_query(local_db.conn, """
            SELECT s.ticker, s.alpha_score, s.sentiment, s.event_type,
                   s.confidence, s.regime, s.entry_price, s.created_at, s.company,
                   p.predicted_return_pct, p.horizon
            FROM signals s
            LEFT JOIN predictions p ON s.event_id = p.signal_id AND p.horizon = '3D'
            WHERE s.status = 'active' AND s.alpha_score >= ?
            ORDER BY s.alpha_score DESC
        """.replace('?', '%s') if os.getenv('DATABASE_URL', '').startswith('postgres') else """
            SELECT s.ticker, s.alpha_score, s.sentiment, s.event_type,
                   s.confidence, s.regime, s.entry_price, s.created_at, s.company,
                   p.predicted_return_pct, p.horizon
            FROM signals s
            LEFT JOIN predictions p ON s.event_id = p.signal_id AND p.horizon = '3D'
            WHERE s.status = 'active' AND s.alpha_score >= ?
            ORDER BY s.alpha_score DESC
        """, (min_alpha,), fetch=True)
        local_db.close()

        # Deduplicate by ticker (keep best alpha)
        seen = {}
        for r in rows:
            t = r['ticker']
            if t not in seen or (r['alpha_score'] or 0) > (seen[t]['alpha_score'] or 0):
                seen[t] = dict(r)
        signals = list(seen.values())

        # Enrich with sector from STOCK_UNIVERSE
        try:
            from stock_universe import STOCK_UNIVERSE
            for sig in signals:
                info = STOCK_UNIVERSE.get(sig['ticker'], {})
                sig['sector'] = info.get('sector', 'OTHER')
                sig['market_cap'] = info.get('market_cap', 'large')
        except Exception:
            for sig in signals:
                sig.setdefault('sector', 'OTHER')

        # Compute market volatility proxy from signal spread
        alphas = [s.get('alpha_score', 50) for s in signals]
        mkt_vol = 0.18  # default; could be fetched from market data in future

        result = run_quant_pipeline(signals, market_volatility=mkt_vol)

        # Serialize (remove non-JSON-safe fields)
        def clean(lst):
            out = []
            for s in lst:
                out.append({
                    'ticker': s.get('ticker'),
                    'company': s.get('company', ''),
                    'sector': s.get('sector', ''),
                    'alpha_score': round(float(s.get('alpha_score', 0)), 1),
                    'sentiment': s.get('sentiment', 'neutral'),
                    'event_type': s.get('event_type', ''),
                    'confidence': round(float(s.get('confidence', 0)), 3),
                    'bayesian_confidence': round(float(s.get('bayesian_confidence', 0)), 3),
                    'z_score': round(float(s.get('z_score', 0)), 3),
                    'percentile': s.get('percentile', 50),
                    'ls_flag': s.get('ls_flag', 'neutral'),
                    'predicted_return_pct': round(float(s.get('predicted_return_pct') or 0), 2),
                    'entry_price': s.get('entry_price'),
                    'regime': s.get('regime', ''),
                    'factor_overlay': round(float(s.get('factor_overlay', 1.0)), 4),
                    'weight_pct': s.get('weight_pct'),
                    'position_pct': s.get('position_pct'),
                    'rank_in_leg': s.get('rank_in_leg'),
                })
            return out

        return jsonify({
            'success': True,
            'data': {
                'portfolio': {
                    'long_leg': clean(result['portfolio']['long_leg']),
                    'short_leg': clean(result['portfolio']['short_leg']),
                    'stats': result['portfolio']['stats'],
                },
                'universe': clean(result['ranked'][:50]),
                'universe_stats': result['universe_stats'],
            }
        })

    except Exception as e:
        logger.error(f"Portfolio longshort error: {e}", exc_info=True)
        return jsonify({'success': True, 'data': {
            'portfolio': {'long_leg': [], 'short_leg': [], 'stats': {}},
            'universe': [], 'universe_stats': {}
        }})


# ============ STATS ============

@app.route('/api/stats', methods=['GET'])
@ttl_cache(seconds=60)
def get_stats():
    """Get system statistics"""
    try:
        stats = get_db().get_dashboard_stats()
        return jsonify({'success': True, 'data': stats})
    except Exception as e:
        logger.error(f"Get stats error: {e}")
        return jsonify({'success': True, 'data': {}})


# ============ PHASE 1.5 EXTENSION ROUTES ============
# Registered after all core routes so the catch-all /<path:filename> doesn't shadow them.
try:
    from api_ext import init_extension
    init_extension(app, get_db)
except Exception as _ext_exc:
    logger.error(f"api_ext init failed (continuing with core app only): {_ext_exc}")


# ============ V2 ROUTES (status, fast news, smart alerts, global, commodities) ============
try:
    import api_v2
    api_v2.register(app, get_db, scraper_status)
    logger.info("api_v2 registered: /api/status /api/news/fast /api/alerts/smart /api/global/markets /api/commodities/prices")
except Exception as _v2_exc:
    logger.error(f"api_v2 init failed: {_v2_exc}")


# ============ REALTIME / SSE ============
try:
    import realtime
    realtime.register(app, get_db)
    logger.info("realtime registered: /api/stream /api/stream/recent /api/stream/stats")
except Exception as _rt_exc:
    logger.error(f"realtime init failed: {_rt_exc}")


# ============ V3 ROUTES (forensics, F&O, screeners, paper, telemetry, audit) ============
try:
    import api_v3
    api_v3.register(app, get_db)
    logger.info("api_v3 registered: /api/forensics/* /api/fo/* /api/bulk-deals /api/screeners/* /api/paper/* /api/telemetry /api/audit/* /api/onboarding/* /api/watchlist/tags /api/sectors/rotation /api/geo/india /api/earnings/* /api/compare/* /api/stock/<t>/(profile|financials|ratios|shareholding|corp-actions|news|peers-detail) /api/screener/(fields|run) /api/fundamentals/score/<t>")
except Exception as _v3_exc:
    logger.error(f"api_v3 init failed: {_v3_exc}")


# ============ PUBLIC DATA API v1 — externally documented surface ============
# Establishes the "TickerWave is a data platform" positioning. Thin wrapper
# over existing endpoints; lives under /api/v1/* with stable contracts.
# Docs at /api-docs.html.
try:
    import api_public
    api_public.register(app, get_db)
except Exception as _pub_exc:
    logger.error(f"api_public init failed: {_pub_exc}")


# ============ EDGE FEATURES (outcomes, leak, whisper, sector, insider, FII/DII, bulk-deal, call sentiment) ============
try:
    import edge_features
    edge_features.init_app(app, get_db)
    logger.info("edge_features registered: /api/edge/outcomes/* /api/edge/leak-check /api/edge/earnings/* /api/edge/sector-regime /api/edge/insider-buys /api/edge/fii-dii /api/edge/bulk-deal-crossref /api/methodology/hit-rates")
except Exception as _edge_exc:
    logger.error(f"edge_features init failed: {_edge_exc}")


# ============ CHAT (RAG + Groq SSE) — /api/chat, /api/chat/quota ============
try:
    try:
        from backend.chat_routes import register as _chat_register
    except ImportError:
        # Falls through when running as `python api.py` (no `backend.` package
        # prefix on sys.path). The plain-module name resolves correctly
        # because api.py is invoked with its own directory in sys.path.
        from chat_routes import register as _chat_register  # type: ignore
    _chat_register(app, get_db, optional_auth, require_auth)
    logger.info("chat_routes registered: /api/chat (SSE) /api/chat/quota")
except Exception as _chat_exc:
    logger.error(f"chat_routes init failed: {_chat_exc}")


# ============ FREEMIUM SCHEMA (subscription_tier column on users) ============
try:
    from backend.freemium import ensure_schema as _freemium_ensure
    _freemium_ensure(get_db())
    logger.info("freemium: subscription_tier column ensured on users")
except Exception as _fm_exc:
    logger.error(f"freemium ensure failed: {_fm_exc}")


# ============ PHASE 4 — EQUITY RESEARCH + SAVED SCREENERS ============
try:
    from backend.research_routes import register as _research_register
    _research_register(app, get_db, optional_auth, require_auth)
    logger.info("research_routes registered: /api/research/<ticker> /api/research/<t>/preview")
except Exception as _rr_exc:
    logger.error(f"research_routes init failed: {_rr_exc}")

# Phase 4.5 — fundamentals AI: explainer, peer rank, score history
try:
    from backend.fundamentals_ai import register as _fai_register
    _fai_register(app, get_db)
    logger.info("fundamentals_ai registered: /api/fundamentals/(explain|peer-rank|history)/<ticker>")
except Exception as _fai_exc:
    logger.error(f"fundamentals_ai init failed: {_fai_exc}")

# Phase 5 — wire-style corporate news (broker-app parity)
try:
    from backend.stock_news_pro import register as _snp_register
    _snp_register(app, get_db)
    logger.info("stock_news_pro registered: /api/stock/<ticker>/news-pro")
except Exception as _snp_exc:
    logger.error(f"stock_news_pro init failed: {_snp_exc}")

try:
    from backend.saved_screeners import register as _saved_register
    _saved_register(app, get_db, require_auth)
    logger.info("saved_screeners registered: /api/screeners/saved (CRUD)")
except Exception as _ss_exc:
    logger.error(f"saved_screeners init failed: {_ss_exc}")


# ============ PHASE D — per-endpoint rate limits ============
# Applied centrally (here, where `limiter` is in scope) instead of via
# decorators inside the blueprints — keeps blueprint code self-contained
# and avoids the circular-import problems of cross-module limiter access.
# Default per-IP cap is 1000/h (set on Limiter init). Tighter caps below.
try:
    # IR + documents — moderate cap; these hit DB + sometimes external PDFs
    _LIMITED_VIEWS = {
        'api_v3.stock_ir':                   '60 per hour',
        'api_v3.stock_documents':            '60 per hour',
        'edge_features.methodology_hit_rates': '120 per hour',
    }
    for endpoint, rule in _LIMITED_VIEWS.items():
        view = app.view_functions.get(endpoint)
        if view is None:
            logger.debug("rate-limit skip: endpoint %s not registered", endpoint)
            continue
        # Re-decorate in place. limiter.limit returns a decorator that wraps
        # the view fn and also registers the rule with the Limiter instance.
        app.view_functions[endpoint] = limiter.limit(rule)(view)
    logger.info("Phase D rate limits applied: %s", _LIMITED_VIEWS)
except Exception as _rl_exc:
    logger.warning("rate-limit wiring skipped: %s", _rl_exc)


# ============ GEO MAP (hotspots, flow lanes, region exposure) ============
try:
    import geo_map
    geo_map.init_app(app, get_db)
    logger.info("geo_map registered: /api/geo/map/hotspots /api/geo/map/flows /api/geo/map/exposure/<id>")
except Exception as _gm_exc:
    logger.error(f"geo_map init failed: {_gm_exc}")


# ============ SMART ALERTS EVAL HOOK (called from scraper job) ============
def _evaluate_smart_alerts(signals, articles=None):
    """Run smart-alert evaluation against the latest scraper output.

    Pushes hits to the SSE 'alert' channel. Wired into run_scraper_job below.
    """
    try:
        import smart_alerts
        from smart_alerts import build_news_velocity
        ctx = {
            "signals": signals or [],
            "news_velocity": build_news_velocity(articles or []) if articles else {},
        }
        # Add bulk-deal / pledge change context if the helper module is loaded
        try:
            from bulk_deals import build_alert_context
            ctx.update(build_alert_context(get_db(), days=2))
        except Exception:
            pass
        try:
            from realtime import broadcast_alert as _bc
        except Exception:
            _bc = None
        return smart_alerts.evaluate(get_db(), ctx, broadcast_fn=_bc)
    except Exception as e:
        logger.warning(f"smart_alerts eval failed: {e}")
        return []


# ============ ERROR HANDLERS ============

@app.errorhandler(404)
def not_found(error):
    return jsonify({'success': False, 'error': 'Endpoint not found'}), 404


@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal error: {error}")
    return jsonify({'success': False, 'error': 'Internal server error'}), 500


# ============ MAIN ============

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))

    logger.info("=" * 50)
    logger.info("Tickwave API Server Starting")
    logger.info(f"Database: {'PostgreSQL' if os.getenv('DATABASE_URL', '').startswith('postgres') else 'SQLite'}")
    logger.info(f"Frontend: {FRONTEND_DIR}")
    logger.info(f"Port: {port}")
    logger.info("=" * 50)

    # Initialize database
    get_db()

    # Start background scraper only when this process owns it.
    # Web containers set RUN_SCHEDULER=false; a dedicated worker container
    # runs the scheduler. Default 'true' preserves single-process dev/Docker
    # behavior (`python api.py`).
    if os.getenv('RUN_SCHEDULER', 'true').lower() == 'true':
        start_scheduler()
    else:
        logger.info("RUN_SCHEDULER=false — scheduler skipped (expected in web container)")

    app.run(debug=False, host='0.0.0.0', port=port)
