// EventAlpha - Shared Router, API, LiveData & Utilities

// ============ ROUTER ============
class Router {
  constructor() {
    this.currentPage = 'home';
    this.routes = {
      home: { file: 'index.html', title: 'Home' },
      explore: { file: 'explore.html', title: 'Explore' },
      map: { file: 'map.html', title: 'Map' },
      alerts: { file: 'alerts.html', title: 'Alerts' },
      watchlist: { file: 'watchlist.html', title: 'Watchlist' },
      stock: { file: 'stock.html', title: 'Stock Details' },
      events: { file: 'events.html', title: 'Events' }
    };
    this.init();
  }

  init() {
    const currentFile = window.location.pathname.split('/').pop() || 'index.html';
    const pageName = currentFile.replace('.html', '') || 'index';
    this.currentPage = pageName === 'index' ? 'home' : pageName;
    this.setActiveNavLink(this.currentPage);
  }

  navigate(pageName) {
    if (!this.routes[pageName]) return;
    this.currentPage = pageName;
    this.setActiveNavLink(pageName);
    this.updatePageTitle(this.routes[pageName].title);
    window.history.pushState({ page: pageName }, null, `?page=${pageName}`);
    window.dispatchEvent(new CustomEvent('pagechange', { detail: { page: pageName } }));
  }

  setActiveNavLink(pageName) {
    document.querySelectorAll('[data-nav]').forEach(link => {
      link.classList.toggle('active', link.dataset.nav === pageName);
    });
  }

  updatePageTitle(title) {
    document.title = `EventAlpha | ${title}`;
  }

  getCurrentPage() {
    return this.currentPage;
  }
}

const router = new Router();

// ============ AUTH ============
class Auth {
  static TOKEN_KEY = 'eventalpha_token';
  static USER_KEY = 'eventalpha_user';

  static getToken() { return Storage.get(this.TOKEN_KEY); }
  static getUser() { return Storage.get(this.USER_KEY); }
  static isLoggedIn() { return !!this.getToken(); }

  static async signup(email, password) {
    const res = await API.post('/auth/signup', { email, password });
    if (res?.success && res.token) {
      Storage.set(this.TOKEN_KEY, res.token);
      Storage.set(this.USER_KEY, res.user);
    }
    return res;
  }

  static async login(email, password) {
    const res = await API.post('/auth/login', { email, password });
    if (res?.success && res.token) {
      Storage.set(this.TOKEN_KEY, res.token);
      Storage.set(this.USER_KEY, res.user);
    }
    return res;
  }

  static logout() {
    Storage.remove(this.TOKEN_KEY);
    Storage.remove(this.USER_KEY);
    LiveData.clearCache();
    window.location.href = 'login.html';
  }

  static async initSupabase(supabaseUrl, supabaseAnonKey) {
    // For Supabase auth mode — load SDK dynamically if needed
    if (!supabaseUrl) return;
    try {
      const { createClient } = await import('https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm');
      this._supabase = createClient(supabaseUrl, supabaseAnonKey);
    } catch (e) {
      console.warn('Supabase SDK not available, using local auth');
    }
  }

  static async supabaseSignup(email, password) {
    if (!this._supabase) return { success: false, error: 'Supabase not initialized' };
    const { data, error } = await this._supabase.auth.signUp({ email, password });
    if (error) return { success: false, error: error.message };
    if (data.session) {
      Storage.set(this.TOKEN_KEY, data.session.access_token);
      Storage.set(this.USER_KEY, { id: data.user.id, email: data.user.email });
    }
    return { success: true, user: data.user, needsVerification: !data.session };
  }

  static async supabaseLogin(email, password) {
    if (!this._supabase) return { success: false, error: 'Supabase not initialized' };
    const { data, error } = await this._supabase.auth.signInWithPassword({ email, password });
    if (error) return { success: false, error: error.message };
    Storage.set(this.TOKEN_KEY, data.session.access_token);
    Storage.set(this.USER_KEY, { id: data.user.id, email: data.user.email });
    return { success: true, user: data.user };
  }

  static updateAuthUI() {
    const el = document.getElementById('authStatus');
    if (!el) return;
    const user = this.getUser();
    if (user) {
      el.innerHTML = `
        <div class="flex items-center gap-3 py-3">
          <div class="w-8 h-8 rounded-lg bg-surface-container-highest flex items-center justify-center border border-outline-variant/20">
            <span class="material-symbols-outlined text-sm text-primary">person</span>
          </div>
          <div class="overflow-hidden flex-1">
            <p class="text-xs font-bold truncate">${user.email || 'User'}</p>
            <button onclick="Auth.logout()" class="text-[10px] text-error hover:underline">Logout</button>
          </div>
        </div>`;
    } else {
      el.innerHTML = `
        <a href="login.html" class="sidebar-link" style="padding-left:0;">
          <span class="material-symbols-outlined sidebar-icon">login</span>Login
        </a>`;
    }
  }
}

// ============ API ============
class API {
  // If served via python -m http.server (port 8000), backend is on 5000.
  // If served directly by Flask (port 5000), use relative /api.
  static baseURL = (() => {
    const port = window.location.port;
    if (port === '8000' || port === '3000') return 'http://localhost:5000/api';
    return '/api';
  })();

  static _headers() {
    const h = { 'Content-Type': 'application/json' };
    const token = Auth.getToken();
    if (token) h['Authorization'] = `Bearer ${token}`;
    return h;
  }

  static async get(endpoint) {
    try {
      const response = await fetch(`${this.baseURL}${endpoint}`, { headers: this._headers() });
      if (response.status === 401) { Auth.logout(); return null; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      console.warn(`GET ${endpoint}:`, error.message);
      return null;
    }
  }

  static async post(endpoint, data) {
    try {
      const response = await fetch(`${this.baseURL}${endpoint}`, {
        method: 'POST', headers: this._headers(), body: JSON.stringify(data)
      });
      if (response.status === 401 && !endpoint.includes('/auth/')) { Auth.logout(); return null; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      console.warn(`POST ${endpoint}:`, error.message);
      return null;
    }
  }

  static async put(endpoint, data) {
    try {
      const response = await fetch(`${this.baseURL}${endpoint}`, {
        method: 'PUT', headers: this._headers(), body: JSON.stringify(data)
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      console.warn(`PUT ${endpoint}:`, error.message);
      return null;
    }
  }

  static async delete(endpoint, data) {
    try {
      const response = await fetch(`${this.baseURL}${endpoint}`, {
        method: 'DELETE', headers: this._headers(), body: data ? JSON.stringify(data) : undefined
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      console.warn(`DELETE ${endpoint}:`, error.message);
      return null;
    }
  }
}

// ============ LIVE DATA (replaces MockData) ============
class LiveData {
  // Cache to avoid hammering API
  static _cache = {};
  static _cacheTTL = 30000; // 30 seconds

  static _isCached(key) {
    const entry = this._cache[key];
    return entry && (Date.now() - entry.time < this._cacheTTL);
  }

  static _setCache(key, data) {
    this._cache[key] = { data, time: Date.now() };
  }

  static async getDashboard() {
    if (this._isCached('dashboard')) return this._cache['dashboard'].data;
    const res = await API.get('/dashboard');
    const data = res?.data || {};
    this._setCache('dashboard', data);
    return data;
  }

  static async getLatestEvents(limit = 20) {
    if (this._isCached('events')) return this._cache['events'].data;
    const res = await API.get(`/events?limit=${limit}`);
    const data = res?.data || [];
    this._setCache('events', data);
    return data;
  }

  static async getTopSignals(limit = 20) {
    if (this._isCached('signals')) return this._cache['signals'].data;
    const res = await API.get(`/signals?limit=${limit}`);
    const data = res?.data || [];
    this._setCache('signals', data);
    return data;
  }

  static async getStockCards(limit = 20) {
    // Signals serve as stock cards in explore view
    const res = await API.get(`/signals?limit=${limit}`);
    return res?.data || [];
  }

  static async getWatchlist() {
    const res = await API.get('/watchlist');
    return res?.data || [];
  }

  static async getAlerts(limit = 20) {
    const res = await API.get(`/alerts?limit=${limit}`);
    return res?.data || [];
  }

  static async getGeoEvents() {
    const res = await API.get('/geoevents');
    return res?.data || [];
  }

  static async getScraperStatus() {
    const res = await API.get('/scraper/status');
    return res?.data || {};
  }

  static async getNotificationConfig() {
    const res = await API.get('/notifications/config');
    return res?.data || null;
  }

  static async saveNotificationConfig(config) {
    return await API.post('/notifications/config', config);
  }

  static async testNotification() {
    return await API.post('/notifications/test', {});
  }

  static async addToWatchlist(ticker, notes = '') {
    return await API.post('/watchlist', { ticker, notes });
  }

  static async removeFromWatchlist(ticker) {
    return await API.delete('/watchlist', { ticker });
  }

  static async createAlert(ticker, alertType, condition, threshold) {
    return await API.post('/alerts', {
      ticker, alert_type: alertType, condition, threshold
    });
  }

  static async triggerScraper() {
    return await API.post('/scraper/run', {});
  }

  static async getAccuracy() {
    const res = await API.get('/accuracy');
    return res?.data || null;
  }

  static async getPredictionOutcomes(limit = 20) {
    const res = await API.get(`/predictions/track?limit=${limit}`);
    return res?.data || [];
  }

  static async checkPredictions() {
    return await API.post('/predictions/check', {});
  }

  static async searchStock(query) {
    const res = await API.get(`/search?q=${encodeURIComponent(query)}`);
    return res?.data || [];
  }

  // Cache watchlist tickers for quick lookup
  static _watchlistTickers = null;
  static async getWatchlistTickers() {
    if (!this._watchlistTickers) {
      const items = await this.getWatchlist();
      this._watchlistTickers = new Set(items.map(w => w.ticker));
    }
    return this._watchlistTickers;
  }
  static async isInWatchlist(ticker) {
    const tickers = await this.getWatchlistTickers();
    return tickers.has(ticker);
  }
  static invalidateWatchlistCache() {
    this._watchlistTickers = null;
  }

  // Clear cache to force fresh data
  static clearCache() {
    this._cache = {};
  }

  static async getMarketIndices() {
    if (this._isCached('market-indices')) return this._cache['market-indices'].data;
    const res = await API.get('/market-indices');
    const data = res?.data || [];
    if (data.length > 0) this._setCache('market-indices', data);
    return data;
  }
}

// ============ SOUND EFFECTS ============
class SoundFX {
  static _ctx = null;
  static _enabled = true;
  static _lastSignalCount = 0;

  static _getCtx() {
    if (!this._ctx) {
      this._ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
    return this._ctx;
  }

  static pop() {
    if (!this._enabled) return;
    try {
      const ctx = this._getCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = 'sine';
      osc.frequency.setValueAtTime(880, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.15);
      gain.gain.setValueAtTime(0.3, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.2);
      osc.start(ctx.currentTime);
      osc.stop(ctx.currentTime + 0.2);
    } catch (e) { /* AudioContext not available */ }
  }

  static alertHigh() {
    if (!this._enabled) return;
    try {
      const ctx = this._getCtx();
      // Two-tone alert for high-alpha signals
      [0, 0.12].forEach((delay, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = 'sine';
        osc.frequency.setValueAtTime(i === 0 ? 660 : 990, ctx.currentTime + delay);
        gain.gain.setValueAtTime(0.25, ctx.currentTime + delay);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + 0.15);
        osc.start(ctx.currentTime + delay);
        osc.stop(ctx.currentTime + delay + 0.15);
      });
    } catch (e) {}
  }

  static success() {
    if (!this._enabled) return;
    try {
      const ctx = this._getCtx();
      // Rising three-note chime
      [0, 0.08, 0.16].forEach((delay, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = 'sine';
        osc.frequency.setValueAtTime([523, 659, 784][i], ctx.currentTime + delay);
        gain.gain.setValueAtTime(0.2, ctx.currentTime + delay);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + 0.12);
        osc.start(ctx.currentTime + delay);
        osc.stop(ctx.currentTime + delay + 0.12);
      });
    } catch (e) {}
  }

  static toggle() {
    this._enabled = !this._enabled;
    Storage.set('sound_enabled', this._enabled);
    return this._enabled;
  }

  static init() {
    const saved = Storage.get('sound_enabled');
    if (saved !== null) this._enabled = saved;
  }

  static checkForNewSignals(currentCount) {
    if (this._lastSignalCount > 0 && currentCount > this._lastSignalCount) {
      const newCount = currentCount - this._lastSignalCount;
      this.pop();
      console.log(`${newCount} new signal(s) detected`);
    }
    this._lastSignalCount = currentCount;
  }
}

// ============ AUTO-REFRESH ============
class DataRefresher {
  static _intervals = [];

  static start(callback, intervalMs = 60000) {
    callback();
    const id = setInterval(callback, intervalMs);
    this._intervals.push(id);
    return id;
  }

  static stop(id) {
    clearInterval(id);
    this._intervals = this._intervals.filter(i => i !== id);
  }

  static stopAll() {
    this._intervals.forEach(id => clearInterval(id));
    this._intervals = [];
  }
}

// ============ MOCK DATA (fallback when API unreachable) ============
class MockData {
  static getLatestEvents() {
    return [
      {
        id: 1, company: 'KNR Constructions Ltd.', ticker: 'KNRCON',
        event_type: 'order_win', sentiment: 'bullish', magnitude: 8.5,
        impact_score: 88, impactColor: 'secondary', confidence: 0.85,
        timestamp: new Date().toISOString(),
        event_projection: 'Aggressive Bullish', order_value: '1,240.50 Cr',
        sentiment_delta: '+12.4%', horizon: '20D',
        title: 'KNR Constructions wins NHAI highway project worth Rs 1240 Cr',
        summary: 'Infrastructure mandate for NHAI project confirms revenue visibility.',
        quant_sentiment: 82, crowd_sentiment: 64,
        insights: 'Infrastructure mandate confirms revenue visibility of 3.8x Book-to-Bill.'
      },
      {
        id: 2, company: 'Infosys Ltd.', ticker: 'INFY',
        event_type: 'earnings', sentiment: 'bullish', magnitude: 7.2,
        impact_score: 78, impactColor: 'secondary', confidence: 0.78,
        timestamp: new Date().toISOString(),
        event_projection: 'Moderate Bullish', sentiment_delta: '+4.2%', horizon: '3D',
        title: 'Infosys Q4 results beat street estimates',
        summary: 'Net profit rises 12% YoY on strong deal wins.',
        quant_sentiment: 72, crowd_sentiment: 65,
        insights: 'Strong deal pipeline and margin expansion drive optimism.'
      },
      {
        id: 3, company: 'Reliance Industries', ticker: 'RELIANCE',
        event_type: 'supply', sentiment: 'neutral', magnitude: 6.1,
        impact_score: 62, impactColor: 'on-surface-variant', confidence: 0.55,
        timestamp: new Date().toISOString(),
        event_projection: 'Stable', sentiment_delta: '+1.2%', horizon: '5D',
        title: 'Reliance secures new manufacturing facility in Vietnam',
        summary: 'Expected to improve supply chain resilience.',
        quant_sentiment: 58, crowd_sentiment: 48,
        insights: 'New manufacturing facility improves supply chain resilience.'
      }
    ];
  }

  static getTopSignals() {
    return [
      { rank: 1, ticker: 'INFY', company: 'INFOSYS', alpha_score: 84.8, expected_return: '+8.4%', confidence: 0.88, confidence_pct: 88, time_horizon: '20D', volatility: 0.18, trend: 'STRENGTHENING', sentiment: 'bullish' },
      { rank: 2, ticker: 'TCS', company: 'TCS', alpha_score: 78.2, expected_return: '+5.6%', confidence: 0.82, confidence_pct: 82, time_horizon: '3D', volatility: 0.21, trend: 'STRENGTHENING', sentiment: 'bullish' },
      { rank: 3, ticker: 'RELIANCE', company: 'RELIANCE INDUSTRIES', alpha_score: 72.4, expected_return: '+2.8%', confidence: 0.75, confidence_pct: 75, time_horizon: '5D', volatility: 0.24, trend: 'STABLE', sentiment: 'neutral' }
    ];
  }

  static getStockCards() {
    return [
      { ticker: 'INFY', company: 'Infosys Ltd.', alpha_score: 84.8, change_pct: '+2.4%', sentiment: 'bullish', confidence: 0.88, regime: 'bull_strong' },
      { ticker: 'TCS', company: 'TCS', alpha_score: 78.2, change_pct: '+1.8%', sentiment: 'bullish', confidence: 0.82, regime: 'bull_weak' },
      { ticker: 'RELIANCE', company: 'Reliance Industries', alpha_score: 72.4, change_pct: '+0.5%', sentiment: 'neutral', confidence: 0.75, regime: 'sideways_calm' }
    ];
  }

  static getWatchlist() {
    return [
      { ticker: 'INFY', notes: 'Tracking earnings', created_at: new Date().toISOString() },
      { ticker: 'RELIANCE', notes: '', created_at: new Date().toISOString() }
    ];
  }

  static getAlerts() { return []; }
  static getGeoEvents() {
    // Fallback dataset — used when backend is offline
    return [
      { country:'India',          event_description:'RBI Monetary Policy — repo rate decision',               impact:'high',   latitude:28.6139,  longitude:77.2090,  event_type:'monetary' },
      { country:'United States',  event_description:'Federal Reserve FOMC meeting — US rate path & dot plot', impact:'high',   latitude:38.8951,  longitude:-77.0369, event_type:'monetary' },
      { country:'China',          event_description:'PBoC stimulus — rate cut & yuan depreciation',           impact:'high',   latitude:39.9042,  longitude:116.4074, event_type:'monetary' },
      { country:'Saudi Arabia',   event_description:'OPEC+ extends voluntary 1 mb/d crude output cuts',       impact:'high',   latitude:24.7136,  longitude:46.6753,  event_type:'oil'      },
      { country:'Russia',         event_description:'Russia–Ukraine war: fertilizer & wheat supply squeeze',  impact:'high',   latitude:55.7558,  longitude:37.6173,  event_type:'conflict' },
      { country:'United States',  event_description:'US Big Tech Q2 earnings — AI capex cycle signal',        impact:'high',   latitude:37.7749,  longitude:-122.4194,event_type:'tech'     },
      { country:'Iran',           event_description:'US–Iran sanctions tighten — Hormuz shipping risk',       impact:'high',   latitude:35.6892,  longitude:51.3890,  event_type:'conflict' },
      { country:'Japan',          event_description:'Bank of Japan ends negative rate policy — yen surge',    impact:'medium', latitude:35.6762,  longitude:139.6503, event_type:'monetary' },
      { country:'Germany',        event_description:'ECB rate cut cycle — euro weakness, FII capital flows',  impact:'medium', latitude:52.5200,  longitude:13.4050,  event_type:'monetary' },
      { country:'UAE',            event_description:'India–UAE CEPA: $100B trade target — pharma & IT',       impact:'medium', latitude:25.2048,  longitude:55.2708,  event_type:'trade'    },
      { country:'Singapore',      event_description:'SGX Nifty futures: positive overnight global sentiment', impact:'medium', latitude:1.3521,   longitude:103.8198, event_type:'markets'  },
      { country:'South Korea',    event_description:'Samsung DRAM price surge — semiconductor supply alert',  impact:'medium', latitude:37.5665,  longitude:126.9780, event_type:'tech'     },
      { country:'Australia',      event_description:'Iron ore price surge on China stimulus data',            impact:'medium', latitude:-33.8688, longitude:151.2093, event_type:'commodity'},
      { country:'Netherlands',    event_description:'ASML export controls: EUV lithography ban to China',    impact:'medium', latitude:52.3676,  longitude:4.9041,   event_type:'tech'     },
      { country:'United Kingdom', event_description:'Bank of England rate decision — sterling & UK outlook',  impact:'medium', latitude:51.5074,  longitude:-0.1278,  event_type:'monetary' },
      { country:'Indonesia',      event_description:'Indonesia palm oil export policy — edible oil prices',   impact:'medium', latitude:-6.2088,  longitude:106.8456, event_type:'commodity'},
      { country:'India',          event_description:'SEBI tightens F&O margin norms for retail traders',      impact:'medium', latitude:17.3850,  longitude:78.4867,  event_type:'policy'   },
      { country:'India',          event_description:'India–EU Free Trade Agreement — IT & pharma exports',    impact:'low',    latitude:12.9716,  longitude:77.5946,  event_type:'trade'    },
      { country:'Brazil',         event_description:'Record soybean harvest — agri commodity glut risk',      impact:'low',    latitude:-23.5505, longitude:-46.6333, event_type:'commodity'},
      { country:'South Africa',   event_description:'SA gold mine strike — global gold output risk',          impact:'low',    latitude:-26.2041, longitude:28.0473,  event_type:'commodity'},
    ];
  }

  static getAlphaMovers() {
    return [
      { ticker: 'KNRCON', company: 'Infrastructure', pct_change: '+8.42%', sensitivity: 'High' },
      { ticker: 'RELIANCE', company: 'Energy', pct_change: '+1.24%', sensitivity: 'Low' },
      { ticker: 'TECHM', company: 'IT Services', pct_change: '-3.15%', sensitivity: 'High' }
    ];
  }

  static getSectorSentiment() {
    return [
      { sector: 'INFRA', sentiment: 95, color: 'secondary' },
      { sector: 'ENER', sentiment: 60, color: 'secondary' },
      { sector: 'TECH', sentiment: 40, color: 'error' },
      { sector: 'FIN', sentiment: 45, color: 'on-surface-variant' },
      { sector: 'AUTO', sentiment: 80, color: 'secondary' },
      { sector: 'CONS', sentiment: 20, color: 'error' }
    ];
  }
}


// ============ UI UTILITIES ============
class UIHelper {
  static formatNumber(num, decimals = 2) {
    if (num == null || isNaN(num)) return '0.00';
    return Number(num).toLocaleString('en-IN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  }

  static formatPercent(num, decimals = 2) {
    if (num == null || isNaN(num)) return '0.00%';
    return `${num > 0 ? '+' : ''}${Number(num).toFixed(decimals)}%`;
  }

  static formatPrice(num) {
    if (num == null || isNaN(num) || num === 0) return '--';
    return `₹${Number(num).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }

  static formatTime(isoString) {
    if (!isoString) return '--';
    const date = new Date(isoString);
    if (isNaN(date.getTime())) return isoString;
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString('en-IN', { month: 'short', day: 'numeric' });
  }

  static sentimentColor(sentiment) {
    if (sentiment === 'bullish' || sentiment === 'positive') return 'var(--color-secondary)';
    if (sentiment === 'bearish' || sentiment === 'negative') return 'var(--color-error)';
    return 'var(--color-on-surface-variant)';
  }

  static sentimentBadgeClass(sentiment) {
    if (sentiment === 'bullish' || sentiment === 'positive') return 'badge-success';
    if (sentiment === 'bearish' || sentiment === 'negative') return 'badge-error';
    return 'badge-primary';
  }

  static tickerLink(ticker, extraClass = '') {
    return `<span data-ticker="${ticker}" class="cursor-pointer text-primary hover:underline font-mono font-bold ${extraClass}">${ticker}</span>`;
  }

  static alphaColor(score) {
    if (score >= 75) return 'var(--bull)';
    if (score >= 55) return 'var(--caution)';
    return 'var(--t2)';
  }

  static getConfidenceColor(confidence) {
    if (confidence > 0.8) return 'var(--bull)';
    if (confidence > 0.6) return 'var(--info)';
    if (confidence > 0.4) return 'var(--caution)';
    return 'var(--bear)';
  }

  static showLoading(container) {
    if (typeof container === 'string') container = document.getElementById(container);
    if (container) container.innerHTML = '<div class="loading-pulse" style="padding:2rem;text-align:center;opacity:0.5;">Loading...</div>';
  }

  static showEmpty(container, message = 'No data available yet. Waiting for scraper...') {
    if (typeof container === 'string') container = document.getElementById(container);
    if (container) container.innerHTML = `<div style="padding:2rem;text-align:center;opacity:0.4;font-size:0.85rem;">${message}</div>`;
  }
}

// ============ STOCK POPUP ============
class StockPopup {
  static _modal = null;

  static _createModal() {
    if (this._modal) return;
    const overlay = document.createElement('div');
    overlay.id = 'stockPopupOverlay';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9999;display:none;align-items:center;justify-content:center;backdrop-filter:blur(4px);';
    overlay.innerHTML = '<div id="stockPopupContent" style="background:#1c2026;border:1px solid #42475466;border-radius:16px;width:90%;max-width:500px;max-height:85vh;overflow-y:auto;box-shadow:0 24px 80px rgba(0,0,0,0.6);"></div>';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) StockPopup.close(); });
    document.body.appendChild(overlay);
    this._modal = overlay;
  }

  static close() {
    if (!this._modal) return;
    if (typeof AlphaFX !== 'undefined') {
      AlphaFX.popupClose(this._modal);
    } else {
      this._modal.style.display = 'none';
    }
  }

  static async show(ticker) {
    this._createModal();
    SoundFX.pop();
    const content = document.getElementById('stockPopupContent');
    content.innerHTML = `<div style="padding:40px;text-align:center;color:#8c909f;font-size:13px;">Loading ${ticker}...</div>`;
    this._modal.style.display = 'flex';
    if (typeof AlphaFX !== 'undefined') AlphaFX.popupOpen(this._modal);

    let res = await API.get(`/stock/${encodeURIComponent(ticker)}`);
    if (!res?.success) {
      // Even if API fails, show basic info
      res = { success: true, data: { ticker, company: ticker, price: {}, signal: null, predictions: {} } };
    }

    const d = res.data;
    const sig = d.signal;
    const p = d.price || {};
    const preds = d.predictions || {};

    const sent = sig?.sentiment || 'neutral';
    const sentColor = sent === 'bullish' ? 'var(--bull)' : sent === 'bearish' ? 'var(--bear)' : 'var(--t2)';
    const alpha = sig?.alpha_score || 0;

    // Prediction rows
    let predRows = '';
    for (const h of ['1D', '3D', '20D']) {
      const pr = preds[h];
      if (pr) {
        const ret = pr.predicted_return_pct || 0;
        const retColor = ret >= 0 ? 'var(--bull)' : 'var(--bear)';
        const tp = pr.target_price || 0;
        const conf = ((pr.confidence || 0) * 100).toFixed(0);
        const actual = pr.actual_return_pct;
        const hitIcon = pr.hit_target === true ? '&check;' : pr.hit_target === false ? '&times;' : '—';
        const hitColor = pr.hit_target === true ? 'var(--bull)' : pr.hit_target === false ? 'var(--bear)' : 'var(--t3)';
        predRows += `<tr style="border-bottom:1px solid #42475422;">
          <td style="padding:10px 12px;font-weight:700;color:#dfe2eb;font-size:13px;">${h}</td>
          <td style="padding:10px 12px;color:${retColor};font-weight:700;font-family:'JetBrains Mono',monospace;font-size:13px;">${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%</td>
          <td style="padding:10px 12px;color:#c2c6d6;font-family:'JetBrains Mono',monospace;font-size:12px;">${tp > 0 ? '₹' + tp.toFixed(2) : '—'}</td>
          <td style="padding:10px 12px;color:#8c909f;font-size:11px;">${conf}%</td>
          <td style="padding:10px 12px;text-align:center;color:${hitColor};font-size:14px;">${hitIcon}</td>
        </tr>`;
      }
    }
    if (!predRows) {
      predRows = '<tr><td colspan="5" style="padding:16px;text-align:center;color:#8c909f55;font-size:12px;">No predictions yet</td></tr>';
    }

    const priceChange = p.change_pct || 0;
    const priceColor = priceChange >= 0 ? 'var(--bull)' : 'var(--bear)';

    content.innerHTML = `
      <div style="padding:24px;">
        <!-- Header -->
        <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:20px;">
          <div>
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
              <span style="font-family:'Space Grotesk',sans-serif;font-size:24px;font-weight:900;color:#dfe2eb;">${d.ticker}</span>
              ${sig ? `<span style="font-size:9px;padding:3px 8px;border-radius:4px;background:${sentColor}22;color:${sentColor};font-weight:700;text-transform:uppercase;">${sent}</span>` : ''}
            </div>
            <div style="font-size:12px;color:#8c909f;">${d.company}</div>
            ${p.sector ? `<div style="font-size:10px;color:#8c909f88;margin-top:2px;">${p.sector}${p.industry ? ' / ' + p.industry : ''}</div>` : ''}
          </div>
          <button onclick="StockPopup.close()" style="background:none;border:none;color:#8c909f;cursor:pointer;font-size:20px;padding:4px;">&times;</button>
        </div>

        <!-- Price -->
        <div style="display:flex;gap:16px;margin-bottom:20px;padding:16px;background:#10141a;border-radius:12px;border:1px solid #42475422;">
          <div style="flex:1;">
            <div style="font-size:10px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;">Current Price</div>
            <div style="font-size:28px;font-weight:900;color:#dfe2eb;font-family:'JetBrains Mono',monospace;">${p.price ? '₹' + p.price.toLocaleString('en-IN', {minimumFractionDigits:2}) : '—'}</div>
            <div style="font-size:13px;font-weight:700;color:${priceColor};">${priceChange >= 0 ? '+' : ''}${priceChange.toFixed(2)}%</div>
          </div>
          ${sig ? `<div style="text-align:right;">
            <div style="font-size:10px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px;">Alpha Score</div>
            <div style="font-size:28px;font-weight:700;font-family:var(--font-mono);color:${UIHelper.alphaColor(alpha)};">${alpha.toFixed(1)}</div>
          </div>` : ''}
        </div>

        <!-- Quick Stats -->
        ${p.price ? `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:20px;">
          <div style="padding:10px;background:#10141a;border-radius:8px;text-align:center;">
            <div style="font-size:8px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;">Day High</div>
            <div style="font-size:12px;font-weight:700;color:#dfe2eb;font-family:'JetBrains Mono',monospace;">₹${(p.day_high||0).toFixed(0)}</div>
          </div>
          <div style="padding:10px;background:#10141a;border-radius:8px;text-align:center;">
            <div style="font-size:8px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;">Day Low</div>
            <div style="font-size:12px;font-weight:700;color:#dfe2eb;font-family:'JetBrains Mono',monospace;">₹${(p.day_low||0).toFixed(0)}</div>
          </div>
          <div style="padding:10px;background:#10141a;border-radius:8px;text-align:center;">
            <div style="font-size:8px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;">52W High</div>
            <div style="font-size:12px;font-weight:700;color:#4edea3;font-family:'JetBrains Mono',monospace;">₹${(p.fifty_two_week_high||0).toFixed(0)}</div>
          </div>
          <div style="padding:10px;background:#10141a;border-radius:8px;text-align:center;">
            <div style="font-size:8px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;">52W Low</div>
            <div style="font-size:12px;font-weight:700;color:#ffb4ab;font-family:'JetBrains Mono',monospace;">₹${(p.fifty_two_week_low||0).toFixed(0)}</div>
          </div>
        </div>` : ''}

        <!-- Predictions Table -->
        <div style="margin-bottom:16px;">
          <div style="font-size:10px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;margin-bottom:10px;font-weight:800;">Predicted Returns</div>
          <table style="width:100%;border-collapse:collapse;">
            <thead>
              <tr style="border-bottom:1px solid #42475444;">
                <th style="padding:8px 12px;text-align:left;font-size:9px;color:#8c909f;text-transform:uppercase;">Horizon</th>
                <th style="padding:8px 12px;text-align:left;font-size:9px;color:#8c909f;text-transform:uppercase;">Return</th>
                <th style="padding:8px 12px;text-align:left;font-size:9px;color:#8c909f;text-transform:uppercase;">Target</th>
                <th style="padding:8px 12px;text-align:left;font-size:9px;color:#8c909f;text-transform:uppercase;">Conf</th>
                <th style="padding:8px 12px;text-align:center;font-size:9px;color:#8c909f;text-transform:uppercase;">Hit</th>
              </tr>
            </thead>
            <tbody>${predRows}</tbody>
          </table>
        </div>

        <!-- Price Chart -->
        <div style="margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="font-size:10px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;font-weight:800;">Price Chart</span>
            <div style="display:flex;gap:4px;" id="chartPeriodBtns">
              <button onclick="StockPopup.loadChart('${d.ticker}','1mo')" class="cp-btn" data-p="1mo" style="padding:2px 8px;font-size:9px;font-weight:700;border:1px solid #42475444;border-radius:4px;background:#adc6ff22;color:#adc6ff;cursor:pointer;">1M</button>
              <button onclick="StockPopup.loadChart('${d.ticker}','3mo')" class="cp-btn" data-p="3mo" style="padding:2px 8px;font-size:9px;font-weight:700;border:1px solid #42475444;border-radius:4px;background:transparent;color:#8c909f;cursor:pointer;">3M</button>
              <button onclick="StockPopup.loadChart('${d.ticker}','6mo')" class="cp-btn" data-p="6mo" style="padding:2px 8px;font-size:9px;font-weight:700;border:1px solid #42475444;border-radius:4px;background:transparent;color:#8c909f;cursor:pointer;">6M</button>
              <button onclick="StockPopup.loadChart('${d.ticker}','1y')" class="cp-btn" data-p="1y" style="padding:2px 8px;font-size:9px;font-weight:700;border:1px solid #42475444;border-radius:4px;background:transparent;color:#8c909f;cursor:pointer;">1Y</button>
            </div>
          </div>
          <div style="position:relative;background:#0a0e14;border-radius:10px;border:1px solid #42475422;overflow:hidden;">
            <canvas id="priceChart" width="452" height="160" style="width:100%;height:160px;display:block;"></canvas>
            <div id="chartTooltip" style="display:none;position:absolute;top:8px;left:8px;background:#1c2026ee;border:1px solid #42475444;border-radius:6px;padding:6px 10px;font-size:10px;color:#dfe2eb;pointer-events:none;z-index:10;"></div>
          </div>
        </div>

        ${sig ? `
        <!-- Signal Details with Explanations -->
        <div style="padding:14px;background:#10141a;border-radius:10px;border:1px solid #42475422;font-size:11px;color:#c2c6d6;line-height:1.8;">
          <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;font-weight:800;margin-bottom:8px;">Signal Analysis</div>

          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;">
            <span style="color:#8c909f;" title="The type of market event that triggered this signal">Event Type</span>
            <span style="font-weight:700;text-transform:uppercase;color:#adc6ff;">${(sig.event_type || '').replace(/_/g, ' ')}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;">
            <span style="color:#8c909f;" title="Current market regime detected from volatility, momentum, and trend analysis across 32 stocks">Regime</span>
            <span>${(sig.regime || '').replace(/_/g, ' ')}</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;">
            <span style="color:#8c909f;" title="How confident the NLP engine is about the sentiment classification (higher = more certain)">Confidence</span>
            <span>${((sig.confidence || 0) * 100).toFixed(0)}%</span>
          </div>

          ${sig.headline ? `<div style="margin-top:8px;padding:8px;background:#0a0e14;border-radius:6px;font-size:10px;color:#8c909f;font-style:italic;line-height:1.5;">${sig.headline}</div>` : ''}

          <!-- Metric Explanations -->
          <div style="margin-top:12px;padding-top:10px;border-top:1px solid #42475422;">
            <div style="font-size:8px;color:#42475488;text-transform:uppercase;letter-spacing:0.15em;font-weight:800;margin-bottom:6px;">What these numbers mean</div>
            <div style="font-size:9px;color:#8c909f88;line-height:1.7;">
              <div><span style="color:#adc6ff;font-weight:700;">Alpha Score</span> — Composite signal strength (0-100). Combines event importance, sentiment, market regime, sector momentum, relative performance, and timing. Above 60 = strong signal.</div>
              <div style="margin-top:3px;"><span style="color:#4edea3;font-weight:700;">Predicted Return</span> — Expected price move based on event type, alpha quality, stock volatility, and market conditions. Positive = expected to go up.</div>
              <div style="margin-top:3px;"><span style="color:#c2c6d6;font-weight:700;">Confidence</span> — Probability that the predicted direction is correct. 60%+ = worth watching. Based on NLP certainty and regime clarity.</div>
              <div style="margin-top:3px;"><span style="color:#8c909f;font-weight:700;">Target Price</span> — Where the stock price could reach if the prediction plays out. Entry price × (1 + predicted return).</div>
            </div>
          </div>

          <!-- Compare link -->
          <div style="margin-top:10px;text-align:center;">
            <a href="compare.html?t=${d.ticker}" style="font-size:10px;color:#adc6ff;text-decoration:none;font-weight:700;">Compare with other stocks →</a>
          </div>
        </div>` : '<div style="padding:16px;text-align:center;color:#8c909f55;font-size:12px;">No active signal — this stock has no recent news events.</div>'}

        <!-- Event Timeline -->
        <div style="margin-top:16px;">
          <div style="font-size:10px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;font-weight:800;margin-bottom:8px;">Event Timeline</div>
          <div id="popupTimeline" style="max-height:200px;overflow-y:auto;"></div>
        </div>

        <!-- Why this signal? Reasoning chain -->
        ${sig?.event_id ? `<div style="margin-top:20px;padding-top:16px;border-top:1px solid #42475422;">
          <div style="font-size:10px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;font-weight:800;margin-bottom:8px;">Why this signal?</div>
          <div id="popupReasoning" data-event-id="${sig.event_id}"><div style="text-align:center;color:#8c909f44;font-size:10px;padding:12px;">Generating reasoning...</div></div>
        </div>` : ''}

        <!-- Forensics Intelligence -->
        <div style="margin-top:20px;padding-top:16px;border-top:1px solid #42475422;">
          <div style="font-size:10px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;font-weight:800;margin-bottom:8px;">Forensics</div>
          <div id="popupForensics"><div style="text-align:center;color:#8c909f44;font-size:10px;padding:12px;">Analyzing forensics...</div></div>
        </div>

        <!-- Volume + OBV Intelligence -->
        <div style="margin-top:20px;padding-top:16px;border-top:1px solid #42475422;">
          <div style="font-size:10px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;font-weight:800;margin-bottom:8px;">Volume / OBV</div>
          <div id="popupVolume"><div style="text-align:center;color:#8c909f44;font-size:10px;padding:12px;">Loading volume analysis...</div></div>
        </div>

        <!-- Promoter Intelligence -->
        <div style="margin-top:20px;padding-top:16px;border-top:1px solid #42475422;">
          <div style="font-size:10px;color:#8c909f;text-transform:uppercase;letter-spacing:0.1em;font-weight:800;margin-bottom:8px;">Promoter Intelligence</div>
          <div id="popupPromoter"><div style="text-align:center;color:#8c909f44;font-size:10px;padding:12px;">Loading promoter data...</div></div>
        </div>
      </div>`;

    // Load chart, timeline, promoter, volume, and forensics data after DOM renders
    setTimeout(() => {
      StockPopup.loadChart(d.ticker, '1mo');
      StockPopup.loadTimeline(d.ticker);
      StockPopup.loadReasoning(sig?.event_id);
      StockPopup.loadForensics(d.ticker);
      StockPopup.loadPromoter(d.ticker);
      StockPopup.loadVolume(d.ticker);
    }, 50);
  }

  // ==== Reasoning: why-this-pick chain ====
  static async loadReasoning(eventId) {
    const host = document.getElementById('popupReasoning');
    if (!host || !eventId) return;
    let res;
    try { res = await API.get(`/signal/${encodeURIComponent(eventId)}/reasoning`); }
    catch (e) { res = null; }
    if (!res?.success) {
      host.innerHTML = '<div style="text-align:center;color:#8c909f55;font-size:11px;padding:12px">Reasoning unavailable for this signal</div>';
      return;
    }
    const r = res.data;
    const meta = r.meta || {};
    const tickerCodeColor = r.why_ticker_code === 'direct' ? '#4edea3'
                          : r.why_ticker_code === 'sector' ? '#f2c96b' : '#8c909f';
    const tickerCodeLabel = r.why_ticker_code === 'direct' ? 'DIRECT MENTION'
                          : r.why_ticker_code === 'sector' ? 'SECTOR LINK' : 'KEYWORD MATCH';

    const bullets = (r.bullets || []).map(b => {
      // Color-code the prefix "Ticker:" / "Direction:" / etc
      const m = b.match(/^([A-Za-z ]+):\s*(.*)$/);
      if (!m) return `<li style="margin-bottom:8px;color:#dfe2eb;line-height:1.5">${b}</li>`;
      const label = m[1];
      const body = m[2];
      const labelColor = {
        'Ticker': '#8eb4e0', 'Direction': '#4edea3', 'Magnitude': '#f2c96b',
        'Confidence': '#adc6ff', 'Risks': '#ffb4ab', 'Source': '#8c909f',
      }[label] || '#8c909f';
      return `<li style="margin-bottom:10px;line-height:1.55;color:#c2c6d6">
        <span style="color:${labelColor};font-weight:700;text-transform:uppercase;font-size:10px;letter-spacing:.1em">${label}</span>
        <span style="margin-left:6px">${body}</span>
      </li>`;
    }).join('');

    const risksHtml = (r.risk_factors || []).map(rk =>
      `<li style="margin-bottom:4px;color:#ffb4ab;font-size:11px;line-height:1.5">${rk}</li>`
    ).join('');

    host.innerHTML = `
      <div style="padding:12px;background:rgba(16,20,26,0.6);border:1px solid #42475422;border-radius:10px">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
          <span style="background:${tickerCodeColor}22;color:${tickerCodeColor};padding:3px 8px;border-radius:4px;font-size:9px;font-weight:700;letter-spacing:.1em">${tickerCodeLabel}</span>
          ${meta.empirical_event_hit_rate != null ? `
            <span style="background:#142a3a;color:#8eb4e0;padding:3px 8px;border-radius:4px;font-size:9px;font-weight:700">
              ${meta.event_type?.toUpperCase()} HIT-RATE: ${(meta.empirical_event_hit_rate*100).toFixed(0)}% (n=${meta.event_prior_samples})
            </span>` : ''}
          ${meta.sector ? `<span style="background:#1a2418;color:#9eddb9;padding:3px 8px;border-radius:4px;font-size:9px;font-weight:700">${meta.sector}</span>` : ''}
        </div>
        <div style="font-size:12px;color:#dfe2eb;margin-bottom:10px;font-style:italic;line-height:1.55">
          ${r.primary_driver || ''}
        </div>
        <ul style="list-style:none;padding:0;margin:0;font-size:11px">${bullets}</ul>
      </div>`;
  }

  static async loadTimeline(ticker) {
    const container = document.getElementById('popupTimeline');
    if (!container) return;
    container.innerHTML = '<div style="text-align:center;color:#8c909f44;font-size:10px;padding:12px;">Loading events...</div>';

    const res = await API.get(`/stock/${ticker}/timeline`);
    const events = res?.data || [];

    if (events.length === 0) {
      container.innerHTML = '<div style="text-align:center;color:#8c909f33;font-size:10px;padding:12px;">No events recorded for this stock yet</div>';
      return;
    }

    const maxAlpha = Math.max(...events.map(e => e.alpha_score || 0), 1);

    container.innerHTML = events.map((ev, idx) => {
      const sent = ev.sentiment || 'neutral';
      const sentColor = sent === 'bullish' ? '#4edea3' : sent === 'bearish' ? '#ffb4ab' : '#adc6ff';
      const alpha = ev.alpha_score || 0;
      const alphaPct = Math.round((alpha / maxAlpha) * 100);
      const evType = (ev.event_type || '').replace(/_/g, ' ');
      const preds = ev.predictions || {};
      const isLast = idx === events.length - 1;

      let predChips = '';
      for (const h of ['1D', '3D', '20D']) {
        const p = preds[h];
        if (p) {
          const ret = p.predicted || 0;
          const hit = p.hit;
          const retColor = ret >= 0 ? 'var(--bull)' : 'var(--bear)';
          let hitIcon = '';
          if (hit === true || hit === 1) hitIcon = '&check;';
          else if (hit === false || hit === 0) hitIcon = '&times;';
          predChips += `<span style="font-size:7px;padding:1px 4px;border-radius:3px;background:${retColor}12;color:${retColor};font-family:JetBrains Mono,monospace;">${h}:${ret >= 0 ? '+' : ''}${ret.toFixed(1)}%${hitIcon ? ' ' + hitIcon : ''}</span>`;
        }
      }

      return `
      <div style="display:flex;gap:0;padding:0;">
        <!-- Timeline spine -->
        <div style="flex-shrink:0;width:28px;display:flex;flex-direction:column;align-items:center;">
          <div style="width:8px;height:8px;border-radius:50%;background:${sentColor};flex-shrink:0;z-index:2;"></div>
          ${!isLast ? `<div style="width:1px;flex:1;background:var(--b1);min-height:24px;"></div>` : ''}
        </div>

        <!-- Content -->
        <div style="flex:1;min-width:0;padding-bottom:${isLast ? '4' : '12'}px;margin-left:4px;">
          <div style="display:flex;align-items:start;gap:8px;">
            <div style="flex:1;min-width:0;">
              <div style="font-size:10px;color:#dfe2eb;font-weight:600;line-height:1.35;margin-bottom:2px;">${(ev.headline || evType).substring(0, 70)}</div>
              <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap;">
                <span style="font-size:7px;color:#8c909f66;">${UIHelper.formatTime(ev.created_at)}</span>
                <span style="font-size:7px;padding:1px 4px;border-radius:3px;background:${sentColor}12;color:${sentColor};font-weight:700;text-transform:uppercase;">${evType}</span>
                ${predChips}
              </div>
            </div>
            <!-- Alpha bar -->
            <div style="flex-shrink:0;width:50px;text-align:right;">
              <div style="font-size:10px;font-weight:800;font-family:JetBrains Mono,monospace;color:${UIHelper.alphaColor(alpha)};margin-bottom:2px;">${alpha.toFixed(0)}</div>
              <div style="width:100%;height:3px;background:#42475422;border-radius:2px;overflow:hidden;">
                <div style="width:${alphaPct}%;height:100%;background:${UIHelper.alphaColor(alpha)};border-radius:2px;"></div>
              </div>
            </div>
          </div>
        </div>
      </div>`;
    }).join('');
  }

  static _chartData = [];

  static async loadChart(ticker, period) {
    const canvas = document.getElementById('priceChart');
    if (!canvas) return;

    // Update period button styles
    document.querySelectorAll('.cp-btn').forEach(b => {
      b.style.background = b.dataset.p === period ? 'var(--info-dim)' : 'transparent';
      b.style.color = b.dataset.p === period ? 'var(--info)' : 'var(--t3)';
    });

    const res = await API.get(`/stock/${ticker}/chart?period=${period}`);
    if (!res?.success || !res.data?.length) {
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#8c909f44';
      ctx.font = '12px Inter';
      ctx.textAlign = 'center';
      ctx.fillText('No chart data', canvas.width / 2, canvas.height / 2);
      return;
    }

    this._chartData = res.data;
    this._drawChart(canvas, res.data);

    // Hover crosshair + tooltip
    const overlay = document.createElement('canvas');
    overlay.width = canvas.width;
    overlay.height = canvas.height;
    overlay.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:5;';
    canvas.parentElement.appendChild(overlay);
    // Remove old overlays
    const oldOverlays = canvas.parentElement.querySelectorAll('canvas:not(#priceChart)');
    if (oldOverlays.length > 1) oldOverlays.forEach((o, i) => { if (i < oldOverlays.length - 1) o.remove(); });

    canvas.onmousemove = (e) => {
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const scaleX = canvas.width / rect.width;
      const cx = mx * scaleX;
      const idx = Math.round((mx / rect.width) * (this._chartData.length - 1));
      const pt = this._chartData[Math.max(0, Math.min(idx, this._chartData.length - 1))];
      if (!pt) return;

      // Draw crosshair on overlay
      const octx = overlay.getContext('2d');
      octx.clearRect(0, 0, overlay.width, overlay.height);

      const closes = this._chartData.map(d => d.close);
      const minP = Math.min(...closes), maxP = Math.max(...closes), rangeP = maxP - minP || 1;
      const py = 12 + (1 - (pt.close - minP) / rangeP) * (overlay.height - 32);

      // Vertical line
      octx.beginPath();
      octx.moveTo(cx, 0);
      octx.lineTo(cx, overlay.height);
      octx.strokeStyle = '#adc6ff22';
      octx.lineWidth = 1;
      octx.setLineDash([3, 3]);
      octx.stroke();
      octx.setLineDash([]);

      // Horizontal line
      octx.beginPath();
      octx.moveTo(0, py);
      octx.lineTo(overlay.width, py);
      octx.strokeStyle = '#adc6ff15';
      octx.stroke();

      // Crosshair dot
      octx.beginPath();
      octx.arc(cx, py, 4, 0, Math.PI * 2);
      octx.fillStyle = '#adc6ff';
      octx.fill();
      octx.beginPath();
      octx.arc(cx, py, 6, 0, Math.PI * 2);
      octx.strokeStyle = '#adc6ff44';
      octx.lineWidth = 1;
      octx.stroke();

      // Price tag on right
      octx.fillStyle = '#1c2026';
      octx.fillRect(overlay.width - 52, py - 8, 50, 16);
      octx.strokeStyle = '#42475444';
      octx.strokeRect(overlay.width - 52, py - 8, 50, 16);
      octx.font = '8px JetBrains Mono';
      octx.fillStyle = '#adc6ff';
      octx.textAlign = 'center';
      octx.fillText('₹' + pt.close, overlay.width - 27, py + 3);

      // Tooltip
      const tip = document.getElementById('chartTooltip');
      const change = idx > 0 ? pt.close - this._chartData[idx - 1].close : 0;
      const changePct = idx > 0 ? (change / this._chartData[idx - 1].close * 100) : 0;
      const changeColor = change >= 0 ? '#4edea3' : '#ffb4ab';
      tip.style.display = 'block';
      tip.style.left = Math.min(mx + 12, rect.width - 140) + 'px';
      tip.innerHTML = `
        <div style="font-weight:700;margin-bottom:3px;">${pt.date}</div>
        <div style="font-size:14px;font-weight:800;color:#dfe2eb;font-family:JetBrains Mono,monospace;">₹${pt.close.toLocaleString('en-IN')}</div>
        <div style="color:${changeColor};font-size:10px;font-weight:700;">${change >= 0 ? '+' : ''}${change.toFixed(2)} (${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%)</div>
        <div style="color:#8c909f55;font-size:8px;margin-top:2px;">H: ₹${pt.high} &nbsp; L: ₹${pt.low}</div>
        ${pt.volume ? `<div style="color:#8c909f44;font-size:8px;">Vol: ${(pt.volume/1e6).toFixed(1)}M</div>` : ''}`;
    };
    canvas.parentElement.onmouseleave = () => {
      const tip = document.getElementById('chartTooltip');
      if (tip) tip.style.display = 'none';
      const octx = overlay.getContext('2d');
      octx.clearRect(0, 0, overlay.width, overlay.height);
    };
  }

  static _drawChart(canvas, data) {
    const ctx = canvas.getContext('2d');
    const W = canvas.width;
    const H = canvas.height;
    const pad = { top: 12, bottom: 20, left: 0, right: 0 };
    const cW = W - pad.left - pad.right;
    const cH = H - pad.top - pad.bottom;

    ctx.clearRect(0, 0, W, H);

    const closes = data.map(d => d.close);
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const range = max - min || 1;

    const isUp = closes[closes.length - 1] >= closes[0];
    const lineColor = isUp ? '#4edea3' : '#ffb4ab';
    const fillTop = isUp ? 'rgba(78,222,163,' : 'rgba(255,180,171,';

    // Map points
    const points = data.map((d, i) => ({
      x: pad.left + (i / (data.length - 1)) * cW,
      y: pad.top + (1 - (d.close - min) / range) * cH
    }));

    // Gradient fill
    const grad = ctx.createLinearGradient(0, pad.top, 0, H);
    grad.addColorStop(0, fillTop + '0.25)');
    grad.addColorStop(0.5, fillTop + '0.08)');
    grad.addColorStop(1, fillTop + '0.0)');

    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 1; i < points.length; i++) {
      const cp = (points[i].x - points[i - 1].x) * 0.3;
      ctx.bezierCurveTo(
        points[i - 1].x + cp, points[i - 1].y,
        points[i].x - cp, points[i].y,
        points[i].x, points[i].y
      );
    }
    // Fill under curve
    ctx.lineTo(points[points.length - 1].x, H);
    ctx.lineTo(points[0].x, H);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 1; i < points.length; i++) {
      const cp = (points[i].x - points[i - 1].x) * 0.3;
      ctx.bezierCurveTo(
        points[i - 1].x + cp, points[i - 1].y,
        points[i].x - cp, points[i].y,
        points[i].x, points[i].y
      );
    }
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Glow effect
    ctx.shadowColor = lineColor;
    ctx.shadowBlur = 8;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Volume bars (subtle, at the bottom)
    if (data[0].volume) {
      const maxVol = Math.max(...data.map(d => d.volume));
      const barW = Math.max(1, cW / data.length - 0.5);
      data.forEach((d, i) => {
        const barH = (d.volume / maxVol) * (cH * 0.15);
        const x = pad.left + (i / (data.length - 1)) * cW - barW / 2;
        ctx.fillStyle = d.close >= (data[i - 1]?.close || d.close) ? '#4edea318' : '#ffb4ab18';
        ctx.fillRect(x, H - pad.bottom - barH, barW, barH);
      });
    }

    // Grid lines (subtle)
    ctx.strokeStyle = '#42475415';
    ctx.lineWidth = 0.5;
    for (let i = 1; i < 4; i++) {
      const y = pad.top + (cH / 4) * i;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
    }

    // Price labels
    ctx.font = '9px JetBrains Mono, monospace';
    ctx.fillStyle = '#8c909f66';
    ctx.textAlign = 'left';
    ctx.fillText('₹' + max.toFixed(0), pad.left + 4, pad.top + 10);
    ctx.fillText('₹' + min.toFixed(0), pad.left + 4, H - pad.bottom - 2);
    const midPrice = ((max + min) / 2).toFixed(0);
    ctx.fillText('₹' + midPrice, pad.left + 4, pad.top + cH / 2 + 3);

    // Date labels
    ctx.textAlign = 'center';
    ctx.fillStyle = '#8c909f55';
    ctx.fillText(data[0].date.slice(5), points[0].x + 15, H - 4);
    ctx.fillText(data[data.length - 1].date.slice(5), points[points.length - 1].x - 15, H - 4);
    if (data.length > 10) {
      const mid = Math.floor(data.length / 2);
      ctx.fillText(data[mid].date.slice(5), points[mid].x, H - 4);
    }

    // Start animated bubble on the last point
    this._animateBubble(canvas, points, lineColor);
  }

  static _bubbleAnim = null;

  static _animateBubble(canvas, points, color) {
    // Cancel previous animation
    if (this._bubbleAnim) cancelAnimationFrame(this._bubbleAnim);

    const ctx = canvas.getContext('2d');
    const last = points[points.length - 1];
    let frame = 0;

    const animate = () => {
      frame++;
      const pulse = Math.sin(frame * 0.05) * 0.5 + 0.5; // 0-1 oscillation
      const radius = 3 + pulse * 4;
      const alpha = 0.6 - pulse * 0.4;

      // Save the area around the dot and redraw
      const x = last.x;
      const y = last.y;
      const r = 14;

      // Clear just the bubble area
      ctx.save();
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.clip();
      // Redraw background
      ctx.fillStyle = '#0a0e14';
      ctx.fillRect(x - r, y - r, r * 2, r * 2);
      ctx.restore();

      // Outer glow ring
      ctx.beginPath();
      ctx.arc(x, y, radius + 2, 0, Math.PI * 2);
      ctx.strokeStyle = color + Math.round(alpha * 40).toString(16).padStart(2, '0');
      ctx.lineWidth = 1;
      ctx.stroke();

      // Pulsing ring
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.fillStyle = color + Math.round(alpha * 60).toString(16).padStart(2, '0');
      ctx.fill();

      // Core dot
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();

      // Bright center
      ctx.beginPath();
      ctx.arc(x, y, 1.2, 0, Math.PI * 2);
      ctx.fillStyle = '#ffffff';
      ctx.fill();

      this._bubbleAnim = requestAnimationFrame(animate);
    };

    animate();
  }

  // ==== Forensics Tab (Phase 1.5 v2) ====
  static async loadForensics(ticker) {
    const host = document.getElementById('popupForensics');
    if (!host) return;
    let res;
    try { res = await API.get(`/stock/${encodeURIComponent(ticker)}/forensics-full`); }
    catch (e) { res = null; }

    if (!res?.success) {
      host.innerHTML = '<div style="text-align:center;color:#8c909f55;font-size:11px;padding:12px">Forensics unavailable</div>';
      return;
    }
    const d = res.data;
    const pd = d.pump_dump || {};
    const recent = (d.recent_signals || []).filter(s => s.score != null);
    const latest = recent[0];

    const pumpColor = pd.band === 'likely_pump' ? '#ffb4ab'
                    : pd.band === 'suspicious' ? '#f2c96b' : '#4edea3';
    const manipColor = latest?.band === 'likely_manipulated' ? '#ffb4ab'
                     : latest?.band === 'unverified' ? '#f2c96b' : '#4edea3';

    // Circular gauge (pump score)
    const pumpScore = pd.pump_score || 0;
    const circ = 2 * Math.PI * 26;
    const dash = (pumpScore / 100) * circ;

    const evidenceHtml = (pd.evidence || []).map(e => `
      <div style="padding:6px 10px;border-bottom:1px solid #42475422;font-size:11px">
        <div style="color:#dfe2eb">${e.detail}</div>
        <div style="color:#8c909f;font-size:9px;margin-top:2px">+${e.score} pts · ${e.code.replace(/_/g,' ')}</div>
      </div>`).join('') || '<div style="padding:10px;color:#8c909f55;font-size:10px;text-align:center">no pump-pattern evidence</div>';

    const reasonsHtml = (latest?.reasons || []).map(r => `
      <span style="background:#3a1418;color:#ffb4ab;padding:2px 8px;border-radius:10px;font-size:9px;font-weight:700;margin:2px 2px 2px 0;display:inline-block">${r.replace(/_/g,' ')}</span>
    `).join('') || '<span style="color:#8c909f55;font-size:10px">no manipulation reasons flagged</span>';

    const recentHtml = recent.slice(0, 5).map(s => {
      const c = s.band === 'likely_manipulated' ? '#ffb4ab'
              : s.band === 'unverified' ? '#f2c96b' : '#4edea3';
      return `<div style="padding:6px 8px;border-bottom:1px solid #42475422;font-size:11px" data-event-id="${s.event_id}">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <span style="color:#dfe2eb;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(s.headline || '').slice(0,70)}</span>
          <span style="color:${c};font-weight:700;font-size:10px">${s.score}</span>
        </div>
      </div>`;
    }).join('') || '<div style="padding:10px;color:#8c909f55;font-size:10px;text-align:center">no recent signals</div>';

    host.innerHTML = `
      <div style="display:grid;grid-template-columns:1.2fr 1fr;gap:10px;margin-bottom:12px">
        <!-- Pump-dump gauge card -->
        <div style="padding:12px;background:#10141a;border:1px solid #42475422;border-radius:10px;text-align:center">
          <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Pump-Dump Risk</div>
          <div style="position:relative;width:72px;height:72px;margin:6px auto">
            <svg width="72" height="72" viewBox="0 0 72 72">
              <circle cx="36" cy="36" r="26" fill="none" stroke="#42475433" stroke-width="6"/>
              <circle id="pumpRing" cx="36" cy="36" r="26" fill="none"
                stroke="${pumpColor}" stroke-width="6" stroke-linecap="round"
                stroke-dasharray="${circ}" stroke-dashoffset="${circ}"
                transform="rotate(-90 36 36)"/>
              <text id="pumpScoreText" x="36" y="41" text-anchor="middle"
                    font-family="JetBrains Mono,monospace" font-size="20" font-weight="700"
                    fill="${pumpColor}">0</text>
            </svg>
          </div>
          <div style="font-size:10px;color:${pumpColor};text-transform:uppercase;font-weight:700">${pd.band || 'unknown'}</div>
          ${pd.liquidity_multiplier > 1 ? `<div style="font-size:9px;color:#f2c96b;margin-top:4px">small-cap ×${pd.liquidity_multiplier}</div>` : ''}
        </div>

        <!-- Manipulation card -->
        <div style="padding:12px;background:#10141a;border:1px solid #42475422;border-radius:10px">
          <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em">Latest article</div>
          <div style="font-size:24px;font-weight:700;color:${manipColor};font-family:'JetBrains Mono',monospace;margin:4px 0">
            ${latest?.score != null ? latest.score : '—'}
          </div>
          <div style="font-size:10px;color:${manipColor};text-transform:uppercase;font-weight:700;margin-bottom:6px">
            ${latest?.band?.replace(/_/g,' ') || 'no recent signal'}
          </div>
          <div>${reasonsHtml}</div>
        </div>
      </div>

      <!-- Evidence -->
      <div style="margin-bottom:12px">
        <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Pump evidence</div>
        <div style="background:#10141a;border:1px solid #42475422;border-radius:8px;max-height:180px;overflow-y:auto">${evidenceHtml}</div>
      </div>

      <!-- Recent signals -->
      <div>
        <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Recent signals (score)</div>
        <div style="background:#10141a;border:1px solid #42475422;border-radius:8px;max-height:160px;overflow-y:auto">${recentHtml}</div>
      </div>`;

    // Animate gauge fill
    if (typeof AlphaFX !== 'undefined') {
      AlphaFX.countUp('#pumpScoreText', pumpScore);
      AlphaFX.gaugeFill('#pumpRing', circ - dash, circ);
    } else {
      const ring = document.getElementById('pumpRing');
      if (ring) ring.setAttribute('stroke-dashoffset', String(circ - dash));
      const t = document.getElementById('pumpScoreText');
      if (t) t.textContent = String(pumpScore);
    }
  }

  // ==== Volume / OBV Sparkline (Phase 1.5) ====
  static async loadVolume(ticker) {
    const host = document.getElementById('popupVolume');
    if (!host) return;
    let res;
    try { res = await API.get(`/stock/${encodeURIComponent(ticker)}/volume`); } catch (e) { res = null; }
    if (!res?.success || !res.data?.series) {
      host.innerHTML = '<div style="text-align:center;color:#8c909f55;font-size:11px;padding:12px">No volume history available</div>';
      return;
    }
    const d = res.data;
    const a = d.analysis || {};
    const s = d.series || {};
    const surge = a.vol_surge_ratio || 0;
    const div = a.obv_divergence_flag;
    const divColor = div === 'bearish' ? '#ffb4ab' : div === 'bullish' ? '#4edea3' : '#8c909f';
    const surgeColor = surge >= 2.5 ? '#f2c96b' : '#8c909f';

    host.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px">
        <div style="padding:8px;background:#10141a;border-radius:8px;text-align:center">
          <div style="font-size:8px;color:#8c909f;text-transform:uppercase">Surge vs 20d</div>
          <div style="font-size:13px;font-weight:700;color:${surgeColor};font-family:'JetBrains Mono',monospace">${surge.toFixed(2)}×</div>
        </div>
        <div style="padding:8px;background:#10141a;border-radius:8px;text-align:center">
          <div style="font-size:8px;color:#8c909f;text-transform:uppercase">OBV divergence</div>
          <div style="font-size:13px;font-weight:700;color:${divColor};text-transform:uppercase">${div || 'none'}</div>
        </div>
        <div style="padding:8px;background:#10141a;border-radius:8px;text-align:center">
          <div style="font-size:8px;color:#8c909f;text-transform:uppercase">Pre-news</div>
          <div style="font-size:13px;font-weight:700;color:${a.unexplained_volume ? '#f2c96b' : '#8c909f'}">${a.unexplained_volume ? 'unexplained' : 'explained'}</div>
        </div>
      </div>
      <div style="position:relative;background:#0a0e14;border:1px solid #42475422;border-radius:8px;padding:4px">
        <canvas id="obvSpark" width="440" height="80" style="width:100%;height:80px;display:block"></canvas>
        <div style="position:absolute;top:6px;left:10px;font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em">OBV · 60d</div>
      </div>`;

    // Native canvas sparkline (no Chart.js needed for a simple line)
    const cvs = document.getElementById('obvSpark');
    if (!cvs || !s.obv || s.obv.length < 2) return;
    const ctx = cvs.getContext('2d');
    const w = cvs.width, h = cvs.height, pad = 6;
    const data = s.obv;
    const min = Math.min(...data), max = Math.max(...data);
    const range = max - min || 1;
    ctx.clearRect(0, 0, w, h);
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = div === 'bearish' ? '#ffb4ab' : div === 'bullish' ? '#4edea3' : '#adc6ff';
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = pad + (i / (data.length - 1)) * (w - pad * 2);
      const y = h - pad - ((v - min) / range) * (h - pad * 2);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    // Fill under
    ctx.lineTo(w - pad, h - pad);
    ctx.lineTo(pad, h - pad);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, (div === 'bearish' ? '#ffb4ab' : div === 'bullish' ? '#4edea3' : '#adc6ff') + '33');
    grad.addColorStop(1, 'transparent');
    ctx.fillStyle = grad;
    ctx.fill();
  }

  // ==== Promoter Intelligence (Phase 1.5) ====
  static async _ensureChartJs() {
    if (window.Chart) return true;
    if (StockPopup._chartLoadPromise) return StockPopup._chartLoadPromise;
    StockPopup._chartLoadPromise = new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js';
      s.onload = () => resolve(true);
      s.onerror = () => resolve(false);
      document.head.appendChild(s);
    });
    return StockPopup._chartLoadPromise;
  }

  static async loadPromoter(ticker) {
    const host = document.getElementById('popupPromoter');
    if (!host) return;

    // Kick off Chart.js load in parallel with data fetch
    const chartReady = StockPopup._ensureChartJs();
    let res;
    try {
      res = await API.get(`/stock/${encodeURIComponent(ticker)}/promoter/insights`);
    } catch (e) { res = null; }

    if (!res?.success || !res.data?.has_data) {
      host.innerHTML = `
        <div style="text-align:center;color:#8c909f55;font-size:11px;padding:16px;">
          No promoter data yet for ${ticker}.<br>
          <span style="font-size:9px;opacity:.7">Run the quarterly scraper or POST /api/admin/seed_promoter to populate.</span>
        </div>`;
      return;
    }

    const d = res.data;
    const cur = d.current || {};
    const quarters = d.quarters || [];
    const flags = d.red_flags || [];
    const insights = d.insights || [];
    const events = d.events || [];
    const sebi = d.sebi_disclosures || [];
    const persons = d.key_persons || [];

    const flagBadge = (f) => {
      const palette = f.severity === 'critical'
        ? { bg: '#3a1418', fg: '#ffb4ab' }
        : f.severity === 'warn'
          ? { bg: '#3a3214', fg: '#f2c96b' }
          : { bg: '#142a3a', fg: '#8eb4e0' };
      return `<span title="${(f.detail || '').replace(/"/g,'&quot;')}" style="display:inline-block;margin:2px;padding:3px 8px;border-radius:10px;background:${palette.bg};color:${palette.fg};font-size:10px;font-weight:700">${f.label}</span>`;
    };

    const statBox = (label, value, color) => `
      <div style="padding:8px;background:#10141a;border-radius:8px;text-align:center">
        <div style="font-size:8px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em">${label}</div>
        <div style="font-size:14px;font-weight:700;color:${color};font-family:'JetBrains Mono',monospace;margin-top:2px">${value == null ? '—' : value.toFixed(2) + '%'}</div>
      </div>`;

    const eventsHtml = (events.slice(0, 5).map(e => `
      <div style="padding:6px 8px;border-bottom:1px solid #42475422;font-size:11px">
        <div style="color:#8c909f;font-size:9px">${e.event_date || ''}</div>
        <div style="color:#dfe2eb">${(e.event_type || '').replace(/_/g,' ')} · ${(e.reason || '').slice(0, 80)}</div>
      </div>`).join('')) || '<div style="padding:8px;color:#8c909f55;font-size:10px;text-align:center">no recent promoter events</div>';

    const sebiHtml = (sebi.slice(0, 5).map(s => {
      const c = s.transaction_type === 'buy' ? '#4edea3' : s.transaction_type === 'sell' ? '#ffb4ab' : '#adc6ff';
      return `<div style="padding:6px 8px;border-bottom:1px solid #42475422;font-size:11px">
        <div style="color:#8c909f;font-size:9px">${s.transaction_date || ''} · ${s.disclosure_type || ''}</div>
        <div style="color:${c}">${s.transaction_type || ''} ${s.quantity ? '· ' + Number(s.quantity).toLocaleString() + ' sh' : ''} ${(s.person_name || '')}</div>
      </div>`;
    }).join('')) || '<div style="padding:8px;color:#8c909f55;font-size:10px;text-align:center">no SEBI disclosures in window</div>';

    const personsHtml = (persons.slice(0, 6).map(p => `
      <div style="padding:4px 8px;font-size:11px">
        <span style="color:#dfe2eb">${p.person_name || ''}</span>
        <span style="color:#8c909f;font-size:9px;margin-left:6px">${p.role || ''}</span>
      </div>`).join('')) || '';

    host.innerHTML = `
      <!-- Insights narrative -->
      <div style="padding:10px;background:#10141a;border-radius:8px;border:1px solid #42475422;margin-bottom:12px;font-size:11px;color:#c2c6d6;line-height:1.7">
        ${insights.map(i => `<div>• ${i}</div>`).join('')}
      </div>

      <!-- Red flags -->
      ${flags.length ? `<div style="margin-bottom:12px">${flags.map(flagBadge).join('')}</div>` : ''}

      <!-- Current snapshot stats -->
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:12px">
        ${statBox('Promoter', cur.promoter_pct, '#adc6ff')}
        ${statBox('Pledge', cur.promoter_pledge_pct, (cur.promoter_pledge_pct || 0) > 20 ? '#ffb4ab' : '#8c909f')}
        ${statBox('FII', cur.fii_pct, '#4edea3')}
        ${statBox('DII', cur.dii_pct, '#f2c96b')}
      </div>

      <!-- Charts row -->
      <div style="display:grid;grid-template-columns:1fr 1.4fr;gap:10px;margin-bottom:12px">
        <div style="padding:8px;background:#0a0e14;border:1px solid #42475422;border-radius:8px">
          <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Current breakdown</div>
          <canvas id="promoterDonut" width="180" height="160" style="width:100%;max-height:160px"></canvas>
        </div>
        <div style="padding:8px;background:#0a0e14;border:1px solid #42475422;border-radius:8px">
          <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">8-quarter history</div>
          <canvas id="promoterHistory" width="280" height="160" style="width:100%;max-height:160px"></canvas>
        </div>
      </div>

      <!-- Recent events + SEBI + persons -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px">
        <div>
          <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Promoter events</div>
          <div style="background:#10141a;border:1px solid #42475422;border-radius:8px;max-height:140px;overflow-y:auto">${eventsHtml}</div>
        </div>
        <div>
          <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">SEBI disclosures</div>
          <div style="background:#10141a;border:1px solid #42475422;border-radius:8px;max-height:140px;overflow-y:auto">${sebiHtml}</div>
        </div>
      </div>

      ${persons.length ? `
      <div>
        <div style="font-size:9px;color:#8c909f;text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Key persons</div>
        <div style="background:#10141a;border:1px solid #42475422;border-radius:8px;padding:4px">${personsHtml}</div>
      </div>` : ''}
    `;

    const ok = await chartReady;
    if (!ok || !window.Chart) return;

    const donutCanvas = document.getElementById('promoterDonut');
    if (donutCanvas) {
      const others = Math.max(0, 100 - (cur.promoter_pct || 0) - (cur.fii_pct || 0) - (cur.dii_pct || 0));
      new Chart(donutCanvas, {
        type: 'doughnut',
        data: {
          labels: ['Promoter', 'FII', 'DII', 'Public/Other'],
          datasets: [{
            data: [cur.promoter_pct || 0, cur.fii_pct || 0, cur.dii_pct || 0, others],
            backgroundColor: ['#adc6ff', '#4edea3', '#f2c96b', '#42475466'],
            borderColor: '#10141a',
            borderWidth: 2,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { position: 'bottom', labels: { color: '#c2c6d6', font: { size: 10 }, boxWidth: 10 } },
            tooltip: { callbacks: { label: (c) => `${c.label}: ${(+c.parsed).toFixed(2)}%` } },
          },
        },
      });
    }

    const histCanvas = document.getElementById('promoterHistory');
    if (histCanvas && quarters.length) {
      new Chart(histCanvas, {
        type: 'line',
        data: {
          labels: quarters.map(q => (q.quarter_end || '').slice(0, 7)),
          datasets: [
            {
              label: 'Promoter %',
              data: quarters.map(q => q.promoter_pct),
              borderColor: '#adc6ff',
              backgroundColor: '#adc6ff22',
              tension: 0.25, pointRadius: 3, borderWidth: 2,
            },
            {
              label: 'Pledge %',
              data: quarters.map(q => q.promoter_pledge_pct),
              borderColor: '#ffb4ab',
              backgroundColor: '#ffb4ab22',
              tension: 0.25, pointRadius: 3, borderWidth: 2,
            },
            {
              label: 'FII %',
              data: quarters.map(q => q.fii_pct),
              borderColor: '#4edea3',
              backgroundColor: '#4edea322',
              tension: 0.25, pointRadius: 2, borderWidth: 1.5,
            },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: '#c2c6d6', font: { size: 10 }, boxWidth: 10 } },
          },
          scales: {
            x: { ticks: { color: '#8c909f', font: { size: 9 } }, grid: { color: '#42475422' } },
            y: { ticks: { color: '#8c909f', font: { size: 9 }, callback: (v) => v + '%' }, grid: { color: '#42475422' } },
          },
        },
      });
    }
  }
}

// Global click handler — any element with data-ticker opens the popup
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-ticker]');
  if (el) {
    e.preventDefault();
    StockPopup.show(el.dataset.ticker);
  }
});


// ============ MARKET TICKER ============
class MarketTicker {
  static _el = null;
  static _clockInterval = null;
  static _dataInterval = null;

  static async init() {
    // Avoid double-init
    if (document.getElementById('market-ticker-bar')) return;

    const bar = document.createElement('div');
    bar.id = 'market-ticker-bar';
    bar.className = 'market-ticker-bar';
    bar.innerHTML = `
      <div class="ticker-live-badge">
        <span class="ticker-live-dot"></span>LIVE
      </div>
      <div class="ticker-scroll-container">
        <div class="ticker-scroll-track" id="ticker-scroll-track">
          <div class="ticker-inner" style="padding:0 20px;color:rgba(194,198,214,0.4);font-size:10px;">
            Fetching market data&hellip;
          </div>
        </div>
      </div>
      <div class="ticker-timestamp" id="ticker-timestamp">&mdash;</div>
    `;

    const main = document.querySelector('main.main-content');
    if (main) main.insertBefore(bar, main.firstChild);
    else document.body.insertBefore(bar, document.body.firstChild);
    this._el = bar;

    await this._render();
    this._startClock();
    this._dataInterval = setInterval(() => {
      LiveData._cache['market-indices'] = null; // force refresh
      this._render();
    }, 30000);
  }

  static async _render() {
    const data = await LiveData.getMarketIndices();
    const track = document.getElementById('ticker-scroll-track');
    if (!track || !data || data.length === 0) return;

    // Make pulse available globally
    if (typeof MarketPulse !== 'undefined') MarketPulse.analyze(data);

    const items = data.map(idx => {
      const pos = idx.change_pct >= 0;
      const color = pos ? '#4edea3' : '#ffb4ab';
      const arrow = pos ? '▲' : '▼';
      const price = idx.price > 0
        ? idx.price.toLocaleString('en-IN', { maximumFractionDigits: idx.price > 1000 ? 0 : 2 })
        : '—';
      return `<span class="ticker-item">
        <span class="ticker-index-name">${idx.short}</span>
        <span class="ticker-price" style="color:${color}">${price}</span>
        <span class="ticker-change" style="color:${color}">${arrow}&nbsp;${Math.abs(idx.change_pct).toFixed(2)}%</span>
      </span>`;
    }).join('');

    // Duplicate for seamless infinite scroll
    track.innerHTML = `<div class="ticker-inner">${items}</div><div class="ticker-inner" aria-hidden="true">${items}</div>`;
  }

  static _startClock() {
    const el = document.getElementById('ticker-timestamp');
    const tick = () => {
      if (el) el.textContent = new Date().toLocaleTimeString('en-IN', {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
      });
    };
    tick();
    this._clockInterval = setInterval(tick, 1000);
  }
}

// Auto-init MarketTicker on every page
document.addEventListener('DOMContentLoaded', () => MarketTicker.init());

// ============ MARKET PULSE ANALYSER ============
class MarketPulse {
  static latest = null; // Stores the most recent analysis result

  static analyze(indices) {
    if (!indices || !indices.length) return null;

    const get = (short) => indices.find(i => i.short === short) || null;
    const nifty     = get('NIFTY 50');
    const sensex    = get('SENSEX');
    const bankNifty = get('BANK NIFTY');
    const niftyIT   = get('NIFTY IT');
    const vix       = get('INDIA VIX');
    const usdInr    = get('USD/INR');

    const niftyChg    = nifty?.change_pct    || 0;
    const sensexChg   = sensex?.change_pct   || 0;
    const bankChg     = bankNifty?.change_pct || 0;
    const itChg       = niftyIT?.change_pct  || 0;

    // Weighted average of 4 broad indices for overall market direction
    const avgChg = (niftyChg * 0.40 + sensexChg * 0.35 + bankChg * 0.15 + itChg * 0.10);

    // Derive trend label + strength
    let trendKey, trendLabel, trendColor, trendEmoji;
    if      (avgChg >=  1.5) { trendKey = 'strong_bull'; trendLabel = 'STRONG BULL';  trendColor = '#4edea3'; trendEmoji = '▲▲'; }
    else if (avgChg >=  0.4) { trendKey = 'bull';        trendLabel = 'BULLISH';      trendColor = '#4edea3'; trendEmoji = '▲';  }
    else if (avgChg >= -0.4) { trendKey = 'sideways';    trendLabel = 'SIDEWAYS';     trendColor = '#adc6ff'; trendEmoji = '→';  }
    else if (avgChg >= -1.5) { trendKey = 'bear';        trendLabel = 'BEARISH';      trendColor = '#ffb4ab'; trendEmoji = '▼';  }
    else                     { trendKey = 'strong_bear'; trendLabel = 'STRONG BEAR';  trendColor = '#ffb4ab'; trendEmoji = '▼▼'; }

    // VIX interpretation
    const vixVal = vix?.price || 0;
    let fearLabel, fearColor;
    if      (vixVal < 12) { fearLabel = 'Greed Zone';      fearColor = '#4edea3'; }
    else if (vixVal < 18) { fearLabel = 'Normal';           fearColor = '#adc6ff'; }
    else if (vixVal < 24) { fearLabel = 'Elevated Fear';    fearColor = '#f9d423'; }
    else if (vixVal < 30) { fearLabel = 'High Fear';        fearColor = '#ffb4ab'; }
    else                  { fearLabel = 'Extreme Fear';      fearColor = '#ff516a'; }

    // Breadth: how many of the 4 major indices are green
    const changes = [niftyChg, sensexChg, bankChg, itChg];
    const greenCount = changes.filter(c => c > 0).length;
    const breadthPct = Math.round(greenCount / changes.length * 100);

    // Rupee move
    const usdChg  = usdInr?.change_pct || 0;
    const rupeeDir = usdChg > 0.1 ? 'weakening' : usdChg < -0.1 ? 'strengthening' : 'stable';

    // FII proxy: if broad market is down but VIX is up a lot, likely FII selling
    const vixChg  = vix?.change_pct || 0;
    const fiiSell = trendKey.includes('bear') && vixChg > 5;

    const result = {
      trendKey, trendLabel, trendColor, trendEmoji,
      avgChg: +avgChg.toFixed(2),
      niftyChg:  +niftyChg.toFixed(2),  niftyPrice:  nifty?.price  || 0,
      sensexChg: +sensexChg.toFixed(2), sensexPrice: sensex?.price || 0,
      bankChg:   +bankChg.toFixed(2),   bankPrice:   bankNifty?.price || 0,
      itChg:     +itChg.toFixed(2),     itPrice:     niftyIT?.price  || 0,
      vixVal:    +vixVal.toFixed(2),    vixChg:      +vixChg.toFixed(2),
      fearLabel, fearColor,
      breadthPct, greenCount,
      rupeeDir, usdChg: +usdChg.toFixed(2),
      fiiSell,
    };

    MarketPulse.latest = result;
    return result;
  }

  /** Plain-English one-liner about today's market for any tooltip or summary */
  static headline(pulse) {
    if (!pulse) return '';
    const { trendLabel, niftyChg, vixVal, fearLabel, breadthPct, rupeeDir, fiiSell } = pulse;
    const niftyStr  = `NIFTY ${niftyChg >= 0 ? '+' : ''}${niftyChg}%`;
    const breadthStr = `${pulse.greenCount}/4 indices positive`;
    const vixStr    = `VIX ${vixVal} (${fearLabel})`;
    const fiiStr    = fiiSell ? ' — likely FII outflows.' : '.';
    return `${trendLabel} session: ${niftyStr}, ${breadthStr}. ${vixStr}${fiiStr} Rupee ${rupeeDir}.`;
  }

  /** Context string for embedding into company suggestions */
  static forSignal(pulse, sentiment) {
    if (!pulse) return '';
    const { trendKey, niftyChg, fearLabel, vixVal } = pulse;
    const bull = sentiment === 'bullish';
    if (trendKey === 'strong_bear' || trendKey === 'bear') {
      return bull
        ? `Despite broad market selling (NIFTY ${niftyChg}%), this company-specific catalyst may deliver counter-trend alpha — stock-specific events outperform in down markets.`
        : `Bear market (NIFTY ${niftyChg}%) amplifies downside; VIX ${vixVal} signals ${fearLabel.toLowerCase()}.`;
    }
    if (trendKey === 'strong_bull' || trendKey === 'bull') {
      return bull
        ? `Bullish market tailwind (NIFTY +${niftyChg}%) reinforces the upside thesis.`
        : `Even in an up market (NIFTY +${niftyChg}%) this bearish catalyst warrants caution — company-specific risk.`;
    }
    // Sideways
    return bull
      ? `Sideways market (NIFTY ${niftyChg}%) means this catalyst is the primary driver — less noise from macro.`
      : `Choppy market reduces conviction on direction.`;
  }
}

// ============ LOCAL STORAGE ============
class Storage {
  static get(key) {
    try {
      const item = localStorage.getItem(key);
      return item ? JSON.parse(item) : null;
    } catch { return null; }
  }

  static set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); }
    catch { /* quota exceeded */ }
  }

  static remove(key) { localStorage.removeItem(key); }
  static clear() { localStorage.clear(); }
}

// ============ MOBILE NAV ============
class MobileNav {
  static _backdrop = null;

  static init() {
    const sidebar = document.querySelector('.sidebar');
    const main    = document.querySelector('.main-content');
    if (!sidebar || !main) return;

    // Inject backdrop
    if (!document.querySelector('.sidebar-backdrop')) {
      const bd = document.createElement('div');
      bd.className = 'sidebar-backdrop';
      bd.addEventListener('click', () => MobileNav.close());
      document.body.appendChild(bd);
      this._backdrop = bd;
    }

    // Inject mobile top bar — defer so ticker bar inserts first
    if (!main.querySelector('.mobile-top-bar')) {
      const pageTitle = document.title.split('|')[1]?.trim() || 'EventAlpha';
      const bar = document.createElement('div');
      bar.className = 'mobile-top-bar';
      bar.innerHTML = `
        <button class="mobile-menu-btn" onclick="MobileNav.toggle()" aria-label="Open navigation">
          <span class="material-symbols-outlined" style="font-size:20px;">menu</span>
        </button>
        <span class="mobile-top-bar__brand">${pageTitle}</span>
        <div style="width:36px;"></div>
      `;
      // Insert after ticker bar if it exists, else at start
      const ticker = main.querySelector('#market-ticker-bar');
      if (ticker) ticker.after(bar);
      else main.insertBefore(bar, main.firstChild);
    }

    // Close sidebar when nav link is clicked (mobile)
    sidebar.querySelectorAll('.sidebar-link').forEach(link => {
      link.addEventListener('click', () => MobileNav.close());
    });
  }

  static toggle() {
    const sidebar = document.querySelector('.sidebar');
    if (!sidebar) return;
    if (sidebar.classList.contains('mobile-open')) {
      this.close();
    } else {
      this.open();
    }
  }

  static open() {
    const sidebar = document.querySelector('.sidebar');
    const bd = document.querySelector('.sidebar-backdrop');
    sidebar?.classList.add('mobile-open');
    bd?.classList.add('active');
    document.body.style.overflow = 'hidden';
  }

  static close() {
    const sidebar = document.querySelector('.sidebar');
    const bd = document.querySelector('.sidebar-backdrop');
    sidebar?.classList.remove('mobile-open');
    bd?.classList.remove('active');
    document.body.style.overflow = '';
  }
}

// Auto-init MobileNav on every page
document.addEventListener('DOMContentLoaded', () => MobileNav.init());

// Auto-load AlphaFX (GSAP-based animations) — adds itself once
(function autoloadFx() {
  if (typeof document === 'undefined') return;
  // Skip if already loaded (some pages may include fx.js directly)
  if (document.querySelector('script[data-fx-autoload="1"]')) return;
  if (window.AlphaFX) return;
  const s = document.createElement('script');
  s.src = './shared/js/fx.js';
  s.dataset.fxAutoload = '1';
  s.defer = true;
  document.head.appendChild(s);
})();


// ============ JARGON ANNOTATOR (Phase 1.5) ============
// Scans rendered text for glossary terms and wraps them in clickable chips.
// Uses a simple longest-match scan — good enough for ~300 terms.
class Jargon {
  static _terms = null;
  static _cache = new Map();
  static _loading = null;

  static async _loadTerms() {
    if (this._terms) return this._terms;
    if (this._loading) return this._loading;
    this._loading = API.get('/glossary/batch').then(res => {
      const list = Array.isArray(res?.data) ? res.data : [];
      list.sort((a, b) => b.length - a.length); // longest first
      this._terms = list;
      return list;
    }).catch(() => { this._terms = []; return []; });
    return this._loading;
  }

  static async lookup(term) {
    const key = term.toLowerCase();
    if (this._cache.has(key)) return this._cache.get(key);
    try {
      const r = await API.get(`/glossary/${encodeURIComponent(term)}`);
      if (r?.success) { this._cache.set(key, r.data); return r.data; }
    } catch (e) { /* ignore */ }
    return null;
  }

  static async annotate(root) {
    if (!root) return;
    const terms = await this._loadTerms();
    if (!terms.length) return;

    // Build one regex of alternations, case-insensitive, word-boundary-ish
    const escaped = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    const re = new RegExp(`\\b(?:${escaped.join('|')})\\b`, 'gi');

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => {
        if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        const p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        const tag = p.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'INPUT' || tag === 'TEXTAREA') return NodeFilter.FILTER_REJECT;
        if (p.classList.contains('jargon-term')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });

    const pending = [];
    while (walker.nextNode()) pending.push(walker.currentNode);

    for (const node of pending) {
      const text = node.nodeValue;
      re.lastIndex = 0;
      if (!re.test(text)) continue;
      re.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let last = 0, m;
      while ((m = re.exec(text)) !== null) {
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        const span = document.createElement('span');
        span.className = 'jargon-term';
        span.textContent = m[0];
        span.dataset.term = m[0];
        span.style.cssText = 'border-bottom:1px dotted #8eb4e0;cursor:help;';
        span.addEventListener('click', (ev) => { ev.preventDefault(); Jargon.show(m[0], ev); });
        frag.appendChild(span);
        last = m.index + m[0].length;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    }
  }

  static async show(term, ev) {
    let pop = document.getElementById('jargon-popover');
    if (!pop) {
      pop = document.createElement('div');
      pop.id = 'jargon-popover';
      pop.style.cssText = 'position:absolute;z-index:10000;background:#1b2030;color:#e7ecf5;padding:12px 14px;border:1px solid #2a3142;border-radius:8px;max-width:340px;font-size:13px;box-shadow:0 6px 24px rgba(0,0,0,0.4);';
      document.body.appendChild(pop);
      document.addEventListener('click', (e) => { if (!pop.contains(e.target) && !e.target.classList.contains('jargon-term')) pop.style.display = 'none'; });
    }
    pop.innerHTML = '<em style="opacity:.6">loading…</em>';
    pop.style.display = 'block';
    if (ev) {
      const r = ev.target.getBoundingClientRect();
      pop.style.top = (window.scrollY + r.bottom + 6) + 'px';
      pop.style.left = Math.min(window.scrollX + r.left, window.innerWidth - 360) + 'px';
    }
    const entry = await Jargon.lookup(term);
    if (!entry) { pop.innerHTML = `<b>${term}</b><br><span style="opacity:.7">definition unavailable</span>`; return; }
    const ex = entry.one_line_example ? `<div style="margin-top:6px;opacity:.75;font-style:italic">e.g. ${entry.one_line_example}</div>` : '';
    const auto = entry.verified_by === 'llm' ? '<span style="font-size:11px;opacity:.6;margin-left:6px">(auto-defined)</span>' : '';
    pop.innerHTML = `<b>${entry.term || term}</b>${auto}<div style="margin-top:4px">${entry.plain_english || ''}</div>${ex}`;
  }
}

// ============ FORENSIC BADGE HELPER ============
class ForensicBadge {
  static render(score, band) {
    if (score == null) return '';
    const palette = band === 'likely_manipulated'
      ? { bg: '#3a1418', fg: '#f26b6b', label: 'Likely manipulated' }
      : band === 'unverified'
        ? { bg: '#3a3214', fg: '#f2c96b', label: 'Unverified' }
        : { bg: '#14331f', fg: '#2dd4aa', label: 'Clean' };
    return `<span class="forensic-badge" data-score="${score}" style="background:${palette.bg};color:${palette.fg};padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">${palette.label} · ${score}</span>`;
  }
}

// ============ PRIORITY-BOOST CHIP ============
class PriorityChip {
  static render(firstSeenSource, edgeMinutes) {
    if (!firstSeenSource) return '';
    const social = ['reddit', 'twitter', 'telegram'].some(p => String(firstSeenSource).startsWith(p));
    if (!social) return '';
    const edge = edgeMinutes ? ` · ${Math.round(edgeMinutes)}m ahead` : '';
    return `<span style="background:#142a3a;color:#8eb4e0;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">⚡ ${firstSeenSource}${edge}</span>`;
  }
}

// ============ COMPLIANCE / SEBI DISCLAIMER ============
// Auto-injected on every page that loads app.js. Renders a fixed footer ribbon
// + a one-time modal on first visit so retail users see the "this is not advice"
// notice before acting on any signal. Required posture for an India-facing
// research/data product without an SEBI Research Analyst registration.
(function injectDisclaimer() {
  if (typeof document === 'undefined') return;
  const FOOTER_ID = 'sebi-disclaimer-footer';
  const MODAL_ID  = 'sebi-disclaimer-modal';
  const ACK_KEY   = 'sebi_ack_v1';

  function buildFooter() {
    if (document.getElementById(FOOTER_ID)) return;
    const el = document.createElement('div');
    el.id = FOOTER_ID;
    el.style.cssText = [
      'position:fixed','left:0','right:0','bottom:0','z-index:9998',
      'background:rgba(10,14,20,0.94)','color:#8c909f','font-size:10px',
      'padding:5px 12px','border-top:1px solid rgba(140,144,159,0.15)',
      'text-align:center','line-height:1.4','backdrop-filter:blur(6px)',
      'pointer-events:auto','font-family:sans-serif',
    ].join(';');
    el.innerHTML =
      '<span style="opacity:0.9">For information only. <strong>Not investment advice.</strong> ' +
      'AlphaEvent is not a SEBI-registered Research Analyst. ' +
      'Signals reflect model output, not a recommendation. Trade at your own risk.</span> ' +
      '<a href="#" id="sebi-disclaimer-more" style="color:#8eb4e0;margin-left:6px;text-decoration:underline">Read full</a>';
    document.body.appendChild(el);
    document.getElementById('sebi-disclaimer-more').addEventListener('click', (e) => {
      e.preventDefault();
      showModal(true);
    });
    // Push body content above the fixed footer
    document.body.style.paddingBottom = (parseInt(getComputedStyle(document.body).paddingBottom) || 0) + 26 + 'px';
  }

  function showModal(force) {
    try { if (!force && localStorage.getItem(ACK_KEY) === '1') return; } catch (_) {}
    if (document.getElementById(MODAL_ID)) return;
    const ov = document.createElement('div');
    ov.id = MODAL_ID;
    ov.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;padding:20px;font-family:sans-serif;';
    ov.innerHTML =
      '<div style="max-width:480px;background:#0f1420;color:#cbd0dc;border:1px solid rgba(140,144,159,0.2);border-radius:14px;padding:22px;line-height:1.55;font-size:13px">' +
      '<h3 style="margin:0 0 10px 0;color:#fff;font-size:16px;font-weight:700">Before you proceed</h3>' +
      '<p style="margin:0 0 8px 0">AlphaEvent is a market intelligence dashboard that surfaces news-driven trading signals from public Indian-market data.</p>' +
      '<p style="margin:0 0 8px 0">It is <strong>not</strong> investment advice. AlphaEvent is not a SEBI-registered Research Analyst or Investment Advisor. Signals are computed from model heuristics over public news; outcomes are never guaranteed.</p>' +
      '<p style="margin:0 0 14px 0">By continuing you confirm you understand that any trading decision is yours alone.</p>' +
      '<button id="sebi-disclaimer-ok" style="background:#2dd4aa;color:#0a0e14;border:0;padding:10px 18px;border-radius:8px;font-weight:700;cursor:pointer">I understand</button>' +
      '</div>';
    document.body.appendChild(ov);
    document.getElementById('sebi-disclaimer-ok').addEventListener('click', () => {
      try { localStorage.setItem(ACK_KEY, '1'); } catch (_) {}
      ov.remove();
    });
  }

  function init() {
    buildFooter();
    showModal(false);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

// ============ EXPORTS ============
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { Router, API, LiveData, MockData, UIHelper, Storage, DataRefresher, Jargon, ForensicBadge, PriorityChip };
}
