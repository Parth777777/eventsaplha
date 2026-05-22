"""
Backend API extension — Phase 1.5 endpoints and scheduled jobs.

Kept separate from `api.py` so the core app stays stable. Wired into api.py
by calling register_routes(app, get_db) + register_jobs(scheduler, get_db)
at startup.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional  # noqa: F401

from flask import Flask, Response, jsonify, request

# Ensure the scraper package resolves the same way as api.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))

from cache import cache_get, cache_set  # noqa: E402  (sibling module)

logger = logging.getLogger(__name__)


# ---------- helpers ----------

def _ok(payload: Any, **extra) -> Response:
    body = {"success": True, "data": payload}
    body.update(extra)
    return jsonify(body)


def _err(msg: str, code: int = 500) -> Response:
    return jsonify({"success": False, "error": msg}), code


# ============================================================
# Routes
# ============================================================

def register_routes(app: Flask, get_db: Callable) -> None:
    """Attach all new endpoints to the Flask app."""

    # Pull the centralized admin gate from api.py. Falls back to a no-op
    # decorator if the symbol moves so this module never crashes the boot.
    try:
        from api import require_admin
    except Exception:  # pragma: no cover
        def require_admin(f):  # type: ignore[no-redef]
            return f

    # ---- Glossary ----
    from jargon.loader import get_glossary, llm_define

    glossary = get_glossary()

    @app.route("/api/glossary/<term>", methods=["GET"])
    def get_glossary_term(term: str):
        glossary.attach_db(get_db())
        entry = glossary.lookup(term)
        if entry:
            return _ok(entry)
        # fallback to LLM
        defn = llm_define(term)
        if defn:
            glossary.cache_put(term, defn["plain_english"], defn.get("one_line_example", ""))
            return _ok({
                "term": term, **defn,
                "verified_by": "llm", "registers": ["auto"],
            })
        return _err(f"Unknown term: {term}", 404)

    @app.route("/api/glossary/search", methods=["GET"])
    def search_glossary():
        q = request.args.get("q", "").strip()
        if not q:
            return _ok([])
        return _ok(glossary.search(q, limit=20))

    @app.route("/api/glossary/batch", methods=["GET"])
    def glossary_batch():
        """Return the full list of canonical terms so frontend can build a local matcher."""
        cached = cache_get("glossary:terms")
        if cached is None:
            cached = glossary.all_terms()
            cache_set("glossary:terms", cached, ttl=3600)
        return _ok(cached)

    # ---- Promoter ----
    @app.route("/api/stock/<ticker>/promoter", methods=["GET"])
    def stock_promoter(ticker: str):
        from fundamentals.promoter_scraper import compute_trend_flags

        db = get_db()
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        try:
            cursor = db.conn.cursor()
            cursor.execute(
                f"""SELECT quarter_end, promoter_pct, promoter_pledge_pct, fii_pct, dii_pct,
                            public_pct, mutual_fund_pct, insurance_pct
                      FROM promoter_holdings
                     WHERE ticker = {p}
                     ORDER BY quarter_end ASC""",
                (ticker.upper(),),
            )
            rows_raw = cursor.fetchall()
            rows: List[Dict] = []
            for r in rows_raw:
                if isinstance(r, dict):
                    rows.append(r)
                else:
                    rows.append({
                        "quarter_end": r[0],
                        "promoter_pct": r[1],
                        "promoter_pledge_pct": r[2],
                        "fii_pct": r[3],
                        "dii_pct": r[4],
                        "public_pct": r[5],
                        "mutual_fund_pct": r[6],
                        "insurance_pct": r[7],
                    })
            trend = compute_trend_flags(rows)
            return _ok({"ticker": ticker.upper(), "quarters": rows, "trend_flags": trend})
        except Exception as exc:
            logger.warning("promoter endpoint failed: %s", exc)
            return _err("lookup failed")

    # ---- Forensics v2 (fine-print, pump-dump, article) ----
    @app.route("/api/stock/<ticker>/pump-dump", methods=["GET"])
    def stock_pump_dump(ticker: str):
        from forensics.pump_dump import compute

        try:
            return _ok(compute(get_db(), ticker))
        except Exception as exc:
            logger.warning("pump-dump failed: %s", exc)
            return _err("lookup failed")

    @app.route("/api/forensics/article", methods=["POST"])
    def forensics_article():
        """Analyse arbitrary article text — rule-based always, LLM if flagged."""
        body = request.get_json(silent=True) or {}
        text = body.get("text", "")
        use_llm = bool(body.get("use_llm", True))
        if not text:
            return _err("text required", 400)
        from forensics.article_forensics import analyze
        from forensics.fine_print import analyze_and_summarise

        return _ok({
            "article_forensics": analyze(text, use_llm=use_llm),
            "fine_print": analyze_and_summarise(text),
        })

    @app.route("/api/stock/<ticker>/forensics-full", methods=["GET"])
    def stock_forensics_full(ticker: str):
        """Aggregate view combining pump-dump + latest signal's manipulation data."""
        db = get_db()
        from forensics.pump_dump import compute

        pump = compute(db, ticker)
        # Latest fused manipulation score for this ticker
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        cursor = db.conn.cursor()
        cursor.execute(
            f"""SELECT s.event_id, s.headline, s.created_at, ms.score, ms.band, ms.reasons
                  FROM signals s
                  LEFT JOIN manipulation_scores ms ON ms.event_id = s.event_id
                 WHERE s.ticker = {p}
                 ORDER BY s.created_at DESC LIMIT 10""",
            (ticker.upper(),),
        )
        latest = []
        for r in cursor.fetchall():
            if isinstance(r, dict):
                latest.append(r)
            else:
                import json as _json
                try:
                    reasons = _json.loads(r[5] or "[]")
                except Exception:
                    reasons = []
                latest.append({
                    "event_id": r[0], "headline": r[1], "created_at": str(r[2]),
                    "score": r[3], "band": r[4], "reasons": reasons,
                })
        return _ok({
            "ticker": ticker.upper(),
            "pump_dump": pump,
            "recent_signals": latest,
        })

    @app.route("/api/signal/<event_id>/reasoning", methods=["GET"])
    def signal_reasoning(event_id: str):
        """Return the structured why-this-pick chain for a signal.

        Cached in DB column when produced by the scraper; falls back to
        computing fresh + caching the response so we don't recompute every hit.
        """
        import json as _json
        cache_key = f"reasoning:{event_id}"
        cached_resp = cache_get(cache_key)
        if cached_resp is not None:
            return _ok(cached_resp)

        db = get_db()
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        cursor = db.conn.cursor()
        cursor.execute(
            f"""SELECT event_id, event_type, ticker, company, alpha_score, confidence,
                       regime, entry_price, sentiment, magnitude, impact_score,
                       source, headline, link, intent, manipulation_score,
                       volume_confirmation, obv_divergence_flag, reasoning
                  FROM signals WHERE event_id = {p}""",
            (event_id,),
        )
        row = cursor.fetchone()
        if not row:
            return _err("signal not found", 404)
        if isinstance(row, dict):
            sig = row
        else:
            sig = dict(zip([c[0] for c in cursor.description], row))

        cached_db = sig.get("reasoning")
        if cached_db:
            try:
                payload = _json.loads(cached_db)
                cache_set(cache_key, payload, ttl=600)
                return _ok(payload)
            except Exception:
                pass

        try:
            from reasoning_engine import explain

            fresh = explain(db, sig)
            cache_set(cache_key, fresh, ttl=600)
            return _ok(fresh)
        except Exception as exc:
            logger.warning("reasoning failed event=%s: %s", event_id, exc)
            return _err("reasoning unavailable")

    @app.route("/api/stock/<ticker>/volume", methods=["GET"])
    def stock_volume(ticker: str):
        """OBV + volume surge analysis for a ticker.

        Returns 60 days of closes, volumes, computed OBV series, divergence flag,
        surge ratio. Lightweight enough to render a sparkline in the popup.
        """
        cached = cache_get(f"vol:{ticker.upper()}")
        if cached is not None:
            return _ok(cached)
        try:
            import yfinance as yf  # type: ignore

            from quant_layer import VolumeAnalyzer

            symbol = ticker.upper()
            yf_sym = symbol if "." in symbol else f"{symbol}.NS"
            hist = yf.Ticker(yf_sym).history(period="90d", interval="1d")
            if hist is None or len(hist) < 25:
                return _err("not enough price history", 404)
            closes = [float(x) for x in hist["Close"].tolist()]
            volumes = [float(x) for x in hist["Volume"].tolist()]
            analysis = VolumeAnalyzer.analyze(symbol, closes, volumes, had_news_last_3d=False)
            obv = VolumeAnalyzer.compute_obv(closes, volumes)
            tail = 60
            payload = {
                "ticker": symbol,
                "analysis": analysis,
                "series": {
                    "dates": [str(d)[:10] for d in hist.index.tolist()][-tail:],
                    "closes": [round(c, 2) for c in closes[-tail:]],
                    "volumes": [int(v) for v in volumes[-tail:]],
                    "obv": [round(v, 0) for v in obv[-tail:]],
                },
            }
            cache_set(f"vol:{ticker.upper()}", payload, ttl=180)
            return _ok(payload)
        except Exception as exc:
            logger.warning("stock volume failed ticker=%s: %s", ticker, exc)
            return _err("lookup failed")

    @app.route("/api/signals/unexplained-volume", methods=["GET"])
    def signals_unexplained_volume():
        """Signals where OBV analysis flagged volume surge without news.

        'Someone knows something' feed — retail-friendly alert list.
        """
        db = get_db()
        try:
            cursor = db.conn.cursor()
            cursor.execute(
                """SELECT event_id, ticker, company, headline, alpha_score, sentiment,
                          manipulation_score, volume_confirmation, obv_divergence_flag,
                          created_at
                     FROM signals
                    WHERE volume_confirmation = 1 OR obv_divergence_flag IS NOT NULL
                    ORDER BY created_at DESC LIMIT 50"""
            )
            rows = cursor.fetchall()
            out = []
            for r in rows:
                d = r if isinstance(r, dict) else dict(zip([c[0] for c in cursor.description], r))
                d["created_at"] = str(d.get("created_at")) if d.get("created_at") else None
                out.append(d)
            return _ok(out)
        except Exception as exc:
            logger.warning("unexplained volume failed: %s", exc)
            return _err("lookup failed")

    @app.route("/api/stock/<ticker>/promoter/insights", methods=["GET"])
    def stock_promoter_insights(ticker: str):
        from fundamentals.promoter_insights import compute

        try:
            data = compute(get_db(), ticker)
            return _ok(data)
        except Exception as exc:
            logger.warning("promoter insights failed ticker=%s: %s", ticker, exc)
            return _err("lookup failed")

    # ---- Abnormal-return (alpha) view ----
    @app.route("/api/analytics/alpha", methods=["GET"])
    def analytics_alpha():
        """Market-beta-adjusted accuracy.

        For each resolved prediction, compute Nifty return over the same window
        and report abnormal_return = actual − nifty. This separates real alpha
        from riding the market's tide.
        """
        cached = cache_get("analytics:alpha")
        if cached is not None:
            return _ok(cached)
        db = get_db()
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        cursor = db.conn.cursor()
        cursor.execute(
            """SELECT s.ticker, s.alpha_score, s.event_type, s.sentiment,
                       p.horizon, p.predicted_return_pct, p.actual_return_pct,
                       p.hit_target, p.created_at
                  FROM signals s
                  JOIN predictions p ON s.event_id = p.signal_id
                 WHERE p.actual_return_pct IS NOT NULL AND p.horizon = '3D'
                 ORDER BY p.created_at DESC LIMIT 500"""
        )
        rows_raw = cursor.fetchall()
        rows: List[Dict] = []
        for r in rows_raw:
            d = r if isinstance(r, dict) else dict(zip([c[0] for c in cursor.description], r))
            d["created_at"] = str(d.get("created_at")) if d.get("created_at") else None
            rows.append(d)
        if not rows:
            return _ok({"samples": 0, "enriched": 0,
                        "avg_abnormal_return": None, "alpha_hit_rate": None})

        from abnormal_returns import enrich_resolved

        agg = enrich_resolved(rows)
        # Aggregate by alpha bucket using abnormal-return metric
        buckets = {"<50": [], "50-65": [], "65-80": [], "80+": []}
        for r in rows:
            if "abnormal_return_pct" not in r:
                continue
            a = r.get("alpha_score") or 0
            bk = "80+" if a >= 80 else "65-80" if a >= 65 else "50-65" if a >= 50 else "<50"
            buckets[bk].append(r)
        bucket_alpha: List[Dict] = []
        for name, subset in buckets.items():
            if not subset:
                continue
            abn = [r["abnormal_return_pct"] for r in subset]
            alpha_hits = sum(
                1 for r in subset
                if (r.get("predicted_return_pct", 0) >= 0 and r["abnormal_return_pct"] > 0)
                or (r.get("predicted_return_pct", 0) < 0 and r["abnormal_return_pct"] < 0)
            )
            bucket_alpha.append({
                "bucket": name,
                "samples": len(subset),
                "alpha_hit_rate": round(alpha_hits / len(subset), 4),
                "avg_abnormal_return": round(sum(abn) / len(abn), 4),
            })
        payload = {
            "samples": len(rows),
            "enriched": agg.get("enriched", 0),
            "avg_abnormal_return": agg.get("avg_abnormal_return"),
            "alpha_hit_rate": agg.get("alpha_hit_rate"),
            "by_alpha_bucket": bucket_alpha,
        }
        cache_set("analytics:alpha", payload, ttl=600)
        return _ok(payload)

    # ---- Model calibration ----
    @app.route("/api/calibration", methods=["GET"])
    def calibration_current():
        """Current fitted parameters (magnitude, alpha buckets, event priors)."""
        from calibration import load

        db = get_db()
        out = {}
        for horizon in ("1D", "3D", "20D"):
            entry = {}
            for kind in ("magnitude", "alpha_buckets", "event_priors"):
                row = load(db, kind, horizon)
                if row:
                    entry[kind] = row
            if entry:
                out[horizon] = entry
        return _ok(out)

    @app.route("/api/admin/calibrate", methods=["POST"])
    @require_admin
    def admin_calibrate():
        from calibration import run_calibration
        from alpha_scoring_engine import set_magnitude_multipliers

        summary = run_calibration(get_db())
        # Install multipliers into running engine immediately
        mults = {h: s.get("magnitude_k") for h, s in summary.get("horizons", {}).items() if s.get("magnitude_k")}
        if mults:
            set_magnitude_multipliers(mults)
        return _ok(summary)

    @app.route("/api/admin/resolve_predictions", methods=["POST"])
    @require_admin
    def admin_resolve_predictions():
        """Force-run the batch resolver. Body: {"max_tickers": 50} (optional)."""
        from batch_resolver import resolve_all

        body = request.get_json(silent=True) or {}
        max_tickers = body.get("max_tickers")
        summary = resolve_all(get_db(), max_tickers=max_tickers)
        slim = {k: v for k, v in summary.items() if k != "per_ticker"}
        return _ok(slim)

    @app.route("/api/admin/backfill_forensics", methods=["POST"])
    @require_admin
    def admin_backfill_forensics():
        """Re-enrich existing signals with forensics (intent, manipulation_score, pump_score)."""
        from backfill_forensics import run

        body = request.get_json(silent=True) or {}
        since = body.get("since")
        limit = body.get("limit")
        only_missing = body.get("only_missing", True)
        result = run(since=since, limit=limit, only_missing=only_missing)
        return _ok(result)

    @app.route("/api/admin/seed_promoter", methods=["POST"])
    @require_admin
    def admin_seed_promoter():
        from fundamentals.promoter_seed import seed_all

        count = seed_all(get_db())
        return _ok({"seeded_rows": count})

    @app.route("/api/stock/<ticker>/promoter/events", methods=["GET"])
    def stock_promoter_events(ticker: str):
        db = get_db()
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        try:
            cursor = db.conn.cursor()
            cursor.execute(
                f"""SELECT event_date, event_type, person_name, quantity, pct_before, pct_after,
                            reason, link
                      FROM promoter_events
                     WHERE ticker = {p}
                     ORDER BY event_date DESC LIMIT 50""",
                (ticker.upper(),),
            )
            rows = cursor.fetchall()
            out: List[Dict] = []
            for r in rows:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    out.append({
                        "event_date": r[0], "event_type": r[1], "person_name": r[2],
                        "quantity": r[3], "pct_before": r[4], "pct_after": r[5],
                        "reason": r[6], "link": r[7],
                    })
            return _ok(out)
        except Exception as exc:
            logger.warning("promoter events endpoint failed: %s", exc)
            return _err("lookup failed")

    # ---- Forensics ----
    @app.route("/api/signal/<event_id>/forensics", methods=["GET"])
    def signal_forensics(event_id: str):
        db = get_db()
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        try:
            cursor = db.conn.cursor()
            cursor.execute(
                f"SELECT score, band, reasons FROM manipulation_scores WHERE event_id = {p}",
                (event_id,),
            )
            row = cursor.fetchone()
            score_row = None
            if row:
                if isinstance(row, dict):
                    score_row = {"score": row.get("score"), "band": row.get("band"),
                                 "reasons": json.loads(row.get("reasons") or "[]")}
                else:
                    score_row = {"score": row[0], "band": row[1],
                                 "reasons": json.loads(row[2] or "[]")}
            cursor.execute(
                f"""SELECT ad.field_name, ad.article_value, ad.filing_value, ad.diff_pct,
                            ad.tolerance_pct, ad.flagged, f.pdf_url, f.title
                      FROM article_discrepancies ad
                      LEFT JOIN filings f ON f.id = ad.filing_id
                     WHERE ad.event_id = {p}""",
                (event_id,),
            )
            discreps = []
            for r in cursor.fetchall():
                if isinstance(r, dict):
                    discreps.append(r)
                else:
                    discreps.append({
                        "field_name": r[0], "article_value": r[1], "filing_value": r[2],
                        "diff_pct": r[3], "tolerance_pct": r[4],
                        "flagged": bool(r[5]), "pdf_url": r[6], "filing_title": r[7],
                    })
            return _ok({"event_id": event_id, "score": score_row, "discrepancies": discreps})
        except Exception as exc:
            logger.warning("forensics endpoint failed: %s", exc)
            return _err("lookup failed")

    @app.route("/api/stock/<ticker>/forensics/history", methods=["GET"])
    def stock_forensics_history(ticker: str):
        db = get_db()
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        try:
            cursor = db.conn.cursor()
            cursor.execute(
                f"""SELECT s.event_id, s.headline, s.created_at, ms.score, ms.band
                      FROM signals s
                      LEFT JOIN manipulation_scores ms ON ms.event_id = s.event_id
                     WHERE s.ticker = {p}
                     ORDER BY s.created_at DESC LIMIT 50""",
                (ticker.upper(),),
            )
            rows = cursor.fetchall()
            out = []
            for r in rows:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    out.append({"event_id": r[0], "headline": r[1], "created_at": str(r[2]),
                                "score": r[3], "band": r[4]})
            return _ok(out)
        except Exception as exc:
            logger.warning("forensics history failed: %s", exc)
            return _err("lookup failed")

    # ---- Commodities impact ----
    @app.route("/api/commodities/impact", methods=["GET"])
    def commodities_impact():
        from commodities.impact import compose_impact

        cached = cache_get("commodities:impact")
        if cached is not None:
            return _ok(cached)
        try:
            payload = compose_impact(get_db())
            cache_set("commodities:impact", payload, ttl=300)
            return _ok(payload)
        except Exception as exc:
            logger.warning("commodities/impact failed: %s", exc)
            return _err("composition failed")

    # ---- Global overnight ----
    @app.route("/api/global/overnight", methods=["GET"])
    def global_overnight():
        from global_market.overnight_model import compose_overnight

        cached = cache_get("global:overnight")
        if cached is not None:
            return _ok(cached)
        try:
            payload = compose_overnight(get_db())
            cache_set("global:overnight", payload, ttl=300)
            return _ok(payload)
        except Exception as exc:
            logger.warning("global/overnight failed: %s", exc)
            return _err("composition failed")

    # ---- Metrics + observability ----
    @app.route("/api/metrics", methods=["GET"])
    def metrics_endpoint():
        from metrics import get_registry
        from ratelimit import get_bucket
        from net import get_breaker

        body = get_registry().render_prometheus()
        # Append rate limit and breaker state as custom gauges
        for src, data in get_bucket().status().items():
            body += f'scraper_tokens_available{{source="{src}"}} {data["tokens"]}\n'
        for src, data in get_breaker().status().items():
            body += f'scraper_circuit_open{{source="{src}"}} {1 if data["open"] else 0}\n'
        # Process-level gauges (CPU/RAM/threads). Best-effort — psutil
        # absence shouldn't blank the whole metrics page.
        try:
            import psutil  # type: ignore
            p = psutil.Process()
            with p.oneshot():
                body += f'process_cpu_percent {p.cpu_percent(interval=0.05)}\n'
                body += f'process_resident_memory_bytes {p.memory_info().rss}\n'
                body += f'process_threads {p.num_threads()}\n'
                body += f'process_uptime_seconds {int(time.time() - p.create_time())}\n'
                if hasattr(p, "num_fds"):
                    body += f'process_open_fds {p.num_fds()}\n'
            body += f'system_cpu_percent {psutil.cpu_percent(interval=None)}\n'
            vm = psutil.virtual_memory()
            body += f'system_memory_used_bytes {vm.used}\n'
            body += f'system_memory_total_bytes {vm.total}\n'
        except Exception:
            pass
        return Response(body, mimetype="text/plain; version=0.0.4")

    @app.route("/api/health/ext", methods=["GET"])
    def health_ext():
        from ratelimit import get_bucket
        from net import get_breaker

        proc: Dict = {}
        try:
            import psutil  # type: ignore
            p = psutil.Process()
            with p.oneshot():
                proc = {
                    "cpu_pct_proc": round(p.cpu_percent(interval=0.05), 2),
                    "cpu_pct_system": round(psutil.cpu_percent(interval=None), 2),
                    "rss_mb": round(p.memory_info().rss / (1024 * 1024), 1),
                    "open_fds": p.num_fds() if hasattr(p, "num_fds") else None,
                    "threads": p.num_threads(),
                    "uptime_secs": int(time.time() - p.create_time()),
                    "load_avg": list(getattr(psutil, 'getloadavg', lambda: [None, None, None])()),
                }
        except Exception as exc:
            proc = {"error": f"psutil unavailable: {exc}"}

        return _ok({
            "ratelimits": get_bucket().status(),
            "circuit_breakers": get_breaker().status(),
            "process": proc,
            "timestamp": datetime.utcnow().isoformat(),
        })

    # ---- Social sources ----
    @app.route("/api/social/handles", methods=["GET"])
    def social_handles():
        db = get_db()
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        platform = request.args.get("platform")
        try:
            cursor = db.conn.cursor()
            if platform:
                cursor.execute(
                    f"""SELECT platform, handle, status, hit_rate, posts_seen, last_scored_at
                          FROM social_sources WHERE platform = {p}
                          ORDER BY hit_rate DESC LIMIT 100""",
                    (platform,),
                )
            else:
                cursor.execute(
                    """SELECT platform, handle, status, hit_rate, posts_seen, last_scored_at
                         FROM social_sources ORDER BY hit_rate DESC LIMIT 100"""
                )
            rows = cursor.fetchall()
            out = []
            for r in rows:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    out.append({
                        "platform": r[0], "handle": r[1], "status": r[2],
                        "hit_rate": r[3], "posts_seen": r[4], "last_scored_at": str(r[5]) if r[5] else None,
                    })
            return _ok(out)
        except Exception as exc:
            logger.warning("social handles failed: %s", exc)
            return _err("lookup failed")

    # ---- News feed split: articles vs social buzz ----
    from news_type import classify as classify_news_type, label as news_type_label

    def _backfill_news_type(rows: List[Dict]) -> List[Dict]:
        """Ensure every row has a news_type field (fallback by source)."""
        for r in rows:
            if not r.get("news_type"):
                r["news_type"] = classify_news_type(r.get("source", ""))
            r["news_type_label"] = news_type_label(r["news_type"])
        return rows

    def _fetch_events(db, news_type: Optional[str], limit: int) -> List[Dict]:
        p = "%s" if getattr(db, "is_postgres", False) else "?"
        cursor = db.conn.cursor()
        try:
            if news_type:
                cursor.execute(
                    f"""SELECT event_id, title, summary, source, link, event_type, sentiment,
                               sentiment_confidence, magnitude, companies, published_at, created_at,
                               news_type
                          FROM events
                         WHERE news_type = {p}
                         ORDER BY created_at DESC LIMIT {p}""",
                    (news_type, limit),
                )
            else:
                cursor.execute(
                    f"""SELECT event_id, title, summary, source, link, event_type, sentiment,
                               sentiment_confidence, magnitude, companies, published_at, created_at,
                               news_type
                          FROM events
                         ORDER BY created_at DESC LIMIT {p}""",
                    (limit,),
                )
            rows = cursor.fetchall()
            out = []
            for r in rows:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    out.append({
                        "event_id": r[0], "title": r[1], "summary": r[2], "source": r[3],
                        "link": r[4], "event_type": r[5], "sentiment": r[6],
                        "sentiment_confidence": r[7], "magnitude": r[8], "companies": r[9],
                        "published_at": str(r[10]) if r[10] else None,
                        "created_at": str(r[11]) if r[11] else None,
                        "news_type": r[12],
                    })
            return _backfill_news_type(out)
        except Exception as exc:
            logger.warning("_fetch_events failed: %s", exc)
            return []

    @app.route("/api/news", methods=["GET"])
    def news_feed():
        """Unified news feed, with type split built in.

        Query params:
          type=articles|buzz|filings|all  (default: all)
          limit=N                          (default 50, max 200)
        Response:
          { articles: [...], buzz: [...], filings: [...], counts: {...} }
        """
        kind = (request.args.get("type") or "all").lower()
        try:
            limit = max(1, min(200, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50
        db = get_db()
        if kind == "articles":
            return _ok({"articles": _fetch_events(db, "news_article", limit)})
        if kind == "buzz":
            return _ok({"buzz": _fetch_events(db, "social_buzz", limit)})
        if kind == "filings":
            return _ok({"filings": _fetch_events(db, "filing", limit)})
        # all
        articles = _fetch_events(db, "news_article", limit)
        buzz = _fetch_events(db, "social_buzz", limit)
        filings = _fetch_events(db, "filing", limit)
        return _ok({
            "articles": articles,
            "buzz": buzz,
            "filings": filings,
            "counts": {
                "articles": len(articles),
                "buzz": len(buzz),
                "filings": len(filings),
            },
        })

    # ---- Event first-seen (priority boost marker) ----
    @app.route("/api/events/priority", methods=["GET"])
    def events_priority():
        db = get_db()
        try:
            cursor = db.conn.cursor()
            cursor.execute(
                """SELECT cluster_hash, canonical_headline, ticker, first_seen_at,
                          first_seen_source, sources, member_count
                     FROM event_clusters
                     WHERE member_count >= 2 AND first_seen_source NOT LIKE 'et_%%'
                       AND first_seen_source NOT LIKE 'mint_%%'
                       AND first_seen_source NOT LIKE 'moneycontrol_%%'
                     ORDER BY first_seen_at DESC LIMIT 50"""
            )
            rows = cursor.fetchall()
            out = []
            for r in rows:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    out.append({
                        "cluster_hash": r[0], "headline": r[1], "ticker": r[2],
                        "first_seen_at": str(r[3]), "first_seen_source": r[4],
                        "sources": r[5], "member_count": r[6],
                    })
            return _ok(out)
        except Exception as exc:
            logger.warning("events priority failed: %s", exc)
            return _err("lookup failed")

    # ---- Schema init (operational) ----
    @app.route("/api/admin/init_ext_schema", methods=["POST"])
    @require_admin
    def init_ext_schema():
        from schema_ext import apply

        apply(get_db().conn)
        return _ok({"applied": True})

    logger.info("api_ext: routes registered")


# ============================================================
# Scheduled jobs
# ============================================================

def register_jobs(scheduler, get_db: Callable) -> None:
    """Register background jobs with the existing APScheduler."""

    def _promoter_quarterly():
        try:
            from fundamentals.promoter_scraper import PromoterScraper
            from config import MONITORED_STOCKS

            scraper = PromoterScraper(get_db(), MONITORED_STOCKS)
            result = scraper.refresh_all()
            logger.info("promoter quarterly refresh: %s", result)
        except Exception as exc:
            logger.error("promoter quarterly job failed: %s", exc)

    def _promoter_events_daily():
        try:
            from fundamentals.promoter_events import PromoterEventsFetcher
            from config import MONITORED_STOCKS

            fetcher = PromoterEventsFetcher(get_db(), MONITORED_STOCKS)
            result = fetcher.refresh_all()
            logger.info("promoter events daily refresh: %s", result)
        except Exception as exc:
            logger.error("promoter events daily job failed: %s", exc)

    def _forensic_daily():
        try:
            from forensics.bse_filing_fetcher import BSEFilingFetcher
            from forensics.nse_filing_fetcher import NSEFilingFetcher
            from forensics.ir_pdf_fetcher import IRPageFetcher
            from forensics.sebi_disclosures import SEBIDisclosuresFetcher
            from forensics.llm_intent import SourceCredibility
            from config import MONITORED_STOCKS

            db = get_db()
            bse_result = BSEFilingFetcher(db, MONITORED_STOCKS).refresh_all()
            nse_result = NSEFilingFetcher(db, MONITORED_STOCKS).refresh_all()
            IRPageFetcher(db, MONITORED_STOCKS).refresh_all()
            SEBIDisclosuresFetcher(db, MONITORED_STOCKS).refresh_all()
            SourceCredibility(db).recompute()
            bse_n = sum(bse_result.values()) if isinstance(bse_result, dict) else 0
            nse_n = sum(nse_result.values()) if isinstance(nse_result, dict) else 0
            logger.info("forensic daily cycle complete: bse_filings=%d nse_filings=%d", bse_n, nse_n)
        except Exception as exc:
            logger.error("forensic daily job failed: %s", exc)

    def _overnight_snapshot():
        try:
            from global_market.overnight_model import compose_overnight

            compose_overnight(get_db())
        except Exception as exc:
            logger.error("overnight snapshot job failed: %s", exc)

    def _overnight_refit():
        try:
            from global_market.overnight_model import refit_weights

            fitted = refit_weights()
            logger.info("overnight weights refit: %s", fitted)
        except Exception as exc:
            logger.error("overnight refit failed: %s", exc)

    def _handle_scorer_hourly():
        try:
            from social.handle_scorer import HandleScorer

            result = HandleScorer(get_db()).recompute_scores()
            logger.info("handle scorer: %s", result)
        except Exception as exc:
            logger.error("handle scorer job failed: %s", exc)

    def _dq_check_nightly():
        try:
            db = get_db()
            cursor = db.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM signals WHERE status = 'active'")
            r = cursor.fetchone()
            active = r[0] if not isinstance(r, dict) else r.get("count") or list(r.values())[0]
            if not active:
                logger.warning("DQ: no active signals")
        except Exception as exc:
            logger.error("DQ check failed: %s", exc)

    scheduler.add_job(_promoter_quarterly, "cron", day="20", hour="3", minute="0", id="promoter_quarterly", replace_existing=True)
    scheduler.add_job(_promoter_events_daily, "cron", hour="4", minute="0", id="promoter_events_daily", replace_existing=True)
    scheduler.add_job(_forensic_daily, "cron", hour="5", minute="0", id="forensic_daily", replace_existing=True)
    scheduler.add_job(_overnight_snapshot, "cron", hour="2", minute="30", id="overnight_snapshot", replace_existing=True)
    scheduler.add_job(_overnight_refit, "cron", day_of_week="sun", hour="6", minute="0", id="overnight_refit", replace_existing=True)
    scheduler.add_job(_handle_scorer_hourly, "interval", hours=1, id="handle_scorer_hourly", replace_existing=True)

    # Batch-resolve past-horizon predictions hourly (cheaper than full daily run)
    def _resolve_hourly():
        try:
            from batch_resolver import resolve_all
            res = resolve_all(get_db(), max_tickers=20, per_ticker_sleep=1.0)
            logger.info("hourly resolve: %s", {k: v for k, v in res.items() if k != "per_ticker"})
        except Exception as exc:
            logger.error("hourly resolve failed: %s", exc)

    scheduler.add_job(_resolve_hourly, "interval", hours=1, id="batch_resolve_hourly",
                      replace_existing=True)

    # Weekly calibration: Sunday 06:30 UTC (after overnight refit)
    def _calibrate_weekly():
        try:
            from calibration import run_calibration
            from alpha_scoring_engine import set_magnitude_multipliers

            summary = run_calibration(get_db())
            mults = {h: s.get("magnitude_k") for h, s in summary.get("horizons", {}).items() if s.get("magnitude_k")}
            if mults:
                set_magnitude_multipliers(mults)
            logger.info("weekly calibration: %s", summary)
        except Exception as exc:
            logger.error("weekly calibration failed: %s", exc)

    scheduler.add_job(_calibrate_weekly, "cron", day_of_week="sun", hour="6", minute="30",
                      id="calibration_weekly", replace_existing=True)
    scheduler.add_job(_dq_check_nightly, "cron", hour="23", minute="45", id="dq_nightly", replace_existing=True)
    logger.info("api_ext: jobs registered")


def init_extension(app: Flask, get_db: Callable) -> None:
    """One-shot entry point: apply extension schema, register routes, attach log of startup."""
    try:
        from schema_ext import apply

        apply(get_db().conn)
        logger.info("api_ext: schema extensions applied")
    except Exception as exc:
        logger.warning("api_ext: schema apply skipped: %s", exc)
    register_routes(app, get_db)
