"""TickerWave Public Data API — v1.

Mount via:  api_public.register(app, get_db)

Positioning: TickerWave is a DATA PLATFORM for Indian markets. This blueprint
is the externally documented surface that establishes that positioning. Every
endpoint here:

  - Lives under /api/v1/*  (versioned, stable)
  - Returns JSON in a consistent envelope: {data, count, as_of}
  - Uses HTTP status codes for errors (not a 200 + {success:false})
  - Documents response shape in app/api-docs.html
  - Is rate-limited per IP via flask-limiter
  - Is thin: wraps existing endpoints; never reimplements logic

Backwards compatibility: existing routes under /api/* are NOT changed. v1 is
purely additive. If we break a v1 contract we ship v2 alongside.

Auth: free tier is unauthenticated with a per-IP rate limit. Future paid tier
will accept ``X-API-Key`` header; the optional `_resolve_api_key()` helper
already extracts it so we can wire quotas later without changing routes.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from flask import Blueprint, jsonify, request

from cache_util import ttl_cache

logger = logging.getLogger(__name__)

bp = Blueprint("api_public", __name__)

# Filled in by register()
_get_db: Optional[Callable] = None
_api_version = "v1"
_started_at = datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Response helpers — consistent envelope across every endpoint.
# ---------------------------------------------------------------------------

def _ok(data, **extra):
    """Standard 200 OK response.

    Shape: {data, count, as_of, ...extra}.  `count` is added automatically
    when data is a list. `as_of` is the server clock at response time so
    clients can detect staleness without parsing every record.
    """
    body = {"data": data, "as_of": datetime.utcnow().isoformat() + "Z"}
    if isinstance(data, list):
        body["count"] = len(data)
    body.update(extra)
    return jsonify(body), 200


def _err(status: int, code: str, message: str, **extra):
    """Standard error response with proper HTTP status."""
    body = {"error": {"code": code, "message": message}}
    body.update(extra)
    return jsonify(body), status


def _query_int(name: str, default: int, *, min_v: int = 1, max_v: int = 500) -> int:
    """Parse an int query param with clamping; returns default on garbage."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(min_v, min(max_v, v))


def _query_float(name: str, default: float) -> float:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _resolve_api_key() -> Optional[str]:
    """Future paid-tier hook. Returns the key string or None.  Currently
    free-tier with per-IP rate limit; we just read the header so audit logs
    can show whether traffic is anonymous or keyed."""
    return (request.headers.get("X-API-Key") or "").strip() or None


# ---------------------------------------------------------------------------
# Index — the entry point an analyst hits when they discover /api/v1
# ---------------------------------------------------------------------------

@bp.route("/api/v1", methods=["GET"])
@bp.route("/api/v1/", methods=["GET"])
def v1_index():
    """List every documented endpoint. Lets clients auto-discover without
    parsing the HTML docs page."""
    return _ok({
        "name": "TickerWave Public Data API",
        "version": _api_version,
        "docs": "/api-docs.html",
        "rate_limit": "60 requests/minute per IP (free tier)",
        "started_at": _started_at,
        "endpoints": [
            {"path": "/api/v1/movers",       "desc": "Top NSE movers (gainers, losers, volume, volatility)"},
            {"path": "/api/v1/news",         "desc": "India market news feed with quality + sentiment + impact tier"},
            {"path": "/api/v1/events",       "desc": "Structured market events (earnings, M&A, policy, IPO, insider, etc.)"},
            {"path": "/api/v1/earnings",     "desc": "Upcoming earnings calendar (forward-looking only)"},
            {"path": "/api/v1/ipos",         "desc": "Upcoming and recent IPOs"},
            {"path": "/api/v1/commodities",  "desc": "Commodity prices + sector equity-impact narrative"},
            {"path": "/api/v1/sectors",      "desc": "Sector sentiment breakdown across active signals"},
            {"path": "/api/v1/stocks/{ticker}",              "desc": "Stock snapshot: profile, price, technicals, fundamentals score"},
            {"path": "/api/v1/stocks/{ticker}/profile",      "desc": "Company profile, sector, industry, market cap"},
            {"path": "/api/v1/stocks/{ticker}/financials",   "desc": "Quarterly + annual income / balance / cashflow"},
            {"path": "/api/v1/stocks/{ticker}/technicals",   "desc": "RSI(14), MA50, MA200, trend label, 52w range"},
            {"path": "/api/v1/stocks/{ticker}/fundamentals", "desc": "F-score (0-100) with positives, red flags, component breakdown"},
            {"path": "/api/v1/stocks/{ticker}/screener",     "desc": "Screener.in snapshot fallback: quarterly P&L, annual P&L, balance sheet, ratios, pros/cons"},
            {"path": "/api/v1/filings",                      "desc": "Searchable BSE/NSE filings archive (PDFs + extracted text)"},
            {"path": "/api/v1/filings/{id}",                 "desc": "Single filing detail with parsed numbers"},
            {"path": "/api/v1/earnings/reactions/{ticker}",  "desc": "Historical earnings dates with T+1/T+3/T+5 price reactions"},
            {"path": "/api/v1/events/{event_id}/related",    "desc": "Related events for an anchor (same ticker ± window_days)"},
            {"path": "/api/v1/smart-money/{ticker}",         "desc": "Longitudinal SAST/PIT insider + promoter activity"},
            {"path": "/api/v1/concalls/search",              "desc": "Full-text search across indexed concall transcripts"},
            {"path": "/api/v1/backtest",                     "desc": "Event-window backtest: gap, fade probability, 5-day drift for (ticker, event_type)"},
            {"path": "/api/v1/sla",                          "desc": "Alert delivery latency: median, p95, p99 across stages"},
            {"path": "/api/v1/alerts/log",                   "desc": "Public audit trail of high-impact alerts: publish time, classify latency, delivery latency, classification"},
            {"path": "/api/v1/earnings/forecast",             "desc": "List of all upcoming earnings forecasts (sortable, filterable by predicted_hit)"},
            {"path": "/api/v1/earnings/forecast/{ticker}",    "desc": "Next-quarter earnings forecast: EPS, hit/miss probability, entry window, top drivers"},
            {"path": "/api/v1/earnings/forecast/{ticker}/history", "desc": "Past forecasts for accuracy review"},
        ],
    })


# ---------------------------------------------------------------------------
# Movers — calls /api/movers/spike under the hood, returns clean shape
# ---------------------------------------------------------------------------

@bp.route("/api/v1/movers", methods=["GET"])
@ttl_cache(seconds=60)
def v1_movers():
    """Top NSE movers.

    Query params:
      mode    = gainers | losers | volume | unusual | volatile   (default: gainers)
      limit   = 1..100                                            (default: 25)
      min_pct = 0..50  (only applies when mode in [gainers, losers, volatile])
    """
    mode = (request.args.get("mode") or "gainers").lower()
    limit = _query_int("limit", 25, min_v=1, max_v=100)
    min_pct = _query_float("min_pct", 0.0)

    # Reuse the internal endpoint's data (same TTL cache).
    from api_v2 import _MOVERS_CACHE, _MOVERS_TTL_SECS  # type: ignore
    # Force the upstream endpoint to populate its cache if cold.
    if (time.time() - _MOVERS_CACHE.get("_ts", 0)) >= _MOVERS_TTL_SECS:
        # Synthetically trigger via internal call; failing that, we'll just
        # have an empty list and the caller can retry.
        try:
            from flask import current_app
            with current_app.test_request_context("/api/movers/spike?min_pct=0&limit=200"):
                from api_v2 import movers_spike  # type: ignore
                movers_spike()
        except Exception as e:
            logger.warning(f"v1_movers warm-up failed: {e}")

    rows = list(_MOVERS_CACHE.get("payload") or [])
    if not rows:
        return _ok([])

    if mode == "gainers":
        rows = [r for r in rows if (r.get("pct_change") or 0) > min_pct]
        rows.sort(key=lambda r: r.get("pct_change") or 0, reverse=True)
    elif mode == "losers":
        rows = [r for r in rows if (r.get("pct_change") or 0) < -min_pct]
        rows.sort(key=lambda r: r.get("pct_change") or 0)
    elif mode == "volume":
        rows = [r for r in rows if r.get("turnover_cr") is not None]
        rows.sort(key=lambda r: r.get("turnover_cr") or 0, reverse=True)
    elif mode == "unusual":
        rows = [r for r in rows if (r.get("vol_multiple") or 0) >= 1.5]
        rows.sort(key=lambda r: r.get("vol_multiple") or 0, reverse=True)
    elif mode == "volatile":
        rows = [r for r in rows if abs(r.get("pct_change") or 0) >= min_pct]
        rows.sort(key=lambda r: abs(r.get("pct_change") or 0), reverse=True)
    else:
        return _err(400, "invalid_mode", f"mode must be one of: gainers, losers, volume, unusual, volatile. Got: {mode!r}")

    return _ok(rows[:limit], mode=mode)


# ---------------------------------------------------------------------------
# News — India-focused, quality-filtered
# ---------------------------------------------------------------------------

@bp.route("/api/v1/news", methods=["GET"])
@ttl_cache(seconds=30)
def v1_news():
    """India market news feed with classifier enrichment.

    Query params:
      category    = corporate | earnings | policy | ipo | geopolitical | commodity | social
      ticker      = filter by ticker
      min_quality = generic | meaningful | high | critical  (default: meaningful)
      hours       = lookback window (default: 24)
      limit       = 1..200 (default: 30)
      region      = domestic | international  (default: domestic)
    """
    limit = _query_int("limit", 30, min_v=1, max_v=200)
    hours = _query_int("hours", 24, min_v=1, max_v=720)
    category = (request.args.get("category") or "").strip() or None
    ticker = (request.args.get("ticker") or "").upper().strip() or None
    min_quality = (request.args.get("min_quality") or "meaningful").lower()
    region = (request.args.get("region") or "domestic").lower()

    if region not in ("domestic", "international"):
        return _err(400, "invalid_region", "region must be 'domestic' or 'international'")

    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")

    # Reuse the unified feed reader.
    try:
        from api_v2 import _read_events, _annotate_tier_freshness, _attach_alpha, \
            _build_is_international  # type: ignore
        cats = {category} if category else None
        items = _read_events(db, hours, ticker, cats, limit * 3)
        _annotate_tier_freshness(items)
        _attach_alpha(db, items)

        # Quality + region filter — same logic as /api/feed, kept inline so
        # this endpoint owns its contract.
        is_intl = _build_is_international()
        tier_order = {"noise": 0, "generic": 1, "meaningful": 2, "high": 3, "critical": 4}
        min_idx = tier_order.get(min_quality, 2)

        try:
            from news_quality import classify_dict as _classify  # type: ignore
        except Exception:
            _classify = None

        kept = []
        for it in items:
            src = it.get("source") or it.get("feed")
            if region == "domestic" and is_intl(src):
                continue
            if region == "international" and not is_intl(src):
                continue
            if _classify:
                q = _classify({
                    "title": it.get("title"), "summary": it.get("summary"),
                    "source": src, "source_tier": it.get("source_tier"),
                })
                it["impact_tier"] = q.impact_tier
                it["quality_score"] = q.quality_score
                if q.kinds:
                    it["kinds"] = q.kinds
                if q.market_implication:
                    it["market_implication"] = q.market_implication
                if tier_order.get(q.impact_tier, 0) < min_idx:
                    continue
            kept.append(it)
        items = kept[:limit]
        return _ok(items, region=region, min_quality=min_quality)
    except Exception as e:
        logger.exception(f"v1_news failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Events — structured market events table
# ---------------------------------------------------------------------------

@bp.route("/api/v1/events", methods=["GET"])
@ttl_cache(seconds=30)
def v1_events():
    """Structured market events.

    Query params:
      type   = earnings | merger | policy | order_win | dividend | supply | insider | ipo | buyback | split | bonus | rights | corporate (groups all of above)
      limit  = 1..200 (default: 50)
    """
    limit = _query_int("limit", 50, min_v=1, max_v=200)
    event_type = (request.args.get("type") or "").strip() or None

    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")

    try:
        if event_type == "corporate":
            cur = db.conn.cursor()
            cur.execute(
                """SELECT id, event_id, title, summary, event_type, sentiment, magnitude,
                          impact_score, companies, source, link, published_at, created_at
                   FROM events
                   WHERE event_type IN ('buyback','dividend','split','bonus','rights','merger')
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        elif event_type:
            cur = db.conn.cursor()
            cur.execute(
                """SELECT id, event_id, title, summary, event_type, sentiment, magnitude,
                          impact_score, companies, source, link, published_at, created_at
                   FROM events
                   WHERE event_type = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (event_type, limit))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            rows = db.get_recent_events(limit)
        return _ok(rows, type=event_type)
    except Exception as e:
        logger.exception(f"v1_events failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Earnings calendar — forward-looking only
# ---------------------------------------------------------------------------

@bp.route("/api/v1/earnings", methods=["GET"])
def v1_earnings():
    """Upcoming earnings dates within the configured window.

    Query params:
      days_ahead = 7..120 (default: 45)
      limit      = 5..60  (default: 20)

    Past dates are filtered out by the underlying endpoint.
    """
    # Direct passthrough — the existing /api/earnings already filters and
    # caches. We just unwrap its envelope into our v1 shape.
    days_ahead = _query_int("days_ahead", 45, min_v=7, max_v=120)
    limit = _query_int("limit", 20, min_v=5, max_v=60)
    try:
        from flask import current_app
        with current_app.test_request_context(
            f"/api/earnings?days_ahead={days_ahead}&limit={limit}"
        ):
            from api import get_earnings_calendar  # type: ignore
            resp = get_earnings_calendar()
            # resp is a flask Response from jsonify; extract data
            import json as _json
            body = _json.loads(resp.get_data(as_text=True))
            return _ok(body.get("data") or [], window_days=days_ahead)
    except Exception as e:
        logger.exception(f"v1_earnings failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# IPOs
# ---------------------------------------------------------------------------

@bp.route("/api/v1/ipos", methods=["GET"])
def v1_ipos():
    """Upcoming and recent IPOs.

    Query params:
      limit = 1..50 (default: 12)
    """
    limit = _query_int("limit", 12, min_v=1, max_v=50)
    try:
        from flask import current_app
        with current_app.test_request_context(f"/api/ipos?limit={limit}"):
            from api import get_ipos  # type: ignore
            resp = get_ipos()
            import json as _json
            body = _json.loads(resp.get_data(as_text=True))
            return _ok(body.get("data") or [])
    except Exception as e:
        logger.exception(f"v1_ipos failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Commodities — price + equity impact (causal map)
# ---------------------------------------------------------------------------

@bp.route("/api/v1/commodities", methods=["GET"])
def v1_commodities():
    """Commodity prices with India equity-impact narrative.

    Each row carries `equity_impact` (beneficiary / affected sectors) via the
    causal_map module — e.g. crude up → hurts Aviation / helps Upstream Oil.

    Query params:
      category = Bullion | Energy | Base Metals | Agri | FX  (optional)
      hours    = event lookback for sentiment (default: 168 = 7d)
    """
    category = (request.args.get("category") or "").strip() or None
    hours = _query_int("hours", 168, min_v=24, max_v=720)
    try:
        from flask import current_app
        q = f"?hours={hours}" + (f"&category={category}" if category else "")
        with current_app.test_request_context("/api/commodities/all" + q):
            from api_v2 import commodities_all  # type: ignore
            resp = commodities_all()
            import json as _json
            body = _json.loads(resp.get_data(as_text=True))
            return _ok(body.get("data") or [], lookback_hours=hours)
    except Exception as e:
        logger.exception(f"v1_commodities failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Sectors — bullish/bearish breakdown of active signals
# ---------------------------------------------------------------------------

@bp.route("/api/v1/sectors", methods=["GET"])
def v1_sectors():
    """Sector sentiment breakdown across active signals."""
    try:
        from flask import current_app
        with current_app.test_request_context("/api/sectors"):
            from api import get_sectors  # type: ignore
            resp = get_sectors()
            import json as _json
            body = _json.loads(resp.get_data(as_text=True))
            return _ok(body.get("data") or [])
    except Exception as e:
        logger.exception(f"v1_sectors failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Stock-level endpoints
# ---------------------------------------------------------------------------

def _ticker_or_400(ticker: str):
    """Normalize ticker; return (ticker, None) or (None, error_response)."""
    tk = (ticker or "").strip().upper()
    if not tk:
        return None, _err(400, "invalid_ticker", "ticker must be a non-empty string")
    if len(tk) > 20 or not all(c.isalnum() or c in "-&" for c in tk):
        return None, _err(400, "invalid_ticker", f"ticker {tk!r} contains invalid characters")
    return tk, None


@bp.route("/api/v1/stocks/<ticker>", methods=["GET"])
def v1_stock_snapshot(ticker: str):
    """Combined stock snapshot: profile + technicals + fundamentals score.

    Returns a flat object with the most-used fields. For deeper data hit
    the dedicated sub-endpoints (/profile, /financials, /technicals,
    /fundamentals).
    """
    tk, err = _ticker_or_400(ticker)
    if err:
        return err
    try:
        from flask import current_app
        import json as _json

        def _call(path):
            with current_app.test_request_context(path):
                endpoint, view_args = current_app.url_map.bind("localhost").match(path)
                rv = current_app.view_functions[endpoint](**view_args)
                if isinstance(rv, tuple):
                    rv = rv[0]
                return _json.loads(rv.get_data(as_text=True))

        prof = (_call(f"/api/stock/{tk}/profile") or {}).get("data") or {}
        tech = (_call(f"/api/stock/{tk}/technicals") or {}).get("data") or {}
        fund = (_call(f"/api/fundamentals/score/{tk}") or {}).get("data") or {}

        snap = {
            "ticker": tk,
            "company": prof.get("company"),
            "sector": prof.get("sector"),
            "industry": prof.get("industry"),
            "market_cap": prof.get("market_cap"),
            "price": tech.get("last_price"),
            "pct_change_1d": tech.get("pct_change_1d"),
            "rsi_14": tech.get("rsi_14"),
            "ma_50": tech.get("ma_50"),
            "ma_200": tech.get("ma_200"),
            "trend": tech.get("trend"),
            "week_52_high": tech.get("week_52_high"),
            "week_52_low": tech.get("week_52_low"),
            "fundamentals_score": fund.get("score"),
            "fundamentals_tier": fund.get("tier"),
            "fundamentals_label": fund.get("label"),
        }
        return _ok(snap)
    except Exception as e:
        logger.exception(f"v1_stock_snapshot {tk} failed: {e}")
        return _err(500, "internal_error", str(e))


def _passthrough(internal_path: str):
    """Helper: call an internal /api/* endpoint and return its data field.
    Passes URL params (e.g. <ticker>) to the dispatched view function via
    Werkzeug's matched view_args dict — otherwise calling `view_functions
    [endpoint]()` with no kwargs raises TypeError on parameterized routes.
    """
    from flask import current_app
    import json as _json
    with current_app.test_request_context(internal_path):
        endpoint, view_args = current_app.url_map.bind("localhost").match(internal_path)
        rv = current_app.view_functions[endpoint](**view_args)
        if isinstance(rv, tuple):
            rv = rv[0]
        return _json.loads(rv.get_data(as_text=True))


# Screener.in fallback — used when yfinance returns empty for Indian tickers.
# Cached for 24h per-ticker in the scraper module. Failure → returns None
# and the caller falls through to the original response.
def _screener_fallback(ticker: str):
    try:
        from screener_scraper import fetch as _screener_fetch
        snap = _screener_fetch(ticker)
        return snap
    except Exception as e:
        logger.debug(f"screener fallback failed for {ticker}: {e}")
        return None


def _payload_has_real_data(body: Dict[str, Any]) -> bool:
    """Heuristic: did the upstream actually return useful data?"""
    if not isinstance(body, dict):
        return False
    data = body.get("data") if "data" in body else body
    if not data or (isinstance(data, dict) and not any(v for v in data.values())):
        return False
    return True


@bp.route("/api/v1/stocks/<ticker>/profile", methods=["GET"])
def v1_stock_profile(ticker: str):
    """Company profile: name, sector, industry, summary, market cap, officers.
    Falls back to Screener.in scrape if yfinance/internal sources are thin."""
    tk, err = _ticker_or_400(ticker)
    if err:
        return err
    body: Dict[str, Any] = {}
    try:
        body = _passthrough(f"/api/stock/{tk}/profile") or {}
    except Exception:
        body = {}

    primary = body.get("data") or {}
    if _payload_has_real_data({"data": primary}):
        return _ok(primary)

    # Screener fallback
    snap = _screener_fallback(tk)
    if snap:
        c = snap.company or {}
        ratios = snap.ratios or {}
        fallback = {
            "ticker": tk,
            "name": c.get("name"),
            "sector": c.get("sector"),
            "industry": c.get("industry"),
            "nse_code": c.get("nse_code"),
            "bse_code": c.get("bse_code"),
            "market_cap_cr": ratios.get("market_cap"),
            "current_price": ratios.get("current_price"),
            "pe": ratios.get("stock_p/e") or ratios.get("pe"),
            "book_value": ratios.get("book_value"),
            "dividend_yield_pct": ratios.get("dividend_yield"),
            "roe_pct": ratios.get("roe"),
            "roce_pct": ratios.get("roce"),
            "face_value": ratios.get("face_value"),
            "pros": snap.pros,
            "cons": snap.cons,
        }
        return _ok(fallback, source="screener.in")
    return _ok(primary)


@bp.route("/api/v1/stocks/<ticker>/financials", methods=["GET"])
def v1_stock_financials(ticker: str):
    """Income statement, balance sheet, cash flow — annual + quarterly.
    Values returned in ₹ crore. Falls back to Screener.in scrape when
    yfinance returns empty (most Indian tickers)."""
    tk, err = _ticker_or_400(ticker)
    if err:
        return err
    body: Dict[str, Any] = {}
    try:
        body = _passthrough(f"/api/stock/{tk}/financials") or {}
    except Exception:
        body = {}

    primary = body.get("data") or {}
    if _payload_has_real_data({"data": primary}):
        return _ok(primary)

    # Screener fallback — fundamental snapshot in ₹ crore
    snap = _screener_fallback(tk)
    if snap:
        def _series(table, label_contains):
            from screener_scraper import get_row
            row = get_row(table, label_contains)
            if not row:
                return None
            return row.get("values") or []
        fallback = {
            "quarterly": {
                "periods":     snap.quarterly_periods,
                "sales":       _series(snap.quarterly_results, "Sales") or _series(snap.quarterly_results, "Revenue"),
                "expenses":    _series(snap.quarterly_results, "Expenses"),
                "operating_profit": _series(snap.quarterly_results, "Operating Profit"),
                "opm_pct":     _series(snap.quarterly_results, "OPM"),
                "other_income":_series(snap.quarterly_results, "Other Income"),
                "interest":    _series(snap.quarterly_results, "Interest"),
                "depreciation":_series(snap.quarterly_results, "Depreciation"),
                "profit_before_tax": _series(snap.quarterly_results, "Profit before tax"),
                "tax_pct":     _series(snap.quarterly_results, "Tax %"),
                "net_profit":  _series(snap.quarterly_results, "Net Profit") or _series(snap.quarterly_results, "Profit"),
                "eps":         _series(snap.quarterly_results, "EPS"),
            },
            "profit_and_loss": {
                "periods":     snap.pl_periods,
                "sales":       _series(snap.profit_and_loss, "Sales") or _series(snap.profit_and_loss, "Revenue"),
                "expenses":    _series(snap.profit_and_loss, "Expenses"),
                "opm_pct":     _series(snap.profit_and_loss, "OPM"),
                "net_profit":  _series(snap.profit_and_loss, "Net Profit") or _series(snap.profit_and_loss, "Profit"),
                "eps":         _series(snap.profit_and_loss, "EPS"),
            },
            "balance_sheet": {
                "periods":     snap.bs_periods,
                "equity_capital": _series(snap.balance_sheet, "Equity Capital"),
                "reserves":    _series(snap.balance_sheet, "Reserves"),
                "borrowings":  _series(snap.balance_sheet, "Borrowings"),
                "fixed_assets":_series(snap.balance_sheet, "Fixed Assets"),
                "total_assets":_series(snap.balance_sheet, "Total Assets"),
            },
            "source": "screener.in",
            "source_url": snap.source_url,
        }
        return _ok(fallback, source="screener.in")
    return _ok(primary)


@bp.route("/api/v1/stocks/<ticker>/screener", methods=["GET"])
@ttl_cache(seconds=3600)
def v1_stock_screener(ticker: str):
    """Direct Screener.in snapshot for a ticker — quarterly P&L, annual P&L,
    balance sheet, top ratios, pros/cons. 24h cache in scraper, 1h API cache.
    Useful when callers explicitly want the Screener perspective."""
    tk, err = _ticker_or_400(ticker)
    if err:
        return err
    snap = _screener_fallback(tk)
    if not snap:
        return _err(404, "screener_unavailable",
                    f"Screener.in returned no data for {tk}")
    return _ok(snap.to_dict(), source="screener.in")


@bp.route("/api/v1/stocks/<ticker>/technicals", methods=["GET"])
def v1_stock_technicals(ticker: str):
    """RSI(14), MA50, MA200, trend label, 52w range."""
    tk, err = _ticker_or_400(ticker)
    if err:
        return err
    try:
        body = _passthrough(f"/api/stock/{tk}/technicals")
        return _ok(body.get("data") or {})
    except Exception as e:
        return _err(500, "internal_error", str(e))


@bp.route("/api/v1/stocks/<ticker>/fundamentals", methods=["GET"])
def v1_stock_fundamentals(ticker: str):
    """F-score (0-100) with tier, label, positives, red flags, component
    breakdown across valuation / profitability / growth / leverage /
    cashflow / promoter pledge / institutional holding."""
    tk, err = _ticker_or_400(ticker)
    if err:
        return err
    try:
        body = _passthrough(f"/api/fundamentals/score/{tk}")
        return _ok(body.get("data") or {})
    except Exception as e:
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Filings archive — searchable BSE/NSE filings
# ---------------------------------------------------------------------------

@bp.route("/api/v1/filings", methods=["GET"])
@ttl_cache(seconds=60)
def v1_filings():
    """BSE/NSE filings archive — searchable, filterable.

    Query params:
      ticker  = filter by NSE symbol (case-insensitive)
      type    = filing_type (quarterly_result | order_intimation | agm | other)
      q       = full-text search across title + raw_text
      since   = ISO date YYYY-MM-DD (filed_at >=)
      until   = ISO date YYYY-MM-DD (filed_at <=)
      limit   = 1..200 (default: 50)
      offset  = pagination offset (default: 0)

    Returns metadata + pdf_url. Use /api/v1/filings/{id} for raw_text.
    """
    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")

    ticker = (request.args.get("ticker") or "").upper().strip() or None
    ftype = (request.args.get("type") or "").strip() or None
    q = (request.args.get("q") or "").strip() or None
    since = (request.args.get("since") or "").strip() or None
    until = (request.args.get("until") or "").strip() or None
    limit = _query_int("limit", 50, min_v=1, max_v=200)
    offset = _query_int("offset", 0, min_v=0, max_v=10000)

    try:
        cur = db.conn.cursor()
        where = ["1=1"]
        params: List[Any] = []
        if ticker:
            where.append("UPPER(ticker) = ?"); params.append(ticker)
        if ftype:
            where.append("filing_type = ?"); params.append(ftype)
        if q:
            where.append("(title LIKE ? OR raw_text LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if since:
            where.append("filed_at >= ?"); params.append(since)
        if until:
            where.append("filed_at <= ?"); params.append(until + " 23:59:59")

        sql = f"""SELECT id, ticker, filing_type, title, pdf_url, filed_at, source
                  FROM filings
                  WHERE {' AND '.join(where)}
                  ORDER BY filed_at DESC NULLS LAST, created_at DESC
                  LIMIT ? OFFSET ?"""
        # SQLite doesn't support NULLS LAST consistently
        if not getattr(db, "is_postgres", False):
            sql = sql.replace(" NULLS LAST", "")
        params.extend([limit, offset])
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return _ok(rows, ticker=ticker, type=ftype, q=q, limit=limit, offset=offset)
    except Exception as e:
        logger.exception(f"v1_filings failed: {e}")
        return _err(500, "internal_error", str(e))


@bp.route("/api/v1/filings/<int:filing_id>", methods=["GET"])
def v1_filing_detail(filing_id: int):
    """Single filing with parsed numbers + raw text excerpt."""
    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT id, ticker, filing_type, title, pdf_url, filed_at,
                      source, raw_text, created_at
               FROM filings WHERE id = ?""", (filing_id,))
        row = cur.fetchone()
        if not row:
            return _err(404, "not_found", f"Filing {filing_id} does not exist")
        cols = [d[0] for d in cur.description]
        rec = dict(zip(cols, row))
        # Truncate raw_text to 8 KB so the response stays reasonable
        if rec.get("raw_text"):
            rt = rec["raw_text"]
            rec["raw_text_excerpt"] = rt[:8000]
            rec["raw_text_length"] = len(rt)
            del rec["raw_text"]
        # Pull extracted numbers from filing_numbers if present
        try:
            cur.execute(
                "SELECT metric, value, unit, period FROM filing_numbers WHERE filing_id = ? LIMIT 50",
                (filing_id,))
            ncols = [d[0] for d in cur.description]
            rec["numbers"] = [dict(zip(ncols, r)) for r in cur.fetchall()]
        except Exception:
            rec["numbers"] = []
        return _ok(rec)
    except Exception as e:
        logger.exception(f"v1_filing_detail {filing_id} failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Earnings reaction archive — historical T+1/T+3/T+5 price moves
# ---------------------------------------------------------------------------

@bp.route("/api/v1/earnings/reactions/<ticker>", methods=["GET"])
@ttl_cache(seconds=21600)  # 6h — historical, rarely changes
def v1_earnings_reactions(ticker: str):
    """Historical earnings-reaction archive for a ticker.

    Reads from the `earnings_reactions` table first (populated by the
    backfill worker). Falls back to live yfinance computation only when
    the ticker isn't in the table yet — and writes the result back so the
    next call is fast.

    Query params:
      lookback_quarters = 4..20 (default: 12) — how many prior results to include
    """
    tk, err = _ticker_or_400(ticker)
    if err:
        return err
    lookback = _query_int("lookback_quarters", 12, min_v=4, max_v=20)

    # --- Fast path: read from earnings_reactions table -----------------
    db = _get_db() if _get_db else None
    if db:
        try:
            cur = db.conn.cursor()
            cur.execute(
                """SELECT earnings_date, base_close, ret_1d_pct, ret_3d_pct,
                          ret_5d_pct, eps_estimate, eps_reported, surprise_pct
                   FROM earnings_reactions
                   WHERE UPPER(ticker) = ?
                   ORDER BY earnings_date DESC LIMIT ?""",
                (tk, lookback))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            if rows:
                # Normalize date column to YYYY-MM-DD string for consistency
                # with the live-compute path (which also returns ISO dates).
                for r in rows:
                    if r.get("earnings_date") and not isinstance(r["earnings_date"], str):
                        r["earnings_date"] = str(r["earnings_date"])[:10]
                return _ok(rows, ticker=tk,
                           lookback_quarters=lookback, source="archive")
        except Exception as e:
            logger.debug(f"earnings_reactions table read failed (will fall back to live): {e}")

    # --- Slow path: live yfinance compute (and trigger background save)
    try:
        import yfinance as yf
        import pandas as pd  # type: ignore
        t = yf.Ticker(f"{tk}.NS")
        # Fetch earnings history — yfinance exposes via .earnings_dates
        try:
            ed = t.earnings_dates
        except Exception:
            ed = None
        if ed is None or (hasattr(ed, "empty") and ed.empty):
            return _ok([], note="No earnings history available from data provider")

        # Strip timezone everywhere up front — yfinance returns Asia/Kolkata
        # tz on earnings_dates.index but plain naive on history.index, which
        # makes any direct comparison raise. We normalize to naive UTC-ish.
        if ed.index.tz is not None:
            ed = ed.copy()
            ed.index = ed.index.tz_localize(None)

        now = pd.Timestamp.utcnow().tz_localize(None) if pd.Timestamp.utcnow().tz is not None else pd.Timestamp.now()
        past = ed[ed.index <= now].head(lookback)
        if past.empty:
            return _ok([])

        # One yfinance history call for the price series, then slice
        earliest = past.index.min() - pd.Timedelta(days=10)
        hist = t.history(start=earliest, period=None, interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return _ok([])

        out = []
        closes = hist["Close"].dropna()
        # Ensure price series is also tz-naive
        if closes.index.tz is not None:
            closes.index = closes.index.tz_localize(None)

        for dt, row in past.iterrows():
            # Find the closing price ON or just before the earnings date
            anchor_date = pd.Timestamp(dt)
            if anchor_date.tz is not None:
                anchor_date = anchor_date.tz_localize(None)
            cutoff = closes[closes.index <= anchor_date]
            if cutoff.empty:
                continue
            base_price = float(cutoff.iloc[-1])
            base_date = cutoff.index[-1]
            # T+1, T+3, T+5 trading days after base_date
            future = closes[closes.index > base_date]
            def _ret(n):
                if len(future) < n:
                    return None
                return round((float(future.iloc[n - 1]) - base_price) / base_price * 100, 2)
            est = row.get("EPS Estimate") if hasattr(row, "get") else None
            rep = row.get("Reported EPS") if hasattr(row, "get") else None
            try:
                surprise_pct = None
                if est is not None and rep is not None and est != 0:
                    surprise_pct = round((float(rep) - float(est)) / abs(float(est)) * 100, 2)
            except Exception:
                surprise_pct = None
            out.append({
                "earnings_date": str(anchor_date.date() if hasattr(anchor_date, "date") else anchor_date)[:10],
                "base_close": round(base_price, 2),
                "ret_1d_pct": _ret(1),
                "ret_3d_pct": _ret(3),
                "ret_5d_pct": _ret(5),
                "eps_estimate": float(est) if est is not None and not pd.isna(est) else None,
                "eps_reported": float(rep) if rep is not None and not pd.isna(rep) else None,
                "surprise_pct": surprise_pct,
            })
        # Newest first
        out.sort(key=lambda r: r["earnings_date"], reverse=True)
        # Best-effort: write what we computed back to the table so the next
        # call to this endpoint is a single indexed SELECT. The backfill
        # worker uses the same upsert path, so this is idempotent.
        if db and out:
            try:
                from earnings_reactions_backfill import _upsert as _er_upsert
                rows_with_tk = [dict(r, ticker=tk) for r in out]
                _er_upsert(db, rows_with_tk)
            except Exception as _save_e:
                logger.debug(f"earnings_reactions write-back skipped: {_save_e}")
        return _ok(out, ticker=tk, lookback_quarters=lookback, source="live")
    except Exception as e:
        logger.exception(f"v1_earnings_reactions {tk} failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Cross-event linkage — related events by ticker + time window
# ---------------------------------------------------------------------------

@bp.route("/api/v1/events/<event_id>/related", methods=["GET"])
@ttl_cache(seconds=120)
def v1_event_related(event_id: str):
    """Events that occurred near this one (same ticker, ± window_days).

    Rule-based linker: companies in the source event + window around its
    published_at → returns siblings with relation tag ('same_ticker',
    'precedes', 'follows').

    Query params:
      window_days = 1..60 (default: 14)
      limit       = 1..50 (default: 15)
    """
    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")
    window = _query_int("window_days", 14, min_v=1, max_v=60)
    limit = _query_int("limit", 15, min_v=1, max_v=50)
    try:
        cur = db.conn.cursor()
        # Pull the anchor event
        cur.execute(
            """SELECT event_id, title, event_type, companies, published_at, created_at
               FROM events WHERE event_id = ?""", (event_id,))
        row = cur.fetchone()
        if not row:
            return _err(404, "not_found", f"Event {event_id} not found")
        cols = [d[0] for d in cur.description]
        anchor = dict(zip(cols, row))
        # Companies field may be CSV or JSON
        cos_raw = anchor.get("companies") or ""
        cos: List[str] = []
        if isinstance(cos_raw, str):
            cos_raw = cos_raw.strip()
            if cos_raw.startswith("["):
                try:
                    import json as _json
                    cos = [c for c in _json.loads(cos_raw) if c]
                except Exception:
                    pass
            if not cos:
                cos = [c.strip() for c in cos_raw.split(",") if c.strip()]
        elif isinstance(cos_raw, list):
            cos = [c for c in cos_raw if c]
        if not cos:
            return _ok([], note="Anchor event has no companies; nothing to link")

        anchor_ts = anchor.get("published_at") or anchor.get("created_at")
        # Find related events on the same companies within +/- window_days
        like_clauses = " OR ".join(["companies LIKE ?"] * len(cos))
        like_params = [f"%{c}%" for c in cos]
        sql = f"""SELECT event_id, title, event_type, sentiment, magnitude,
                         impact_score, companies, published_at, created_at, source
                  FROM events
                  WHERE event_id != ?
                    AND ({like_clauses})
                    AND COALESCE(published_at, created_at) IS NOT NULL"""
        params = [event_id] + like_params
        cur.execute(sql + " ORDER BY COALESCE(published_at, created_at) DESC LIMIT 500", params)
        rcols = [d[0] for d in cur.description]
        candidates = [dict(zip(rcols, r)) for r in cur.fetchall()]

        # Filter by time window in Python (avoids cross-DB date arithmetic)
        from datetime import datetime as _dt, timedelta as _td
        def _parse(ts):
            if not ts: return None
            try:
                # Try a few common shapes
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                    try: return _dt.strptime(str(ts)[:19], fmt)
                    except ValueError: continue
                return _dt.strptime(str(ts)[:10], "%Y-%m-%d")
            except Exception:
                return None
        anchor_dt = _parse(anchor_ts)
        related = []
        for c in candidates:
            cdt = _parse(c.get("published_at") or c.get("created_at"))
            if not cdt or not anchor_dt:
                continue
            delta = cdt - anchor_dt
            days = delta.days
            if abs(days) > window:
                continue
            relation = "precedes" if days < 0 else ("follows" if days > 0 else "concurrent")
            c["lag_days"] = days
            c["relation"] = relation
            related.append(c)
            if len(related) >= limit:
                break
        return _ok(related, anchor_event_id=event_id, window_days=window)
    except Exception as e:
        logger.exception(f"v1_event_related {event_id} failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Smart-money tracker — longitudinal view over promoter_events + sebi
# ---------------------------------------------------------------------------

@bp.route("/api/v1/smart-money/<ticker>", methods=["GET"])
@ttl_cache(seconds=300)
def v1_smart_money(ticker: str):
    """Longitudinal smart-money view for a ticker.

    Combines `promoter_events` (SAST/PIT) + `sebi_disclosures` (insider) into
    a single chronological feed with aggregate stats.

    Query params:
      months = 1..120 (default: 24) — lookback window
      limit  = 1..200 (default: 50)
    """
    tk, err = _ticker_or_400(ticker)
    if err:
        return err
    months = _query_int("months", 24, min_v=1, max_v=120)
    limit = _query_int("limit", 50, min_v=1, max_v=200)
    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")
    try:
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.utcnow() - _td(days=30 * months)).date().isoformat()
        cur = db.conn.cursor()
        # promoter_events
        prom = []
        try:
            cur.execute(
                """SELECT event_date, event_type, person_name, quantity,
                          pct_before, pct_after, reason, source, link
                   FROM promoter_events
                   WHERE UPPER(ticker) = ? AND event_date >= ?
                   ORDER BY event_date DESC LIMIT ?""",
                (tk, cutoff, limit * 2))
            cols = [d[0] for d in cur.description]
            prom = [dict(zip(cols, r), table="promoter_events") for r in cur.fetchall()]
        except Exception as e:
            logger.debug(f"promoter_events query failed: {e}")
        # sebi_disclosures
        sebi = []
        try:
            cur.execute(
                """SELECT transaction_date AS event_date, disclosure_type AS event_type,
                          person_name, designation, transaction_type, quantity,
                          pct_before, pct_after, source, link
                   FROM sebi_disclosures
                   WHERE UPPER(ticker) = ? AND transaction_date >= ?
                   ORDER BY transaction_date DESC LIMIT ?""",
                (tk, cutoff, limit * 2))
            cols = [d[0] for d in cur.description]
            sebi = [dict(zip(cols, r), table="sebi_disclosures") for r in cur.fetchall()]
        except Exception as e:
            logger.debug(f"sebi_disclosures query failed: {e}")
        # Merge + sort
        events = prom + sebi
        events.sort(key=lambda x: str(x.get("event_date") or ""), reverse=True)
        events = events[:limit]
        # Aggregate stats: buys vs sells, pledges, persons
        def _is_buy(ev):
            et = (ev.get("event_type") or "").lower()
            tt = (ev.get("transaction_type") or "").lower()
            return "buy" in et or "acquire" in et or "buy" == tt
        def _is_sell(ev):
            et = (ev.get("event_type") or "").lower()
            tt = (ev.get("transaction_type") or "").lower()
            return "sell" in et or "dispose" in et or "sell" == tt
        def _is_pledge(ev):
            et = (ev.get("event_type") or "").lower()
            tt = (ev.get("transaction_type") or "").lower()
            return "pledge" in et or "pledge" in tt
        buys = sum(1 for e in events if _is_buy(e))
        sells = sum(1 for e in events if _is_sell(e))
        pledges = sum(1 for e in events if _is_pledge(e))
        people = sorted({(e.get("person_name") or "").strip()
                          for e in events if e.get("person_name")})
        net_qty_buy = sum(float(e.get("quantity") or 0)
                           for e in events if _is_buy(e) and e.get("quantity"))
        net_qty_sell = sum(float(e.get("quantity") or 0)
                            for e in events if _is_sell(e) and e.get("quantity"))
        return _ok(events,
                   ticker=tk,
                   months=months,
                   stats={
                       "total_events": len(events),
                       "buys": buys,
                       "sells": sells,
                       "pledges": pledges,
                       "unique_persons": len(people),
                       "buy_quantity": net_qty_buy,
                       "sell_quantity": net_qty_sell,
                       "net_buy_quantity": net_qty_buy - net_qty_sell,
                   },
                   people=people[:30])
    except Exception as e:
        logger.exception(f"v1_smart_money {tk} failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# SLA — alert delivery latency dashboard
# ---------------------------------------------------------------------------
# Minimal in-memory ring buffer for now. Captures every BSE filing → alert
# delivery measurement so the dashboard can show median + p99 + last-24h.
# Persists across requests but not across restart — that's fine for a
# transparency surface; long-term we'd flush to a `alert_latency_log` table.

_SLA_BUFFER: List[Dict[str, Any]] = []
_SLA_MAX = 5000


def _sla_record(stage: str, ticker: Optional[str], latency_ms: float):
    """Public function the scraper/notification pipeline can call. Records
    a single (stage, ticker, latency_ms) sample. Caller is responsible for
    measuring; we just store.
    """
    try:
        _SLA_BUFFER.append({
            "stage": stage,
            "ticker": ticker,
            "latency_ms": float(latency_ms),
            "at": datetime.utcnow().isoformat() + "Z",
        })
        if len(_SLA_BUFFER) > _SLA_MAX:
            del _SLA_BUFFER[:len(_SLA_BUFFER) - _SLA_MAX]
    except Exception:
        pass


@bp.route("/api/v1/sla", methods=["GET"])
def v1_sla():
    """Alert delivery latency stats.

    Returns median + p50 + p95 + p99 latency in ms across recent samples,
    bucketed by `stage` (e.g. 'bse_filing_to_classify', 'classify_to_alert',
    'alert_to_telegram').

    Query params:
      hours = 1..168 (default: 24) — lookback window
    """
    hours = _query_int("hours", 24, min_v=1, max_v=168)
    from datetime import datetime as _dt, timedelta as _td
    cutoff = (_dt.utcnow() - _td(hours=hours)).isoformat() + "Z"

    samples = [s for s in _SLA_BUFFER if s.get("at", "") >= cutoff]
    if not samples:
        # Diagnostic empty state — tell the consumer exactly what's missing
        # so they can tell "pipeline broken" apart from "no traffic yet".
        db = _get_db() if _get_db else None
        diag = {"notification_log_rows": None, "events_24h": None, "alerts_configured": None}
        try:
            if db:
                cur = db.conn.cursor()
                cur.execute("SELECT COUNT(*) FROM notification_log")
                diag["notification_log_rows"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM events WHERE created_at >= datetime('now', '-24 hours')")
                diag["events_24h"] = cur.fetchone()[0]
                try:
                    cfg = db.get_notification_config()
                    diag["alerts_configured"] = bool(
                        cfg and (cfg.get("telegram_bot_token") or cfg.get("discord_webhook_url"))
                    )
                except Exception:
                    pass
        except Exception:
            pass
        note = (
            "No latency samples in window yet. "
            f"Pipeline state: events_24h={diag['events_24h']}, "
            f"notification_log_rows={diag['notification_log_rows']}, "
            f"alerts_configured={diag['alerts_configured']}. "
            "Latency will populate after the first successful Telegram/Discord delivery."
        )
        return _ok({
            "samples": 0,
            "hours": hours,
            "stages": {},
            "overall": None,
            "diagnostic": diag,
            "note": note,
        })

    def _stats(arr):
        if not arr:
            return None
        s = sorted(arr)
        n = len(s)
        def _pct(p):
            idx = min(n - 1, max(0, int(p * (n - 1) / 100)))
            return round(s[idx], 1)
        return {
            "count": n,
            "median_ms": _pct(50),
            "p95_ms": _pct(95),
            "p99_ms": _pct(99),
            "min_ms": round(s[0], 1),
            "max_ms": round(s[-1], 1),
        }

    by_stage: Dict[str, List[float]] = {}
    for s in samples:
        by_stage.setdefault(s["stage"], []).append(s["latency_ms"])
    stages = {k: _stats(v) for k, v in by_stage.items()}
    overall = _stats([s["latency_ms"] for s in samples])

    return _ok({
        "samples": len(samples),
        "hours": hours,
        "stages": stages,
        "overall": overall,
    })


# ---------------------------------------------------------------------------
# Earnings forecast — Track A v0 (2026-05-19).
# Predicts next-quarter EPS + hit/miss probability + entry window. Reads
# from earnings_forecasts table (populated by daily forecast_job.py); on
# cache miss computes fresh via forecast_orchestrator.
# ---------------------------------------------------------------------------

@bp.route("/api/v1/earnings/forecast/<ticker>", methods=["GET"])
@ttl_cache(seconds=60)
def v1_earnings_forecast(ticker: str):
    """Latest forecast for the next upcoming earnings for `ticker`.

    Returns: revenue/EPS estimate with 80% CI, beat/meet/miss probabilities,
    expected day-1 reaction with CI, entry-window recommendation (dump risk
    + wait minutes + optimal entry window), top-3 driving signals.

    Query params:
      refresh = 1 to force recompute even if cached forecast exists
    """
    tk = (ticker or "").upper().strip().replace(".NS", "").replace(".BO", "")
    if not tk:
        return _err(400, "missing_ticker", "ticker required")
    force = request.args.get("refresh") in ("1", "true", "yes")

    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")

    try:
        from forecast_orchestrator import get_latest, compute_full_forecast, upsert
        existing = None if force else get_latest(db, tk)
        if existing:
            # Strip the bulky features blob for the v1 envelope
            existing.pop("features", None)
            return _ok(existing, source="cache")
        fc = compute_full_forecast(db, tk)
        try:
            upsert(db, fc)
        except Exception as ue:
            logger.debug(f"forecast upsert non-fatal: {ue}")
        out = {k: v for k, v in fc.items() if k != "features"}
        return _ok(out, source="fresh")
    except Exception as e:
        logger.exception(f"v1_earnings_forecast failed for {tk}: {e}")
        return _err(500, "internal_error", str(e))


@bp.route("/api/v1/earnings/forecast/<ticker>/history", methods=["GET"])
@ttl_cache(seconds=120)
def v1_earnings_forecast_history(ticker: str):
    """All past forecasts for `ticker` (for internal accuracy review)."""
    tk = (ticker or "").upper().strip().replace(".NS", "").replace(".BO", "")
    if not tk:
        return _err(400, "missing_ticker", "ticker required")
    limit = _query_int("limit", 20, min_v=1, max_v=100)
    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")
    try:
        from forecast_orchestrator import get_history
        rows = get_history(db, tk, limit=limit)
        return _ok(rows)
    except Exception as e:
        logger.exception(f"v1_earnings_forecast_history failed for {tk}: {e}")
        return _err(500, "internal_error", str(e))


@bp.route("/api/v1/earnings/forecast", methods=["GET"])
@ttl_cache(seconds=60)
def v1_earnings_forecast_list():
    """List all current forecasts for tickers with upcoming earnings.

    Query params:
      days_ahead = how far forward to look (default 30, max 90)
      sort       = 'confidence' (default) | 'date' | 'expected_return'
      hit        = filter by predicted_hit ('beat' | 'meet' | 'miss')
      limit      = 1..200 (default 50)
    """
    days_ahead = _query_int("days_ahead", 30, min_v=1, max_v=90)
    limit = _query_int("limit", 50, min_v=1, max_v=200)
    sort = (request.args.get("sort") or "confidence").lower()
    hit = (request.args.get("hit") or "").lower().strip() or None

    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")
    is_pg = getattr(db, "is_postgres", False)

    sort_col = {
        "confidence": "hit_confidence DESC",
        "date": "target_earnings_date ASC",
        "expected_return": "expected_ret_1d_pct DESC",
    }.get(sort, "hit_confidence DESC")

    try:
        cur = db.conn.cursor()
        where = ["model_version = ?"]
        params: List[Any] = ["v0"]
        if is_pg:
            where.append("target_earnings_date BETWEEN CURRENT_DATE AND (CURRENT_DATE + INTERVAL '%d days')" % days_ahead)
        else:
            where.append(f"target_earnings_date BETWEEN date('now') AND date('now', '+{days_ahead} days')")
        if hit:
            where.append("predicted_hit = ?")
            params.append(hit)
        sql = (f"SELECT ticker, forecast_date, target_earnings_date, predicted_hit, "
               f"hit_confidence, p_beat, p_meet, p_miss, eps_estimate, "
               f"expected_ret_1d_pct, dump_risk_score, recommended_action, "
               f"wait_minutes_estimate, fade_probability "
               f"FROM earnings_forecasts WHERE {' AND '.join(where)} "
               f"ORDER BY {sort_col} LIMIT ?")
        params.append(limit)
        if is_pg:
            sql = sql.replace("?", "%s")
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return _ok(rows, days_ahead=days_ahead, sort=sort, hit=hit)
    except Exception as e:
        logger.exception(f"v1_earnings_forecast_list failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Public alert audit log — the wedge moat for the intraday-trader persona.
#
# Paid Telegram channels (Stockwhip, Hilega Milega) physically cannot publish
# this view without exposing their hit-rate. We can — and it's the verifiable
# proof of the ₹499/mo pitch ("sub-60s alerts, here's the receipt").
# ---------------------------------------------------------------------------

@bp.route("/api/v1/alerts/log", methods=["GET"])
@ttl_cache(seconds=20)
def v1_alerts_log():
    """Public audit trail of every high-impact alert we dispatched.

    Each row shows the BSE/news publish time, when we classified it, when
    we sent the Telegram alert, and the two latencies. This is the wedge
    moat — verifiable, time-stamped, public.

    Query params:
      hours       = 1..168  (default 24)   — lookback window
      limit       = 1..500  (default 100)
      min_impact  = 0..100  (default 50)   — only show real alerts
      ticker      = optional, filter to one symbol
    """
    hours = _query_int("hours", 24, min_v=1, max_v=168)
    limit = _query_int("limit", 100, min_v=1, max_v=500)
    min_impact = _query_int("min_impact", 50, min_v=0, max_v=100)
    ticker = (request.args.get("ticker") or "").upper().strip() or None

    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")

    is_pg = getattr(db, "is_postgres", False)
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.utcnow() - _td(hours=hours)
    cutoff_str = cutoff.isoformat(sep=" ", timespec="seconds")

    try:
        cur = db.conn.cursor()
        where = ["e.impact_score >= ?", "COALESCE(e.published_at, e.created_at) >= ?"]
        params: List[Any] = [min_impact, cutoff_str]
        if ticker:
            where.append("UPPER(COALESCE(e.companies, '')) LIKE ?")
            params.append(f"%{ticker}%")

        # LEFT JOIN — alerts may not yet have a notification_log row if
        # they fall below the notification threshold or no Telegram is
        # configured. Show them anyway with NULL alerted_at.
        sql = f"""SELECT e.event_id, e.title, e.event_type, e.sentiment,
                         e.impact_score, e.companies, e.source, e.link,
                         e.published_at, e.created_at,
                         (SELECT MIN(sent_at)
                            FROM notification_log nl
                            WHERE nl.signal_event_id = e.event_id
                              AND nl.channel = 'telegram'
                              AND nl.status = 'success') AS telegram_sent_at,
                         (SELECT MIN(sent_at)
                            FROM notification_log nl
                            WHERE nl.signal_event_id = e.event_id
                              AND nl.status = 'success') AS first_sent_at
                  FROM events e
                  WHERE {' AND '.join(where)}
                  ORDER BY COALESCE(e.published_at, e.created_at) DESC
                  LIMIT ?"""
        params.append(limit)
        if is_pg:
            sql = sql.replace("?", "%s")
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        def _parse_ts(v):
            if not v:
                return None
            if isinstance(v, _dt):
                return v
            s = str(v).replace("Z", "").split(".")[0]
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    return _dt.strptime(s, fmt)
                except ValueError:
                    continue
            return None

        out = []
        classify_lats: List[float] = []
        deliver_lats: List[float] = []
        for r in rows:
            pub = _parse_ts(r.get("published_at"))
            cls = _parse_ts(r.get("created_at"))
            snt = _parse_ts(r.get("telegram_sent_at") or r.get("first_sent_at"))
            classify_s = (cls - pub).total_seconds() if pub and cls and cls >= pub else None
            deliver_s = (snt - pub).total_seconds() if pub and snt and snt >= pub else None
            if classify_s is not None and 0 <= classify_s <= 86400:
                classify_lats.append(classify_s)
            if deliver_s is not None and 0 <= deliver_s <= 86400:
                deliver_lats.append(deliver_s)
            out.append({
                "event_id": r.get("event_id"),
                "title": r.get("title"),
                "event_type": r.get("event_type"),
                "sentiment": r.get("sentiment"),
                "impact_score": round(float(r.get("impact_score") or 0), 1),
                "ticker": (r.get("companies") or "").split(",")[0].strip() or None,
                "source": r.get("source"),
                "link": r.get("link"),
                "published_at": r.get("published_at"),
                "classified_at": r.get("created_at"),
                "alerted_at": r.get("telegram_sent_at") or r.get("first_sent_at"),
                "classify_latency_s": round(classify_s, 1) if classify_s is not None else None,
                "delivery_latency_s": round(deliver_s, 1) if deliver_s is not None else None,
                "delivered": bool(r.get("telegram_sent_at") or r.get("first_sent_at")),
            })

        def _median(arr: List[float]) -> Optional[float]:
            if not arr:
                return None
            s = sorted(arr)
            n = len(s)
            return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0, 1)

        def _p95(arr: List[float]) -> Optional[float]:
            if not arr:
                return None
            s = sorted(arr)
            idx = min(len(s) - 1, max(0, int(0.95 * (len(s) - 1))))
            return round(s[idx], 1)

        summary = {
            "window_hours": hours,
            "alerts_in_window": len(out),
            "delivered_count": sum(1 for r in out if r["delivered"]),
            "classify_latency_median_s": _median(classify_lats),
            "classify_latency_p95_s": _p95(classify_lats),
            "delivery_latency_median_s": _median(deliver_lats),
            "delivery_latency_p95_s": _p95(deliver_lats),
        }

        return _ok(out, summary=summary, min_impact=min_impact)
    except Exception as e:
        logger.exception(f"v1_alerts_log failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Concall transcript search
# ---------------------------------------------------------------------------

@bp.route("/api/v1/concalls/search", methods=["GET"])
@ttl_cache(seconds=120)
def v1_concalls_search():
    """Search across indexed concall-transcript paragraphs.

    Query params:
      q       = required, free-text search (case-insensitive LIKE for now)
      ticker  = filter to a single ticker
      since   = YYYY-MM-DD — only paragraphs from filings filed after this
      limit   = 1..100 (default: 25)

    Each row: filing_id, ticker, filed_at, paragraph_idx, text, snippet.
    Use /api/v1/filings/{id} for the parent filing's metadata + pdf_url.
    """
    q = (request.args.get("q") or "").strip()
    if not q or len(q) < 3:
        return _err(400, "invalid_query",
                    "q must be at least 3 characters of free text")
    ticker = (request.args.get("ticker") or "").upper().strip() or None
    since = (request.args.get("since") or "").strip() or None
    limit = _query_int("limit", 25, min_v=1, max_v=100)

    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")
    try:
        cur = db.conn.cursor()
        where = ["text LIKE ?"]
        params: List[Any] = [f"%{q}%"]
        if ticker:
            where.append("UPPER(ticker) = ?"); params.append(ticker)
        if since:
            where.append("filed_at >= ?"); params.append(since)
        sql = f"""SELECT filing_id, ticker, filed_at, paragraph_idx, text
                  FROM concall_paragraphs
                  WHERE {' AND '.join(where)}
                  ORDER BY filed_at DESC NULLS LAST, filing_id DESC, paragraph_idx
                  LIMIT ?"""
        if not getattr(db, "is_postgres", False):
            sql = sql.replace(" NULLS LAST", "")
        params.append(limit)
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Build a small "snippet" (window around the first match) so clients
        # don't have to parse 700 chars to find the keyword.
        q_lower = q.lower()
        for r in rows:
            t = r.get("text") or ""
            idx = t.lower().find(q_lower)
            if idx >= 0:
                start = max(0, idx - 60)
                end = min(len(t), idx + len(q) + 80)
                snip = t[start:end]
                if start > 0: snip = "…" + snip
                if end < len(t): snip = snip + "…"
                r["snippet"] = snip
            else:
                r["snippet"] = t[:140] + ("…" if len(t) > 140 else "")

        return _ok(rows, q=q, ticker=ticker, since=since)
    except Exception as e:
        logger.exception(f"v1_concalls_search failed: {e}")
        return _err(500, "internal_error", str(e))


# ---------------------------------------------------------------------------
# Event-window backtest — public access to the same engine the live terminal
# uses internally. Lets API consumers ask "what historically happens N days
# after an `event_type` for `ticker`" without subscribing to the live stream.
# ---------------------------------------------------------------------------

@bp.route("/api/v1/backtest", methods=["GET"])
@ttl_cache(seconds=300)
def v1_backtest():
    """Event-window backtest for a (ticker, event_type) pair.

    Query params:
      ticker       = required. NSE/BSE symbol with or without .NS/.BO suffix.
      event_type   = required. e.g. "earnings", "order_win", "capacity_expansion".
      date         = optional. Anchor date for the primary event window.
                     Default: today (server clock, IST date approximated as UTC).
                     Format: YYYY-MM-DD.
      sentiment    = optional. Narrow the historical sample to events with
                     the same sentiment direction ("bullish" | "bearish" | "neutral").
                     Lets you ask "what happened when this earnings was a BEAT"
                     instead of "what happened on any earnings day".
      magnitude    = optional. Anchor magnitude (float). Combined with
                     `magnitude_band` (default ±0.4 = ±40%) to filter the
                     historical sample to comparably-sized events.
      magnitude_band = optional. Default 0.4. Range 0..1.
      lookback_days= optional. How far back to pull historically-similar events
                     for aggregation. Default 1825 (5 years), clamped 30..3650.
      drift_window = optional. Trading sessions after T to measure drift over.
                     Default 5, clamped 1..30.
      limit        = optional. Max historical events to include in the sample.
                     Default 20, clamped 1..100.

    Returns the same payload shape the live broadcast's `backtest_summary`
    field uses, plus the per-event breakdown so callers can chart distributions.

    Response (200):
      data: {
        ticker, sample_events_found,
        avg_gap_up_pct, fade_probability_pct, avg_5day_drift_pct,
        events: [{event_date, resolved_trading_date,
                  gap_pct, intraday_pct, drift_5d_pct}, ...],
        stale, reason?, as_of
      }
    """
    ticker = (request.args.get("ticker") or "").strip()
    event_type = (request.args.get("event_type") or "").strip()
    date_raw = (request.args.get("date") or "").strip()
    sentiment = (request.args.get("sentiment") or "").strip().lower() or None
    if sentiment and sentiment not in ("bullish", "bearish", "neutral"):
        return _err(400, "bad_sentiment",
                    "sentiment must be one of: bullish, bearish, neutral")
    magnitude_raw = request.args.get("magnitude")
    magnitude = None
    if magnitude_raw not in (None, ""):
        try:
            magnitude = float(magnitude_raw)
        except ValueError:
            return _err(400, "bad_magnitude", "magnitude must be a float")
    magnitude_band = _query_float("magnitude_band", 0.4)
    if magnitude_band < 0 or magnitude_band > 1:
        magnitude_band = 0.4
    lookback_days = _query_int("lookback_days", 365 * 5, min_v=30, max_v=3650)
    drift_window = _query_int("drift_window", 5, min_v=1, max_v=30)
    limit = _query_int("limit", 20, min_v=1, max_v=100)

    if not ticker:
        return _err(400, "missing_ticker",
                    "ticker is required, e.g. ticker=RELIANCE.NS")
    if not event_type:
        return _err(400, "missing_event_type",
                    "event_type is required, e.g. event_type=earnings")

    # Anchor date: explicit param wins, else today.
    import datetime as _dt
    if date_raw:
        try:
            anchor = _dt.date.fromisoformat(date_raw[:10])
        except ValueError:
            return _err(400, "bad_date",
                        f"date must be YYYY-MM-DD (got {date_raw!r})")
    else:
        anchor = _dt.date.today()

    # Pull historically-similar event dates for aggregation. Falls back to
    # single-event mode if event_history is unavailable or the DB lookup fails.
    # When `sentiment` / `magnitude` are passed, the historical sample is
    # narrowed to comparable OUTCOMES, not just same event_type.
    similar: List[_dt.date] = []
    try:
        from event_history import find_similar_event_dates
        db = _get_db() if _get_db else None
        if db is not None:
            similar = find_similar_event_dates(
                db, ticker, event_type,
                sentiment=sentiment,
                magnitude=magnitude,
                magnitude_band=magnitude_band,
                limit=limit, lookback_days=lookback_days,
                exclude_date=anchor,
            )
    except Exception as e:
        logger.debug("v1_backtest: similar-event lookup failed: %s", e)

    try:
        from event_backtest import backtest_event
    except Exception as e:
        return _err(503, "backtest_engine_unavailable",
                    f"event_backtest module not importable: {e}")

    try:
        summary = backtest_event(
            ticker, anchor,
            similar_dates=similar,
            drift_window=drift_window,
        )
    except Exception as e:
        logger.exception("v1_backtest: backtest_event failed for %s", ticker)
        return _err(500, "backtest_failed", str(e))

    return _ok(summary,
               ticker=ticker.upper(),
               event_type=event_type,
               anchor_date=anchor.isoformat(),
               similar_dates_found=len(similar),
               filters={"sentiment": sentiment, "magnitude": magnitude,
                        "magnitude_band": magnitude_band})


# ---------------------------------------------------------------------------
# Admin: trigger earnings reactions backfill
# ---------------------------------------------------------------------------

@bp.route("/api/v1/admin/concalls/reindex", methods=["POST"])
def v1_admin_concalls_reindex():
    """Trigger concall paragraph indexer.

    Query params:
      since   = optional ISO date (only index filings filed after)
      wipe    = '1' to drop existing paragraphs and rebuild
      limit   = optional cap on filings to process
    """
    since = (request.args.get("since") or "").strip() or None
    wipe = (request.args.get("wipe") or "").lower() in ("1", "true", "yes")
    limit_raw = request.args.get("limit")
    limit = int(limit_raw) if (limit_raw and limit_raw.isdigit()) else None
    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")
    try:
        from concall_indexer import run_indexer
        summary = run_indexer(db, since=since, wipe=wipe, limit=limit)
        return _ok(summary)
    except Exception as e:
        logger.exception(f"concall reindex failed: {e}")
        return _err(500, "internal_error", str(e))


@bp.route("/api/v1/admin/earnings-reactions/backfill", methods=["POST"])
def v1_admin_backfill_reactions():
    """Manually trigger an earnings-reactions backfill run.

    Query params:
      limit    = 1..500 (default: 50) — top-N tickers to walk
      quarters = 4..20  (default: 12) — quarters of history each

    Synchronous — returns when done. Long-running (~5-10 min for 50 tickers
    on yfinance free tier). For production we'd run this on a scheduler;
    this endpoint exists for ad-hoc seeding + post-deploy population.

    No auth currently (free-tier admin). Future: gate behind an admin token.
    """
    limit = _query_int("limit", 50, min_v=1, max_v=500)
    quarters = _query_int("quarters", 12, min_v=4, max_v=20)
    db = _get_db() if _get_db else None
    if not db:
        return _err(503, "db_unavailable", "Database not connected")
    try:
        from earnings_reactions_backfill import run_backfill
        summary = run_backfill(db, limit=limit, quarters=quarters)
        return _ok(summary)
    except Exception as e:
        logger.exception(f"backfill trigger failed: {e}")
        return _err(500, "internal_error", str(e))


# Expose the recorder so other modules can call it without importing
# api_public globally (avoids circular imports).
def get_sla_recorder():
    """Returns the _sla_record function. Use from scraper / notifications:
        from api_public import get_sla_recorder
        sla = get_sla_recorder()
        sla('bse_filing_to_alert', ticker, latency_ms)
    """
    return _sla_record


def _backfill_sla_from_history(db, max_rows: int = 500):
    """One-shot backfill at startup: join notification_log → events to
    compute realized publish→delivery and classify→delivery latencies for
    the last N successful deliveries. Seeds the in-process buffer so
    /api/v1/sla isn't empty before the first live alert flows through.
    """
    if not db:
        return 0
    try:
        cur = db.conn.cursor()
        cur.execute(
            """SELECT n.channel, n.ticker, n.sent_at, e.created_at, e.published_at
               FROM notification_log n
               LEFT JOIN events e ON e.event_id = n.signal_event_id
               WHERE n.status = 'sent' AND n.sent_at IS NOT NULL
               ORDER BY n.sent_at DESC LIMIT ?""",
            (max_rows,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.debug(f"SLA backfill query failed: {e}")
        return 0

    from datetime import datetime as _dt
    def _parse(ts):
        if not ts:
            return None
        if not isinstance(ts, str):
            return ts
        try:
            return _dt.fromisoformat(ts.replace('Z', '').split('.')[0])
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime as _p
                return _p(ts).replace(tzinfo=None)
            except Exception:
                return None

    seeded = 0
    for r in rows:
        sent = _parse(r.get("sent_at"))
        created = _parse(r.get("created_at"))
        pub = _parse(r.get("published_at"))
        ticker = r.get("ticker")
        if sent and created:
            ms = max(0.0, (sent - created).total_seconds() * 1000.0)
            # Sanity: cap at 7 days, drop negative (clock-skew artefacts)
            if 0 <= ms <= 7 * 24 * 3600 * 1000:
                _sla_record("event_to_delivery", ticker, ms)
                seeded += 1
        if sent and pub:
            ms = max(0.0, (sent - pub).total_seconds() * 1000.0)
            if 0 <= ms <= 7 * 24 * 3600 * 1000:
                _sla_record("publish_to_delivery", ticker, ms)
                seeded += 1
    return seeded


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(app, get_db: Callable):
    """Mount the v1 public API onto the Flask app.

    Call this after the existing v2/v3 blueprints so v1 wraps them by route
    lookup. Rate limiting is applied via flask-limiter when available.
    """
    global _get_db
    _get_db = get_db
    app.register_blueprint(bp)

    # Apply a per-IP rate limit if flask-limiter is configured on the app.
    # 60 req/min is generous enough for serious analysts, low enough to deter
    # abuse. Paid tier (future) will raise this when X-API-Key is present.
    try:
        limiter = app.extensions.get("limiter")
        if limiter:
            for ep in (
                "api_public.v1_index", "api_public.v1_movers", "api_public.v1_news",
                "api_public.v1_events", "api_public.v1_earnings", "api_public.v1_ipos",
                "api_public.v1_commodities", "api_public.v1_sectors",
                "api_public.v1_stock_snapshot", "api_public.v1_stock_profile",
                "api_public.v1_stock_financials", "api_public.v1_stock_technicals",
                "api_public.v1_stock_fundamentals", "api_public.v1_stock_screener",
                "api_public.v1_filings", "api_public.v1_filing_detail",
                "api_public.v1_earnings_reactions", "api_public.v1_event_related",
                "api_public.v1_smart_money", "api_public.v1_sla",
                "api_public.v1_concalls_search", "api_public.v1_backtest",
                "api_public.v1_alerts_log",
                "api_public.v1_earnings_forecast",
                "api_public.v1_earnings_forecast_history",
                "api_public.v1_earnings_forecast_list",
            ):
                try:
                    limiter.limit("60 per minute")(app.view_functions[ep])
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"v1 rate-limit wiring skipped: {e}")

    # Seed the SLA buffer from historical notification_log so the endpoint
    # shows real numbers from the get-go (not after the first live alert).
    try:
        db = get_db()
        seeded = _backfill_sla_from_history(db, max_rows=500)
        if seeded:
            logger.info(f"SLA backfill seeded {seeded} historical samples from notification_log")
    except Exception as e:
        logger.debug(f"SLA backfill skipped: {e}")

    logger.info("api_public registered: /api/v1 (movers, news, events, earnings, "
                "ipos, commodities, sectors, stocks/<t>/{profile,financials,technicals,fundamentals}, "
                "filings, filings/<id>, earnings/reactions/<t>, events/<id>/related, "
                "smart-money/<t>, sla)")
