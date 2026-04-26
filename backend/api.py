#!/usr/bin/env python3
"""
EventAlpha Backend API
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

from database_schema import EventAlphaDB
from notifications import process_signal_notifications, send_test_notification

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============ FLASK APP ============

# Serve frontend static files from ../app
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'app')

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='')
CORS(app)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret')

# ============ AUTH CONFIG ============
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_JWT_SECRET = os.getenv('SUPABASE_JWT_SECRET', '')
AUTH_MODE = 'supabase' if SUPABASE_URL else 'local'

# ============ DATABASE ============

db = None

def get_db() -> EventAlphaDB:
    """Get or create database connection"""
    global db
    if db is None:
        db = EventAlphaDB()
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
RL_MAX_HITS = int(os.getenv('RL_MAX_HITS', '90'))  # 90 req/min/ip — generous for retail UI


def _client_ip():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'


@app.after_request
def _no_cache_static(resp):
    """Force the browser to revalidate HTML/JS/CSS so a fresh deploy is seen
    immediately. Without this, cached app.js/index.html mask backend fixes.
    """
    p = (request.path or '')
    if p.endswith(('.html', '.js', '.css')) or p in ('/', ''):
        resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
    return resp


@app.before_request
def _rate_limit():
    # Only throttle public /api/* endpoints — static files and the health endpoint pass through.
    if not request.path.startswith('/api/'):
        return None
    if request.path == '/api/health':
        return None
    ip = _client_ip()
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
def get_signals():
    """Get active trading signals"""
    try:
        limit = request.args.get('limit', 50, type=int)
        signals = get_db().get_active_signals(limit)
        return jsonify({
            'success': True,
            'count': len(signals),
            'data': signals
        })
    except Exception as e:
        logger.error(f"Get signals error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


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
def get_events():
    """Get events from database"""
    try:
        limit = request.args.get('limit', 50, type=int)
        events = get_db().get_recent_events(limit)
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
        from database_schema import EventAlphaDB, _execute_query
        local_db = EventAlphaDB()
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
                'day_high': float(getattr(fi, 'dayHigh', 0) or 0),
                'day_low': float(getattr(fi, 'dayLow', 0) or 0),
                'market_cap': int(getattr(fi, 'marketCap', 0) or 0),
                'fifty_two_week_high': float(getattr(fi, 'yearHigh', 0) or 0),
                'fifty_two_week_low': float(getattr(fi, 'yearLow', 0) or 0),
            }
            # If fast_info missing data, try history
            if not price_data['price'] or not price_data.get('fifty_two_week_high'):
                hist = t.history(period='1y')
                if not hist.empty:
                    if not price_data['price']:
                        price_data['price'] = float(hist['Close'].iloc[-1])
                    price_data['fifty_two_week_high'] = float(hist['Close'].max())
                    price_data['fifty_two_week_low'] = float(hist['Close'].min())
                    if len(hist) >= 2:
                        prev = float(hist['Close'].iloc[-2])
                        curr = float(hist['Close'].iloc[-1])
                        price_data['change_pct'] = round((curr - prev) / prev * 100, 2) if prev else 0
                        price_data['day_high'] = float(hist['High'].iloc[-1])
                        price_data['day_low'] = float(hist['Low'].iloc[-1])
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
def get_sectors():
    """Get sector-level aggregated data for heatmap"""
    try:
        from database_schema import EventAlphaDB, _execute_query
        from stock_universe import STOCK_UNIVERSE
        local_db = EventAlphaDB()

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
        from database_schema import EventAlphaDB, _execute_query
        from stock_universe import STOCK_UNIVERSE
        local_db = EventAlphaDB()

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
def portfolio_simulator():
    """Simulate returns from following top alpha signals.
    Shows what would happen if you invested equally in top N signals."""
    try:
        from database_schema import EventAlphaDB, _execute_query
        local_db = EventAlphaDB()

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
def simulator_equity_curve():
    """Return time-series data for cumulative PnL chart and drawdown chart."""
    try:
        from database_schema import EventAlphaDB, _execute_query
        local_db = EventAlphaDB()

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
def get_earnings_calendar():
    """Get upcoming earnings dates for monitored stocks via yfinance"""
    try:
        import yfinance as yf
        from stock_universe import STOCK_UNIVERSE
        from config import MONITORED_STOCKS

        # Use monitored stocks (32) + any watchlist stocks for the calendar
        tickers_to_check = list(MONITORED_STOCKS)

        # Limit to avoid timeout — check top 20
        results = []
        for ticker in tickers_to_check[:20]:
            try:
                t = yf.Ticker(f"{ticker}.NS")
                cal = t.calendar
                if cal is not None and not (hasattr(cal, 'empty') and cal.empty):
                    # cal can be a dict or DataFrame
                    if isinstance(cal, dict):
                        earn_date = cal.get('Earnings Date', [None])
                        if isinstance(earn_date, list) and earn_date:
                            earn_date = str(earn_date[0])
                        else:
                            earn_date = str(earn_date) if earn_date else None
                    else:
                        # DataFrame
                        if 'Earnings Date' in cal.columns:
                            earn_date = str(cal['Earnings Date'].iloc[0]) if len(cal) > 0 else None
                        elif 'Earnings Date' in cal.index:
                            val = cal.loc['Earnings Date']
                            earn_date = str(val.iloc[0]) if hasattr(val, 'iloc') else str(val)
                        else:
                            earn_date = None

                    if earn_date and earn_date != 'None' and earn_date != 'NaT':
                        info = STOCK_UNIVERSE.get(ticker, {})
                        results.append({
                            'ticker': ticker,
                            'company': info.get('name', ticker),
                            'sector': info.get('sector', ''),
                            'earnings_date': earn_date[:10],  # YYYY-MM-DD
                        })
            except Exception:
                continue

        results.sort(key=lambda x: x.get('earnings_date', '9999'))
        return jsonify({'success': True, 'count': len(results), 'data': results})
    except Exception as e:
        logger.error(f"Earnings calendar error: {e}")
        return jsonify({'success': True, 'count': 0, 'data': []})


# ============ EVENT TIMELINE ============

@app.route('/api/stock/<ticker>/timeline', methods=['GET'])
def get_stock_timeline(ticker):
    """Get events + signals for a specific stock over time (for timeline view)"""
    ticker = ticker.strip().upper()
    try:
        from database_schema import EventAlphaDB, _execute_query
        local_db = EventAlphaDB()
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
    """Create new alert"""
    try:
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
    """Add stock to watchlist"""
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

@app.route('/api/market-indices', methods=['GET'])
def get_market_indices():
    """Get live Nifty 50, Sensex, and other key Indian market indices via yfinance."""
    import time as _time
    cache_ttl = 300  # 5 minutes

    if _indices_cache['data'] and (_time.time() - _indices_cache['time']) < cache_ttl:
        return jsonify({'success': True, 'data': _indices_cache['data'], 'cached': True})

    try:
        import yfinance as yf
        indices_meta = [
            {'symbol': '^NSEI',    'short': 'NIFTY 50'},
            {'symbol': '^BSESN',   'short': 'SENSEX'},
            {'symbol': '^NSEBANK', 'short': 'BANK NIFTY'},
            {'symbol': '^CNXIT',   'short': 'NIFTY IT'},
            {'symbol': '^INDIAVIX','short': 'INDIA VIX'},
            {'symbol': 'USDINR=X', 'short': 'USD/INR'},
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
                    'price':  round(price, 2),
                    'change': round(chg, 2),
                    'change_pct': round(chg_pct, 2),
                })
            except Exception as e:
                logger.warning(f"Indices fetch failed for {meta['symbol']}: {e}")
                result.append({'symbol': meta['symbol'], 'short': meta['short'],
                               'price': 0, 'change': 0, 'change_pct': 0})

        _indices_cache['data'] = result
        _indices_cache['time'] = _time.time()
        return jsonify({'success': True, 'data': result, 'cached': False})
    except Exception as e:
        logger.error(f"Market indices error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ ANALYTICS (Jane Street-grade edge analysis) ============

@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    """
    Comprehensive analytics — exposes the system's actual statistical edge.
    Returns hit rates by alpha bucket, event type, and horizon; EV per signal;
    IC estimate; sector concentration; and a signal leaderboard.
    """
    try:
        from database_schema import EventAlphaDB, _execute_query
        local_db = EventAlphaDB()
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
        from database_schema import EventAlphaDB, _execute_query

        local_db = EventAlphaDB()
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
    logger.info("EventAlpha API Server Starting")
    logger.info(f"Database: {'PostgreSQL' if os.getenv('DATABASE_URL', '').startswith('postgres') else 'SQLite'}")
    logger.info(f"Frontend: {FRONTEND_DIR}")
    logger.info(f"Port: {port}")
    logger.info("=" * 50)

    # Initialize database
    get_db()

    # Start background scraper
    start_scheduler()

    app.run(debug=False, host='0.0.0.0', port=port)
