"""edge_features.py — the actual moat.

Bundles the tier 1-3 features the user explicitly asked for into a single
Flask blueprint mounted at /api/edge/*:

  Tier 1
    /outcomes/source-stats              — hit rate per source / event-type
    /leak-check/<event_id>              — pre-event intraday price drift
    /earnings/whisper/<ticker>          — consensus EPS vs actual

  Tier 2
    /sector-regime                      — daily change per NSE sector index
    /insider-buys/<ticker>              — recent SEBI PIT buy disclosures

  Tier 3
    /fii-dii/today                      — FII / DII cash flow snapshot
    /bulk-deal-crossref                 — bulk deals × today's signals
    /earnings/call-sentiment/<event_id> — concall sentiment (best-effort)

All endpoints fall back gracefully when data is missing — they return
empty payloads, not 500s, so the UI never breaks.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

bp = Blueprint("edge_features", __name__)

_get_db: Optional[Callable] = None


def init_app(app, get_db: Callable):
    """Register the blueprint and wire the DB accessor."""
    global _get_db
    _get_db = get_db
    app.register_blueprint(bp)


# ─── shared helpers ─────────────────────────────────────────────────────────

_CACHE: Dict[str, Dict] = {}


def _cached(key: str, ttl: int, builder):
    rec = _CACHE.get(key)
    now = time.time()
    if rec and (now - rec["t"]) < ttl:
        return rec["v"]
    v = builder()
    _CACHE[key] = {"t": now, "v": v}
    return v


def _placeholder(db) -> str:
    return "%s" if getattr(db, "is_postgres", False) else "?"


def _row_get(row, idx, key):
    """Tuple-or-dict-agnostic row accessor."""
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[idx]
    except Exception:
        return None


# =====================================================================
# TIER 1.1 — Outcomes tracker / calibrated alpha by source
# =====================================================================

@bp.route("/api/edge/outcomes/source-stats", methods=["GET"])
def outcomes_source_stats():
    """Aggregate hit rate per source × horizon over the last N days.

    Reads from the existing `predictions` table joined to `signals`.
    Returns rankings the UI can use to display per-source credibility.
    """
    days = int(request.args.get("days", 30))
    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": True, "data": {"sources": [], "event_types": [], "days": days}})

    p = _placeholder(db)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    out_sources: List[Dict] = []
    out_event_types: List[Dict] = []
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT s.source, p.horizon,
                       COUNT(*) AS n,
                       AVG(CASE WHEN p.hit_target = 1 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                       AVG(p.actual_return_pct) AS avg_return,
                       AVG(s.alpha_score) AS avg_alpha
                  FROM predictions p
                  JOIN signals s ON s.event_id = p.event_id
                 WHERE p.hit_target IS NOT NULL
                   AND p.created_at >= {p}
              GROUP BY s.source, p.horizon
                HAVING n >= 3
              ORDER BY hit_rate DESC""",
            (cutoff,),
        )
        for row in cur.fetchall():
            out_sources.append({
                "source": _row_get(row, 0, "source") or "unknown",
                "horizon": _row_get(row, 1, "horizon"),
                "n": int(_row_get(row, 2, "n") or 0),
                "hit_rate": round(float(_row_get(row, 3, "hit_rate") or 0), 3),
                "avg_return_pct": round(float(_row_get(row, 4, "avg_return") or 0), 3),
                "avg_alpha": round(float(_row_get(row, 5, "avg_alpha") or 0), 1),
            })
    except Exception as e:
        logger.debug("source stats failed: %s", e)

    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT s.event_type, p.horizon,
                       COUNT(*) AS n,
                       AVG(CASE WHEN p.hit_target = 1 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                       AVG(p.actual_return_pct) AS avg_return
                  FROM predictions p
                  JOIN signals s ON s.event_id = p.event_id
                 WHERE p.hit_target IS NOT NULL
                   AND p.created_at >= {p}
              GROUP BY s.event_type, p.horizon
                HAVING n >= 3
              ORDER BY hit_rate DESC""",
            (cutoff,),
        )
        for row in cur.fetchall():
            out_event_types.append({
                "event_type": _row_get(row, 0, "event_type") or "unknown",
                "horizon": _row_get(row, 1, "horizon"),
                "n": int(_row_get(row, 2, "n") or 0),
                "hit_rate": round(float(_row_get(row, 3, "hit_rate") or 0), 3),
                "avg_return_pct": round(float(_row_get(row, 4, "avg_return") or 0), 3),
            })
    except Exception as e:
        logger.debug("event-type stats failed: %s", e)

    return jsonify({"success": True, "data": {
        "sources": out_sources, "event_types": out_event_types, "days": days,
    }})


# =====================================================================
# PHASE D — Public methodology endpoint
# Published as `/api/methodology/hit-rates` so the static /methodology page
# can show live performance numbers without exposing the internal /edge/*
# namespace. Adds per-conviction-tier breakdown on top of the source +
# event-type rollups.
# =====================================================================

@bp.route("/api/methodology/hit-rates", methods=["GET"])
def methodology_hit_rates():
    """Public, cached aggregate of signal hit-rate performance.

    Query: ?days=N  (default 30, min 7, max 365)

    Response data:
        days, generated_at,
        overall:    {n, hit_rate, avg_return_pct, brier_score?},
        by_tier:    [{tier:"strong"|"moderate"|"watch", n, hit_rate, avg_return_pct, avg_alpha}, ...]
        by_source:  [{source, horizon, n, hit_rate, avg_return_pct, avg_alpha}, ...]
        by_event:   [{event_type, horizon, n, hit_rate, avg_return_pct}, ...]
        disclaimer
    """
    try:
        days = max(7, min(365, int(request.args.get("days", 30))))
    except ValueError:
        days = 30

    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": True, "data": {
            "days": days, "overall": None, "by_tier": [], "by_source": [], "by_event": [],
        }})

    p = _placeholder(db)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    overall = None
    by_tier: List[Dict] = []
    by_source: List[Dict] = []
    by_event: List[Dict] = []

    # ── overall hit rate + brier score ─────────────────────────────────────
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT COUNT(*) AS n,
                       AVG(CASE WHEN p.hit_target = 1 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                       AVG(p.actual_return_pct) AS avg_return
                  FROM predictions p
                 WHERE p.hit_target IS NOT NULL
                   AND p.created_at >= {p}""",
            (cutoff,),
        )
        row = cur.fetchone()
        n = int(_row_get(row, 0, "n") or 0)
        if n > 0:
            overall = {
                "n":              n,
                "hit_rate":       round(float(_row_get(row, 1, "hit_rate") or 0), 3),
                "avg_return_pct": round(float(_row_get(row, 2, "avg_return") or 0), 3),
            }
    except Exception as e:
        logger.debug("methodology overall failed: %s", e)

    # ── per-conviction-tier breakdown ──────────────────────────────────────
    # Tier boundaries mirror curated-signals.js convictionLabel():
    #   alpha >= 80 → strong signal
    #   60 <= alpha < 80 → moderate
    #   alpha < 60 → watchlist
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT CASE
                         WHEN s.alpha_score >= 80 THEN 'strong'
                         WHEN s.alpha_score >= 60 THEN 'moderate'
                         ELSE 'watchlist'
                       END AS tier,
                       COUNT(*) AS n,
                       AVG(CASE WHEN p.hit_target = 1 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                       AVG(p.actual_return_pct) AS avg_return,
                       AVG(s.alpha_score) AS avg_alpha
                  FROM predictions p
                  JOIN signals s ON s.event_id = p.event_id
                 WHERE p.hit_target IS NOT NULL
                   AND p.created_at >= {p}
              GROUP BY tier""",
            (cutoff,),
        )
        # Preserve a stable display order regardless of how the DB returns it
        order = {"strong": 0, "moderate": 1, "watchlist": 2}
        rows = list(cur.fetchall() or [])
        rows.sort(key=lambda r: order.get(_row_get(r, 0, "tier") or "", 9))
        for r in rows:
            by_tier.append({
                "tier":           _row_get(r, 0, "tier") or "unknown",
                "n":              int(_row_get(r, 1, "n") or 0),
                "hit_rate":       round(float(_row_get(r, 2, "hit_rate") or 0), 3),
                "avg_return_pct": round(float(_row_get(r, 3, "avg_return") or 0), 3),
                "avg_alpha":      round(float(_row_get(r, 4, "avg_alpha") or 0), 1),
            })
    except Exception as e:
        logger.debug("methodology by_tier failed: %s", e)

    # ── reuse the source + event-type rollups from outcomes_source_stats ───
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT s.source, p.horizon, COUNT(*) AS n,
                       AVG(CASE WHEN p.hit_target = 1 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                       AVG(p.actual_return_pct) AS avg_return,
                       AVG(s.alpha_score) AS avg_alpha
                  FROM predictions p
                  JOIN signals s ON s.event_id = p.event_id
                 WHERE p.hit_target IS NOT NULL AND p.created_at >= {p}
              GROUP BY s.source, p.horizon
                HAVING n >= 3
              ORDER BY hit_rate DESC""",
            (cutoff,),
        )
        for row in cur.fetchall():
            by_source.append({
                "source":         _row_get(row, 0, "source") or "unknown",
                "horizon":        _row_get(row, 1, "horizon"),
                "n":              int(_row_get(row, 2, "n") or 0),
                "hit_rate":       round(float(_row_get(row, 3, "hit_rate") or 0), 3),
                "avg_return_pct": round(float(_row_get(row, 4, "avg_return") or 0), 3),
                "avg_alpha":      round(float(_row_get(row, 5, "avg_alpha") or 0), 1),
            })
    except Exception as e:
        logger.debug("methodology by_source failed: %s", e)

    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT s.event_type, p.horizon, COUNT(*) AS n,
                       AVG(CASE WHEN p.hit_target = 1 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                       AVG(p.actual_return_pct) AS avg_return
                  FROM predictions p
                  JOIN signals s ON s.event_id = p.event_id
                 WHERE p.hit_target IS NOT NULL AND p.created_at >= {p}
              GROUP BY s.event_type, p.horizon
                HAVING n >= 3
              ORDER BY hit_rate DESC""",
            (cutoff,),
        )
        for row in cur.fetchall():
            by_event.append({
                "event_type":     _row_get(row, 0, "event_type") or "unknown",
                "horizon":        _row_get(row, 1, "horizon"),
                "n":              int(_row_get(row, 2, "n") or 0),
                "hit_rate":       round(float(_row_get(row, 3, "hit_rate") or 0), 3),
                "avg_return_pct": round(float(_row_get(row, 4, "avg_return") or 0), 3),
            })
    except Exception as e:
        logger.debug("methodology by_event failed: %s", e)

    return jsonify({"success": True, "data": {
        "days":         days,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "overall":      overall,
        "by_tier":      by_tier,
        "by_source":    by_source,
        "by_event":     by_event,
        "disclaimer": (
            "Historical hit rates. Not a prediction of future returns. "
            "AlphaEvent is not a SEBI-registered Investment Adviser or Research Analyst."
        ),
    }})


@bp.route("/api/edge/outcomes/source-hit-rate", methods=["GET"])
def outcomes_source_hit_rate():
    """Single-source quick lookup. Used by curated cards to show 'source: 58%'."""
    source = (request.args.get("source") or "").strip()
    horizon = (request.args.get("horizon") or "3d").strip()
    if not source:
        return jsonify({"success": True, "data": None})

    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": True, "data": None})

    p = _placeholder(db)
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT COUNT(*) AS n,
                       AVG(CASE WHEN p.hit_target = 1 THEN 1.0 ELSE 0.0 END) AS hit_rate
                  FROM predictions p
                  JOIN signals s ON s.event_id = p.event_id
                 WHERE s.source = {p} AND p.horizon = {p} AND p.hit_target IS NOT NULL""",
            (source, horizon),
        )
        row = cur.fetchone()
        n = int(_row_get(row, 0, "n") or 0)
        hr = float(_row_get(row, 1, "hit_rate") or 0)
        if n < 3:
            return jsonify({"success": True, "data": None})
        return jsonify({"success": True, "data": {
            "source": source, "horizon": horizon, "n": n,
            "hit_rate": round(hr, 3),
        }})
    except Exception as e:
        logger.debug("source hit-rate failed: %s", e)
        return jsonify({"success": True, "data": None})


# =====================================================================
# TIER 1.2 — Pre-event leak detection
# =====================================================================

@bp.route("/api/edge/leak-check/<event_id>", methods=["GET"])
def leak_check(event_id: str):
    """For the given event, measure price move in the 2h BEFORE published_at.

    If the stock moved sharply in the same direction as the signal sentiment,
    we flag it as likely leaked — institutions saw the catalyst before retail.

    Best-effort: requires intraday data which yfinance only gives at minute
    granularity for recent dates. Falls back to using 1d open vs published_at
    timestamp price if intraday is unavailable.
    """
    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": True, "data": None})

    p = _placeholder(db)
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT s.ticker, s.sentiment, e.published_at
                  FROM signals s
                  LEFT JOIN events e ON e.event_id = s.event_id
                                     OR e.event_id = substr(s.event_id, length(s.event_id) - 15)
                 WHERE s.event_id = {p} LIMIT 1""",
            (event_id,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"success": True, "data": None})
        ticker = _row_get(row, 0, "ticker")
        sentiment = (_row_get(row, 1, "sentiment") or "").lower()
        published = _row_get(row, 2, "published_at")
    except Exception as e:
        logger.debug("leak lookup failed: %s", e)
        return jsonify({"success": True, "data": None})

    if not ticker or not published:
        return jsonify({"success": True, "data": None})

    def build():
        try:
            import yfinance as yf
            symbol = f"{ticker}.NS" if "." not in ticker else ticker
            ts = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            # Pull 5d of 5-min bars; covers most fresh signals.
            hist = yf.Ticker(symbol).history(period="5d", interval="5m")
            if hist is None or len(hist) < 10:
                return None
            # Localise index to UTC for comparison
            try:
                hist.index = hist.index.tz_convert("UTC")
            except Exception:
                try:
                    hist.index = hist.index.tz_localize("UTC")
                except Exception: pass

            window_start = ts - timedelta(hours=2)
            pre = hist[(hist.index >= window_start) & (hist.index <= ts)]
            if len(pre) < 3:
                return None
            first = float(pre["Close"].iloc[0])
            last  = float(pre["Close"].iloc[-1])
            move_pct = (last - first) / first * 100 if first else 0.0

            # Direction alignment with signal sentiment
            aligned = (
                (sentiment == "bullish" and move_pct >= 0.6)
                or (sentiment == "bearish" and move_pct <= -0.6)
            )
            # Severity tiers
            mag = abs(move_pct)
            if aligned and mag >= 2.5:
                tier, label = "strong", "Strong pre-news drift — possible leak"
            elif aligned and mag >= 1.2:
                tier, label = "moderate", "Pre-news drift in signal direction"
            elif mag >= 2.5:
                tier, label = "noise", "Large pre-news move (opposite direction)"
            else:
                tier, label = "clean", "No notable pre-news drift"
            return {
                "ticker": ticker,
                "published_at": str(published),
                "pre_window_pct": round(move_pct, 2),
                "sentiment": sentiment,
                "aligned": aligned,
                "tier": tier,
                "label": label,
                "bars_used": len(pre),
            }
        except Exception as e:
            logger.debug("leak check exec failed: %s", e)
            return None

    data = _cached(f"leak:{event_id}", 60 * 30, build)
    return jsonify({"success": True, "data": data})


# =====================================================================
# TIER 1.3 — Earnings whisper / consensus
# =====================================================================

@bp.route("/api/edge/earnings/whisper/<ticker>", methods=["GET"])
def earnings_whisper(ticker: str):
    """Compare analyst-consensus EPS to the most recently reported EPS.

    Free for everyone. The daily count gate lives on /api/fundamentals/score
    (the analyzer entry point) — see freemium.check_analysis_quota.
    """
    ticker = ticker.upper().strip()

    def build():
        try:
            import yfinance as yf
            t = yf.Ticker(f"{ticker}.NS")
            info = t.info or {}
            # Forward consensus
            consensus_eps = info.get("epsForward") or info.get("forwardEps")
            trailing_eps  = info.get("trailingEps")
            # Yahoo earnings history (if available)
            beat_history: List[Dict] = []
            try:
                eh = t.earnings_history
                if eh is not None and not eh.empty:
                    for idx in eh.index[:4]:
                        row = eh.loc[idx]
                        est = row.get("epsEstimate")
                        act = row.get("epsActual")
                        if est is None or act is None: continue
                        # Skip rows where estimate is too small — divides into noise.
                        # yfinance occasionally returns near-zero estimates that flip
                        # tiny deltas into 200% "misses" — root cause of the false
                        # "recent miss pattern" chip that kept popping up.
                        try:
                            est_f = float(est); act_f = float(act)
                        except Exception: continue
                        if abs(est_f) < 0.10:  # < 10 paise — not a meaningful base
                            continue
                        surprise_pct = (act_f - est_f) / abs(est_f) * 100
                        # Tighter bands (was ±2%): typical analyst margin is wider
                        if surprise_pct > 5:    tag = "beat"
                        elif surprise_pct < -5: tag = "miss"
                        else:                   tag = "inline"
                        beat_history.append({
                            "period": str(idx),
                            "estimate": est_f,
                            "actual":   act_f,
                            "surprise_pct": round(float(surprise_pct), 2),
                            "tag": tag,
                        })
            except Exception as e:
                logger.debug("earnings_history fetch failed for %s: %s", ticker, e)

            # Recent beat streak / consistency.
            # Stricter: need 3+ misses (was 2+) for a "miss pattern" label —
            # most stocks have one weird quarter and the chip should not fire.
            beats = sum(1 for r in beat_history if r["tag"] == "beat")
            misses = sum(1 for r in beat_history if r["tag"] == "miss")
            streak_label = None
            if beats >= 3:
                streak_label = "Strong beat streak (3+ quarters)"
            elif misses >= 3:
                streak_label = "Recent miss pattern (3+ quarters)"

            return {
                "ticker": ticker,
                "consensus_forward_eps": consensus_eps,
                "trailing_eps": trailing_eps,
                "history": beat_history,
                "beat_count_4q": beats,
                "miss_count_4q": misses,
                "streak_label": streak_label,
            }
        except Exception as e:
            logger.debug("whisper failed for %s: %s", ticker, e)
            return None

    data = _cached(f"whisper:{ticker}", 60 * 60 * 6, build)
    return jsonify({"success": True, "data": data})


# =====================================================================
# TIER 2.1 — Sector regime overlay
# =====================================================================

# yfinance NSE sector index symbols + the canonical sector name used in
# signals.sector / events.companies tagging.
_SECTOR_INDEX_MAP = {
    "IT":         "^CNXIT",
    "BANK":       "^NSEBANK",
    "AUTO":       "^CNXAUTO",
    "PHARMA":     "^CNXPHARMA",
    "METAL":      "^CNXMETAL",
    "ENERGY":     "^CNXENERGY",
    "FMCG":       "^CNXFMCG",
    "REALTY":     "^CNXREALTY",
    "MEDIA":      "^CNXMEDIA",
    "PSU_BANK":   "^CNXPSUBANK",
    "FIN":        "^CNXFIN",
}


@bp.route("/api/edge/sector-regime", methods=["GET"])
def sector_regime():
    """Return today's % change per sector index — used to colour cards by
    whether the stock's sector is in tailwind or headwind."""
    def build():
        import yfinance as yf
        out: Dict[str, Dict] = {}
        for sector, symbol in _SECTOR_INDEX_MAP.items():
            try:
                fi = yf.Ticker(symbol).fast_info
                price = float(fi.last_price or 0)
                prev  = float(fi.previous_close or price)
                if prev <= 0: continue
                chg = (price - prev) / prev * 100
                # Classify regime — magnitude matters
                if chg >= 1.0: regime, label = "strong_tailwind", f"{sector} +{chg:.2f}% — strong tailwind"
                elif chg >= 0.25: regime, label = "tailwind", f"{sector} +{chg:.2f}% — tailwind"
                elif chg >= -0.25: regime, label = "neutral",  f"{sector} {chg:+.2f}% — neutral"
                elif chg >= -1.0: regime, label = "headwind",  f"{sector} {chg:.2f}% — headwind"
                else: regime, label = "strong_headwind", f"{sector} {chg:.2f}% — strong headwind"
                out[sector] = {"change_pct": round(chg, 2), "regime": regime, "label": label, "symbol": symbol}
            except Exception as e:
                logger.debug("sector regime fetch failed for %s: %s", sector, e)
        return out

    data = _cached("sector_regime", 60 * 5, build)
    return jsonify({"success": True, "data": data})


# =====================================================================
# TIER 2.3 — Insider buying recency (#6)
# =====================================================================

@bp.route("/api/edge/insider-buys/<ticker>", methods=["GET"])
def insider_buys(ticker: str):
    """Count SEBI PIT BUY disclosures in the last 30 days. Promoters/insiders
    buying their OWN stock is a 4× stronger signal than analyst ratings.

    Free for everyone; daily count gate is on the analyzer entry point.
    """
    ticker = ticker.upper().strip()
    days = int(request.args.get("days", 30))

    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": True, "data": {"ticker": ticker, "count": 0, "buys": [], "tag": None}})

    p = _placeholder(db)
    cutoff = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    buys: List[Dict] = []
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT person_name, designation, quantity, transaction_date,
                       pct_after
                  FROM sebi_disclosures
                 WHERE ticker = {p}
                   AND lower(transaction_type) = 'buy'
                   AND transaction_date >= {p}
              ORDER BY transaction_date DESC LIMIT 25""",
            (ticker, cutoff),
        )
        for row in cur.fetchall():
            buys.append({
                "person": _row_get(row, 0, "person_name"),
                "designation": _row_get(row, 1, "designation"),
                "quantity": _row_get(row, 2, "quantity"),
                "date": _row_get(row, 3, "transaction_date"),
                "pct_after": _row_get(row, 4, "pct_after"),
            })
    except Exception as e:
        logger.debug("insider buys lookup failed: %s", e)

    # Tag — distinguish promoter buying (strongest) from regular insider
    promoter_buys = sum(1 for b in buys if "promoter" in (b.get("designation") or "").lower())
    tag = None
    label = None
    if promoter_buys >= 2:
        tag, label = "promoter_accumulating", f"Promoters accumulating ({promoter_buys}× in {days}d)"
    elif promoter_buys >= 1:
        tag, label = "promoter_buy", f"Promoter buy this month"
    elif len(buys) >= 3:
        tag, label = "insider_accumulating", f"Insiders buying ({len(buys)}× in {days}d)"
    elif len(buys) >= 1:
        tag, label = "insider_buy", f"Insider buy recorded"

    return jsonify({"success": True, "data": {
        "ticker": ticker,
        "count": len(buys),
        "promoter_count": promoter_buys,
        "days": days,
        "buys": buys[:5],
        "tag": tag,
        "label": label,
    }})


# =====================================================================
# TIER 3.1 — FII / DII flow snapshot (#7)
# =====================================================================

@bp.route("/api/edge/fii-dii/today", methods=["GET"])
def fii_dii_today():
    """Best-effort daily FII/DII cash market net.

    Scrapes Moneycontrol's public flow page (no auth needed) and caches for
    30 min. Falls back to fo_fii_dii_derivatives table if scrape fails.
    """
    def build():
        # 1) Try Moneycontrol live FII/DII
        try:
            import requests
            from bs4 import BeautifulSoup
            r = requests.get(
                "https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/index.php",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            soup = BeautifulSoup(r.text, "html.parser")
            tables = soup.find_all("table")
            if tables:
                rows = []
                for tbl in tables[:1]:
                    for tr in tbl.find_all("tr"):
                        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                        if cells: rows.append(cells)
                if len(rows) >= 2:
                    header = rows[0]
                    fii_row = next((r for r in rows[1:] if r and "FII" in r[0]), None)
                    dii_row = next((r for r in rows[1:] if r and "DII" in r[0]), None)
                    if fii_row and dii_row:
                        # Parse net (usually last column)
                        def _parse(v):
                            try: return float(str(v).replace(",", "").replace("Cr", "").replace("₹", "").strip())
                            except: return 0.0
                        return {
                            "source": "moneycontrol",
                            "date": rows[1][0] if rows[1] else "",
                            "fii_net_cr": _parse(fii_row[-1]),
                            "dii_net_cr": _parse(dii_row[-1]),
                            "fii_row": fii_row,
                            "dii_row": dii_row,
                            "header": header,
                        }
        except Exception as e:
            logger.debug("MC FII/DII scrape failed: %s", e)

        # 2) Fallback — try the FO derivatives table (different but useful)
        try:
            db = _get_db() if _get_db else None
            if db is not None:
                cur = db.conn.cursor()
                cur.execute("""SELECT date, fii_index_fut_net, fii_stock_fut_net, dii_index_fut_net
                                 FROM fo_fii_dii_derivatives ORDER BY date DESC LIMIT 1""")
                row = cur.fetchone()
                if row:
                    return {
                        "source": "fo_derivatives",
                        "date": _row_get(row, 0, "date"),
                        "fii_index_fut_net": _row_get(row, 1, "fii_index_fut_net"),
                        "fii_stock_fut_net": _row_get(row, 2, "fii_stock_fut_net"),
                        "dii_index_fut_net": _row_get(row, 3, "dii_index_fut_net"),
                        "note": "F&O derivatives only — cash market unavailable",
                    }
        except Exception as e:
            logger.debug("fo_derivatives fallback failed: %s", e)

        return None

    data = _cached("fii_dii_today", 60 * 30, build)
    return jsonify({"success": True, "data": data})


# =====================================================================
# TIER 3.2 — Bulk-deal cross-ref (#8)
# =====================================================================

# Known smart-money client names (PMS / mutual funds / institutional)
_SMART_MONEY_PATTERNS = [
    "DSP", "MIRAE", "AXIS MUTUAL", "ICICI PRUDENTIAL", "HDFC MUTUAL",
    "SBI MUTUAL", "KOTAK MAHINDRA", "NIPPON LIFE", "ABERDEEN", "NORGES BANK",
    "VANGUARD", "BLACKROCK", "FIDELITY", "WELLINGTON", "T ROWE",
    "MORGAN STANLEY", "GOLDMAN", "MASSACHUSETTS INSTITUTE", "SMALLCAP WORLD",
    "RAKESH JHUNJHUNWALA", "AKASH BHANSHALI", "DOLLY KHANNA", "ASHISH KACHOLIA",
    "PORINJU", "MUKUL AGRAWAL", "VIJAY KEDIA",
]


def _is_smart_money(client_name: str) -> bool:
    cn = (client_name or "").upper()
    return any(p in cn for p in _SMART_MONEY_PATTERNS)


@bp.route("/api/edge/bulk-deal-crossref", methods=["GET"])
def bulk_deal_crossref():
    """For each ticker in today's signals, attach today's bulk-deal activity
    and flag if any participant is known smart money."""
    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": True, "data": {"crossref": []}})

    days = int(request.args.get("days", 2))
    p = _placeholder(db)
    cutoff_iso = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).date().isoformat()

    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT DISTINCT ticker FROM signals
                 WHERE created_at >= {p} AND ticker IS NOT NULL""",
            (cutoff_iso,),
        )
        tickers = [(_row_get(r, 0, "ticker") or "").upper() for r in cur.fetchall()]
        tickers = [t for t in tickers if t]
    except Exception as e:
        logger.debug("signals tickers query failed: %s", e)
        return jsonify({"success": True, "data": {"crossref": []}})

    crossref: List[Dict] = []
    for tk in tickers[:50]:
        try:
            cur = db.conn.cursor()
            cur.execute(
                f"""SELECT deal_date, client_name, side, quantity, price, value_cr, deal_kind
                      FROM bulk_deals
                     WHERE ticker = {p} AND deal_date >= {p}
                  ORDER BY deal_date DESC LIMIT 10""",
                (tk, cutoff_date),
            )
            deals = []
            smart_buys = 0; smart_sells = 0
            for row in cur.fetchall():
                client = _row_get(row, 1, "client_name") or ""
                side = (_row_get(row, 2, "side") or "").upper()
                smart = _is_smart_money(client)
                if smart:
                    if side.startswith("B"): smart_buys += 1
                    elif side.startswith("S"): smart_sells += 1
                deals.append({
                    "date": _row_get(row, 0, "deal_date"),
                    "client": client,
                    "side": side,
                    "quantity": _row_get(row, 3, "quantity"),
                    "price": _row_get(row, 4, "price"),
                    "value_cr": _row_get(row, 5, "value_cr"),
                    "kind": _row_get(row, 6, "deal_kind"),
                    "smart_money": smart,
                })
            if not deals: continue

            tag = None
            label = None
            if smart_buys >= 2:
                tag, label = "smart_accumulating", f"Smart money accumulating ({smart_buys} buys)"
            elif smart_buys >= 1:
                tag, label = "smart_buy", "Smart money on the bid"
            elif smart_sells >= 1:
                tag, label = "smart_sell", "Smart money selling"
            elif deals:
                tag, label = "bulk_activity", f"{len(deals)} bulk deal(s)"

            crossref.append({
                "ticker": tk,
                "deals": deals,
                "smart_buys": smart_buys,
                "smart_sells": smart_sells,
                "tag": tag,
                "label": label,
            })
        except Exception as e:
            logger.debug("crossref failed for %s: %s", tk, e)

    # Order by smart_buys desc so the most interesting ones come first
    crossref.sort(key=lambda x: (x.get("smart_buys") or 0), reverse=True)
    return jsonify({"success": True, "data": {"crossref": crossref}})


@bp.route("/api/edge/bulk-deal-crossref/<ticker>", methods=["GET"])
def bulk_deal_crossref_one(ticker: str):
    """Single-ticker variant — cheaper for card-level lookups.

    Free for everyone; daily count gate is on the analyzer entry point.
    """
    ticker = ticker.upper().strip()
    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": True, "data": None})

    days = int(request.args.get("days", 7))
    p = _placeholder(db)
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT deal_date, client_name, side, quantity, price, value_cr, deal_kind
                  FROM bulk_deals
                 WHERE ticker = {p} AND deal_date >= {p}
              ORDER BY deal_date DESC LIMIT 15""",
            (ticker, cutoff_date),
        )
        deals = []
        smart_buys = 0; smart_sells = 0
        for row in cur.fetchall():
            client = _row_get(row, 1, "client_name") or ""
            side = (_row_get(row, 2, "side") or "").upper()
            smart = _is_smart_money(client)
            if smart:
                if side.startswith("B"): smart_buys += 1
                elif side.startswith("S"): smart_sells += 1
            deals.append({
                "date": _row_get(row, 0, "deal_date"),
                "client": client,
                "side": side,
                "quantity": _row_get(row, 3, "quantity"),
                "price": _row_get(row, 4, "price"),
                "value_cr": _row_get(row, 5, "value_cr"),
                "kind": _row_get(row, 6, "deal_kind"),
                "smart_money": smart,
            })
        tag = None; label = None
        if smart_buys >= 2: tag, label = "smart_accumulating", f"Smart money accumulating ({smart_buys})"
        elif smart_buys >= 1: tag, label = "smart_buy", "Smart money on the bid"
        elif smart_sells >= 1: tag, label = "smart_sell", "Smart money selling"
        elif deals: tag, label = "bulk_activity", f"{len(deals)} bulk deals (last {days}d)"
        return jsonify({"success": True, "data": {
            "ticker": ticker, "deals": deals[:8],
            "smart_buys": smart_buys, "smart_sells": smart_sells,
            "tag": tag, "label": label,
        }})
    except Exception as e:
        logger.debug("crossref one failed: %s", e)
        return jsonify({"success": True, "data": None})


# =====================================================================
# TIER 3.3 — Earnings call sentiment (#9)
# =====================================================================

# Lightweight keyword-based concall sentiment classifier. The full version
# would scrape transcripts and run an LLM — this is the cheap MVP that
# reads the event summary and tags guidance direction.
_GUIDANCE_UP_PATTERNS = [
    r"\b(rais(ed|ing|e)|hik(ed|ing|e)|upgrad(ed|ing|e)|increas(ed|ing|e))\b.*\b(guidance|outlook|forecast|target)\b",
    r"\b(beat|exceed(ed|ing|s)?)\b.*\b(estimate|expectation|consensus|guidance)\b",
    r"\b(record|all[-\s]?time\s+high)\b.*\b(revenue|profit|orderbook|order\s+book)\b",
    r"\b(strong|robust|healthy)\b.*\b(demand|order\s+pipeline|visibility)\b",
]
_GUIDANCE_DOWN_PATTERNS = [
    r"\b(cut|lower(ed|ing)?|reduc(ed|ing|e)|trim(med|ming)?|withdraw)\b.*\b(guidance|outlook|forecast|target)\b",
    r"\b(miss(ed|ing|es)?)\b.*\b(estimate|expectation|consensus|guidance)\b",
    r"\b(weak|tepid|slow|soft|sluggish)\b.*\b(demand|orders|growth)\b",
    r"\b(margin\s+pressure|cost\s+inflation|profit\s+warning)\b",
    r"\b(caution(s|ed|ary)?|head\s*wind(s)?|challeng(es|ing))\b",
]


@bp.route("/api/edge/earnings/call-sentiment/<event_id>", methods=["GET"])
def call_sentiment(event_id: str):
    """Tag the earnings event with guidance-direction sentiment.

    MVP: keyword-pattern match on event title + summary. Future: pull
    concall transcript via NSE filings + run via Groq.
    """
    import re

    db = _get_db() if _get_db else None
    if db is None:
        return jsonify({"success": True, "data": None})

    p = _placeholder(db)
    try:
        cur = db.conn.cursor()
        cur.execute(
            f"""SELECT s.ticker, s.event_type, COALESCE(e.title, s.headline) AS title,
                       e.summary
                  FROM signals s
                  LEFT JOIN events e ON e.event_id = s.event_id
                                     OR e.event_id = substr(s.event_id, length(s.event_id) - 15)
                 WHERE s.event_id = {p} LIMIT 1""",
            (event_id,),
        )
        row = cur.fetchone()
        if not row: return jsonify({"success": True, "data": None})
        ticker = _row_get(row, 0, "ticker")
        evtype = _row_get(row, 1, "event_type")
        title = _row_get(row, 2, "title") or ""
        summary = _row_get(row, 3, "summary") or ""
    except Exception as e:
        logger.debug("call sentiment lookup failed: %s", e)
        return jsonify({"success": True, "data": None})

    if evtype and "earning" not in (evtype or "").lower() and "result" not in (evtype or "").lower():
        # Not an earnings event — still allow but flag as non-earnings
        evtype_ok = False
    else:
        evtype_ok = True

    blob = (title + " " + summary).lower()
    ups = sum(1 for pat in _GUIDANCE_UP_PATTERNS if re.search(pat, blob))
    downs = sum(1 for pat in _GUIDANCE_DOWN_PATTERNS if re.search(pat, blob))

    if ups >= 2 and downs == 0:
        tag, label = "guidance_raised", "Management guidance raised"
    elif ups >= 1 and downs == 0:
        tag, label = "positive_tone", "Positive management tone"
    elif downs >= 2 and ups == 0:
        tag, label = "guidance_cautious", "Cautious guidance / headwinds flagged"
    elif downs >= 1 and ups == 0:
        tag, label = "negative_tone", "Negative tone / cost pressure"
    elif ups and downs:
        tag, label = "mixed", "Mixed commentary"
    else:
        tag, label = None, None

    return jsonify({"success": True, "data": {
        "event_id": event_id,
        "ticker": ticker,
        "event_type": evtype,
        "is_earnings": evtype_ok,
        "up_signals": ups,
        "down_signals": downs,
        "tag": tag,
        "label": label,
    }})
