"""
Phase 1.5 orchestrator — composes the new collectors/enrichments.

Designed to be called from inside hybrid_scraper.DataPipeline.run() (or
standalone). The orchestrator is idempotent — every call:
  1. Runs social collectors → returns canonical event dicts.
  2. Records each ticker+headline cluster in event_clusters via the
     PublishPriorityTracker so we can detect social-before-mainstream edges.
  3. For each signal in the pipeline output, runs LLM intent classification,
     number-matcher, and writes a manipulation_score.

All steps fail-soft — one source breaking doesn't stop the others.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def collect_social(db, known_tickers: Iterable[str],
                    per_collector_budget_secs: int = 20) -> List[Dict]:
    """Run all social collectors with strict per-collector time budgets.

    Each platform has two paths: credentialed (best) and no-auth fallback.
    Whichever is used, we cap wall-clock time so a single slow source
    (e.g. Nitter instance timeout storm) can't block the pipeline.
    """
    import os as _os
    import time as _time
    posts: List[Dict] = []

    def _run_with_budget(label, fn):
        t0 = _time.time()
        try:
            result = fn() or []
            elapsed = _time.time() - t0
            if elapsed > per_collector_budget_secs:
                logger.warning("%s took %.1fs (over %ds budget)", label, elapsed, per_collector_budget_secs)
            return result
        except Exception as exc:
            logger.warning("%s failed: %s", label, exc)
            return []

    # --- Reddit ---
    def _reddit():
        if _os.getenv("REDDIT_CLIENT_ID") and _os.getenv("REDDIT_CLIENT_SECRET"):
            from social.reddit_collector import RedditCollector
            return RedditCollector(known_tickers).collect()
        from social.reddit_json import RedditJsonCollector
        return RedditJsonCollector(known_tickers).collect(limit_per_sub=15)

    posts.extend(_run_with_budget("reddit", _reddit))

    # --- Telegram web (fastest breaking news path — prioritise) ---
    def _telegram():
        if _os.getenv("TELEGRAM_API_ID") and _os.getenv("TELEGRAM_API_HASH"):
            from social.telegram_collector import TelegramCollector
            return TelegramCollector(known_tickers).collect()
        from social.telegram_web import TelegramWebCollector
        return TelegramWebCollector(known_tickers).collect()

    posts.extend(_run_with_budget("telegram", _telegram))

    # --- Twitter / Nitter (most flaky — last + shortest leash) ---
    def _twitter():
        from social.twitter_collector import TwitterCollector
        return TwitterCollector(known_tickers).collect()

    posts.extend(_run_with_budget("twitter", _twitter))

    # Record first-seen per cluster
    try:
        from social.publish_priority import PublishPriorityTracker

        tracker = PublishPriorityTracker(db)
        for p in posts:
            try:
                meta = tracker.record(
                    p["cluster_hash"],
                    p.get("title") or p.get("text", "")[:280],
                    p.get("primary_ticker"),
                    p["source"],
                    datetime.fromisoformat(p["published_at"].replace("Z", "+00:00"))
                    if isinstance(p.get("published_at"), str) else None,
                )
                p.update(meta)
            except Exception as exc:
                logger.debug("publish priority record failed: %s", exc)
    except Exception as exc:
        logger.warning("publish priority tracker failed: %s", exc)

    # Handle-level observation counts for scorer
    try:
        from social.handle_scorer import HandleScorer

        scorer = HandleScorer(db)
        for p in posts:
            scorer.record_observation(p["platform"], p.get("handle", "unknown"))
    except Exception as exc:
        logger.debug("handle scorer record failed: %s", exc)

    return posts


def enrich_signal_forensics_v2(db, signal: Dict, article_text: str = "") -> Dict:
    """Full forensics layer v2 — fine-print + pump-dump + article-forensics
    + existing base (discrepancy + intent + credibility) fused into one score.
    """
    event_id = signal.get("event_id") or signal.get("id")
    ticker = signal.get("ticker")
    if not event_id or not ticker:
        return signal

    # --- Base (existing) ---
    # Always compute intent — LLM if available, heuristic otherwise.
    # Ensures every signal has an intent field so the UI is never blank.
    intent_result: Optional[Dict] = None
    try:
        from forensics.llm_intent import classify_intent_with_fallback

        text_for_intent = f"{signal.get('headline', '') or ''}\n\n{article_text or signal.get('summary', '') or ''}"
        intent_result = classify_intent_with_fallback(text_for_intent, event=signal)
        if intent_result:
            signal["intent"] = intent_result.get("intent")
            signal["fingerprint_flags"] = intent_result.get("fingerprint_flags", [])
    except Exception as exc:
        logger.debug("intent classify failed event=%s: %s", event_id, exc)

    coordinated = False
    try:
        from forensics.llm_intent import detect_coordinated_campaign

        coordinated = detect_coordinated_campaign(db, signal.get("headline", ""), ticker)
        signal["coordinated_campaign"] = coordinated
    except Exception:
        pass

    credibility = 0.5
    try:
        from forensics.llm_intent import SourceCredibility

        credibility = SourceCredibility(db).get(signal.get("source", ""))
    except Exception:
        pass

    discrepancy: Optional[Dict] = None
    try:
        from forensics.number_matcher import NumberMatcher

        discrepancy = NumberMatcher(db).check_article(
            event_id=event_id,
            ticker=ticker,
            article_text=article_text or signal.get("headline", ""),
            event_dt=datetime.now(timezone.utc),
        )
        signal["article_discrepancy_flag"] = bool(discrepancy and discrepancy.get("flagged"))
    except Exception:
        pass

    promoter_sell = False
    try:
        from forensics.forensic_scorer import promoter_selling_recent

        promoter_sell = promoter_selling_recent(db, ticker)
    except Exception:
        pass

    # Base score
    base_result: Dict[str, Any] = {}
    try:
        from forensics.forensic_scorer import score_event

        base_result = score_event(
            event_id=event_id,
            discrepancy=discrepancy,
            intent=intent_result,
            source_credibility=credibility,
            coordinated_campaign=coordinated,
            unexplained_volume_pre_news=bool(signal.get("unexplained_volume")),
            promoter_selling_recent=promoter_sell,
        )
    except Exception as exc:
        logger.debug("base score failed: %s", exc)
        base_result = {"score": 0, "band": "clean", "reasons": []}

    # --- Fine print ---
    fp_result = None
    try:
        from forensics.fine_print import analyze_and_summarise

        text_for_fp = " ".join([signal.get("headline", ""), signal.get("summary", ""), article_text]).strip()
        if text_for_fp:
            fp_result = analyze_and_summarise(text_for_fp)
    except Exception as exc:
        logger.debug("fine_print failed: %s", exc)

    # --- Pump-dump composite ---
    pd_result = None
    try:
        from forensics.pump_dump import compute

        pd_result = compute(db, ticker)
    except Exception as exc:
        logger.debug("pump_dump failed: %s", exc)

    # --- Article forensics (rule-based always, LLM only if flagged) ---
    af_result = None
    try:
        from forensics.article_forensics import analyze

        text_for_af = " ".join([signal.get("headline", ""), signal.get("summary", ""), article_text]).strip()
        if text_for_af:
            af_result = analyze(text_for_af, use_llm=False)  # LLM off in orchestrator (cost)
    except Exception as exc:
        logger.debug("article_forensics failed: %s", exc)

    # --- Fuse ---
    try:
        from forensics.forensic_scorer import fuse, persist

        fused = fuse(
            event_id=event_id,
            ticker=ticker,
            base_score_result=base_result,
            pump_dump_result=pd_result,
            fine_print_result=fp_result,
            article_forensics_result=af_result,
        )
        persist(db, fused)
        signal["manipulation_score"] = fused["score"]
        signal["manipulation_band"] = fused["band"]
        signal["manipulation_reasons"] = fused["reasons"]
        signal["manipulation_sub_scores"] = fused.get("sub_scores", {})
        signal["pump_score"] = (pd_result or {}).get("pump_score")
        signal["fine_print_score"] = (fp_result or {}).get("score")
        signal["article_rule_score"] = (af_result or {}).get("rule_score")
    except Exception as exc:
        logger.debug("forensic fuse failed: %s", exc)

    return signal


def enrich_signal_forensics(db, signal: Dict, article_text: str = "") -> Dict:
    """Run the full intent + discrepancy + forensic scoring on one signal.

    Mutates and returns `signal` with new fields:
      intent, fingerprint_flags, volume_multiplier (if volume analysis available),
      article_discrepancy_flag, manipulation_score.
    """
    event_id = signal.get("event_id") or signal.get("id")
    ticker = signal.get("ticker")
    if not event_id or not ticker:
        return signal

    # Intent classification
    intent_result: Optional[Dict] = None
    try:
        from forensics.llm_intent import classify_intent, should_classify

        if should_classify(signal):
            intent_result = classify_intent(
                f"{signal.get('headline', '')}\n\n{article_text or signal.get('summary', '')}"
            )
            if intent_result:
                signal["intent"] = intent_result["intent"]
                signal["fingerprint_flags"] = intent_result.get("fingerprint_flags", [])
    except Exception as exc:
        logger.debug("intent classify failed event=%s: %s", event_id, exc)

    # Coordinated-campaign
    coordinated = False
    try:
        from forensics.llm_intent import detect_coordinated_campaign

        coordinated = detect_coordinated_campaign(db, signal.get("headline", ""), ticker)
        signal["coordinated_campaign"] = coordinated
    except Exception as exc:
        logger.debug("coord detect failed: %s", exc)

    # Source credibility
    credibility = 0.5
    try:
        from forensics.llm_intent import SourceCredibility

        credibility = SourceCredibility(db).get(signal.get("source", ""))
    except Exception:
        pass

    # Number matcher
    discrepancy: Optional[Dict] = None
    try:
        from forensics.number_matcher import NumberMatcher

        discrepancy = NumberMatcher(db).check_article(
            event_id=event_id,
            ticker=ticker,
            article_text=article_text or signal.get("headline", ""),
            event_dt=datetime.now(timezone.utc),
        )
        signal["article_discrepancy_flag"] = bool(discrepancy and discrepancy.get("flagged"))
    except Exception as exc:
        logger.debug("number match failed: %s", exc)

    # Pre-news volume flag (from quant_layer)
    unexplained_volume = bool(signal.get("unexplained_volume"))

    # Promoter selling flag
    promoter_sell = False
    try:
        from forensics.forensic_scorer import promoter_selling_recent

        promoter_sell = promoter_selling_recent(db, ticker)
    except Exception:
        pass

    # Composite score
    try:
        from forensics.forensic_scorer import score_event, persist

        result = score_event(
            event_id=event_id,
            discrepancy=discrepancy,
            intent=intent_result,
            source_credibility=credibility,
            coordinated_campaign=coordinated,
            unexplained_volume_pre_news=unexplained_volume,
            promoter_selling_recent=promoter_sell,
        )
        persist(db, result)
        signal["manipulation_score"] = result["score"]
        signal["manipulation_band"] = result["band"]
        signal["manipulation_reasons"] = result["reasons"]
    except Exception as exc:
        logger.debug("forensic score failed: %s", exc)

    return signal


def enrich_signal_volume(db, signal: Dict) -> Dict:
    """Attach OBV/volume-surge fields to a signal if OHLCV is available."""
    try:
        import yfinance as yf  # type: ignore

        from quant_layer import VolumeAnalyzer
    except Exception:
        return signal

    ticker = signal.get("ticker")
    if not ticker:
        return signal
    try:
        symbol = f"{ticker}.NS" if "." not in ticker else ticker
        hist = yf.Ticker(symbol).history(period="90d", interval="1d")
        if hist is None or len(hist) < 5:
            return signal
        closes = list(hist["Close"].astype(float))
        volumes = list(hist["Volume"].astype(float))
        analysis = VolumeAnalyzer.analyze(ticker, closes, volumes,
                                            had_news_last_3d=bool(signal.get("headline")))
        if not analysis.get("has_data"):
            return signal
        signal["obv_divergence_flag"] = analysis.get("obv_divergence_flag")
        signal["obv_divergence_strength"] = analysis.get("obv_divergence_strength", "none")
        signal["volume_surge"] = analysis.get("volume_surge", False)
        signal["strong_volume_surge"] = analysis.get("strong_volume_surge", False)
        signal["unexplained_volume"] = analysis.get("unexplained_volume", False)
        signal["vol_surge_ratio"] = analysis.get("vol_surge_ratio", 0.0)
        signal["obv_z_20d"] = analysis.get("obv_z_20d", 0.0)
        mul = VolumeAnalyzer.volume_confirmation_multiplier(analysis, signal.get("sentiment", ""))
        signal["volume_multiplier"] = mul
        signal["volume_confirmation"] = mul > 1.0
        if "alpha_score" in signal:
            signal["alpha_score"] = round(float(signal["alpha_score"]) * mul, 2)
    except Exception as exc:
        logger.debug("volume enrich failed ticker=%s: %s", ticker, exc)
    return signal


def persist_social_as_events(db, posts: List[Dict]) -> int:
    """Write social posts into the events table with news_type='social_buzz'.

    Social posts already have the canonical event shape from build_post(). We
    use cluster_hash as event_id so cross-source duplicates collapse.
    """
    if not posts:
        return 0
    written = 0
    for p in posts:
        try:
            event_id = p.get("cluster_hash") or f"social_{hash((p.get('source'), p.get('url')))}"
            success = db.insert_event(
                event_id=event_id,
                title=p.get("title") or (p.get("text") or "")[:280],
                summary=(p.get("text") or "")[:1000],
                source=p.get("source"),
                link=p.get("url"),
                event_type="social",
                event_confidence=0.5,
                sentiment="neutral",
                sentiment_confidence=0.3,
                magnitude=float(p.get("score", 1.0)),
                impact_score=0,
                companies=",".join(p.get("tickers", [])),
                published_at=p.get("published_at"),
            )
            if success:
                # tag as social_buzz
                try:
                    db.update_event_news_type(event_id, "social_buzz")
                except Exception:
                    pass
                written += 1
        except Exception as exc:
            logger.debug("persist social event failed: %s", exc)
    return written


# Every extended signal field that the orchestrator may populate
_SIGNAL_EXT_FIELDS = (
    "intent", "fingerprint_flags", "volume_confirmation", "volume_multiplier",
    "obv_divergence_flag", "article_discrepancy_flag", "manipulation_score",
    "first_seen_source", "priority_boost", "edge_minutes", "coordinated_campaign",
    "news_type", "reasoning",
)


def persist_signal_extensions(db, signal: Dict) -> bool:
    """Pick up enriched fields from an in-memory signal dict and persist them."""
    event_id = signal.get("event_id") or signal.get("id")
    if not event_id:
        return False
    fields = {k: signal[k] for k in _SIGNAL_EXT_FIELDS if k in signal and signal[k] is not None}
    # Derive news_type if missing
    if "news_type" not in fields:
        try:
            from news_type import classify as _classify

            fields["news_type"] = _classify(signal.get("source", ""))
        except Exception:
            pass
    return db.update_signal_extensions(event_id, fields)


def run_phase_1_5(db, signals: List[Dict], known_tickers: Iterable[str]) -> Dict[str, Any]:
    """Run the whole Phase 1.5 cycle. Returns a summary for logging.

    Also persists social posts as events AND writes enriched signal fields back
    to the DB so the UI can render them.
    """
    social_posts = collect_social(db, known_tickers)
    social_written = persist_social_as_events(db, social_posts)

    # Only enrich signals that haven't been processed yet, and cap by time.
    # This was the hang: previous code enriched ALL signals (thousands) per cycle,
    # each touching yfinance + Groq — wall-clock hours. Bounds are env-tunable
    # so we can dial throughput up when there's backlog and down under load.
    import os as _os
    import time as _time
    TIME_BUDGET_SECS = int(_os.getenv('ENRICH_TIME_BUDGET_SECS', '180'))
    MAX_SIGNALS_PER_CYCLE = int(_os.getenv('ENRICH_MAX_PER_CYCLE', '100'))
    start = _time.time()

    # Filter to signals that need enrichment (missing manipulation_score)
    to_enrich = []
    p = "%s" if getattr(db, "is_postgres", False) else "?"
    try:
        cursor = db.conn.cursor()
        event_ids = [s.get("event_id") for s in signals if s.get("event_id")]
        if event_ids:
            qmarks = ",".join([p] * len(event_ids))
            cursor.execute(
                f"SELECT event_id FROM signals WHERE event_id IN ({qmarks}) AND manipulation_score IS NOT NULL",
                event_ids,
            )
            already = {row[0] if not isinstance(row, dict) else row.get("event_id") for row in cursor.fetchall()}
        else:
            already = set()
    except Exception:
        already = set()

    for sig in signals:
        if sig.get("event_id") in already:
            continue
        to_enrich.append(sig)
        if len(to_enrich) >= MAX_SIGNALS_PER_CYCLE:
            break

    enriched = 0
    persisted = 0
    rescored = 0
    skipped = len(signals) - len(to_enrich)
    for sig in to_enrich:
        if _time.time() - start > TIME_BUDGET_SECS:
            logger.warning("phase 1.5 time budget hit at signal %d/%d — deferring rest", enriched, len(to_enrich))
            break
        try:
            enrich_signal_volume(db, sig)
            enrich_signal_forensics_v2(db, sig)
            try:
                from rescoring import rescore_signal

                rescore_signal(db, sig, horizon="3D")
                rescored += 1
            except Exception as _rs:
                logger.debug("rescore failed: %s", _rs)

            # Reasoning: structured why-this-pick chain, written last so it
            # can see all the prior enrichment fields.
            try:
                from reasoning_engine import explain
                import json as _json

                reasoning = explain(db, sig)
                sig["reasoning"] = _json.dumps(reasoning, default=str)
            except Exception as _re:
                logger.debug("reasoning failed: %s", _re)

            enriched += 1
            if persist_signal_extensions(db, sig):
                persisted += 1
        except Exception as exc:
            logger.debug("enrich loop failed: %s", exc)

    return {
        "social_posts": len(social_posts),
        "social_events_written": social_written,
        "candidates": len(to_enrich),
        "skipped_already_enriched": skipped,
        "enriched_signals": enriched,
        "persisted_enrichments": persisted,
        "rescored_signals": rescored,
        "elapsed_secs": round(_time.time() - start, 2),
    }
