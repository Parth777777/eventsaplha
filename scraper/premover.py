"""Pre-mover scoring engine.

The high-alpha feed surfaces stocks AFTER the news has hit — by definition
those names have already moved.  This module ranks tickers by a composite
*PreMoverScore* (0-100) built from leading indicators that fire BEFORE a
visible price move:

    accumulation       bulk-deal BUY net flow  (last 5d, log-scaled)
    insider            promoter SAST/PIT acquire or pledge ↓
    oi_buildup         F&O OTM-call OI increase (last 24h)
    vol_divergence     OBV up while close-on-close return ≈ 0
    catalyst           Tier-1 policy event mapped to the ticker's sector
    valuation          PE/PB below sector median
    low_attention      few signals in the last 30d (under-covered name)
    sector_tailwind    sector hot, ticker hasn't kept up

Hard filters reject names that have already surged, are forensically dirty,
sit below ₹500 Cr market cap, or rely on rumour-grade sources only.

This module deliberately holds NO Flask / HTTP dependencies — it is a pure
function so the API endpoint *and* a future scheduler precompute job can both
call it.  All cursor work goes through small, defensive try/except blocks: if
a table doesn't exist on a fresh DB, the corresponding factor score is 0
rather than raising.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Weight tables — same factor stack, reweighted per horizon
# ----------------------------------------------------------------------------

WEIGHTS: Dict[str, Dict[str, float]] = {
    "1D": {
        "accumulation":   0.18,
        "insider":        0.08,
        "oi_buildup":     0.22,
        "vol_divergence": 0.18,
        "catalyst":       0.07,
        "news_catalyst":  0.13,
        "valuation":      0.04,
        "low_attention":  0.04,
        "sector_tailwind":0.06,
    },
    "5D": {
        "accumulation":   0.18,
        "insider":        0.15,
        "oi_buildup":     0.12,
        "vol_divergence": 0.10,
        "catalyst":       0.10,
        "news_catalyst":  0.15,
        "valuation":      0.10,
        "low_attention":  0.05,
        "sector_tailwind":0.05,
    },
    "20D": {
        "accumulation":   0.12,
        "insider":        0.16,
        "oi_buildup":     0.04,
        "vol_divergence": 0.04,
        "catalyst":       0.15,
        "news_catalyst":  0.15,
        "valuation":      0.18,
        "low_attention":  0.10,
        "sector_tailwind":0.06,
    },
}

FACTOR_KEYS = list(WEIGHTS["5D"].keys())

# Map promoter_events.event_type → directional weight (positive = bullish)
_INSIDER_WEIGHTS = {
    "sast_acquire": +1.0,
    "pit_buy":      +0.9,
    "release":      +0.6,        # pledge release — de-leverage, net positive
    "sast_dispose": -1.0,
    "pit_sell":     -0.9,
    "pledge":       -0.7,        # new pledge — distress signal
}


# ----------------------------------------------------------------------------
# Small DB helpers — defensive, return [] / None on failure
# ----------------------------------------------------------------------------

def _is_postgres() -> bool:
    return os.getenv("DATABASE_URL", "").startswith("postgres")


def _ph() -> str:
    return "%s" if _is_postgres() else "?"


def _safe_query(db, sql: str, params: Tuple = (), label: str = "premover") -> List[Dict]:
    try:
        cur = db.conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.debug(f"{label}: {e}")
        return []


# ----------------------------------------------------------------------------
# Per-factor primitives — each returns {ticker -> (raw_score, evidence_str)}
# raw_score in [0, 1]; evidence is human-readable for the UI.
# ----------------------------------------------------------------------------

def _accumulation_factor(db) -> Dict[str, Tuple[float, str]]:
    """Net BUY value over the last 5 trading days, log-scaled into [0,1]."""
    rows = _safe_query(db, """
        SELECT ticker,
               SUM(CASE WHEN UPPER(side)='BUY'  THEN value_cr ELSE 0 END) AS buy_cr,
               SUM(CASE WHEN UPPER(side)='SELL' THEN value_cr ELSE 0 END) AS sell_cr,
               COUNT(*) AS n_deals
        FROM bulk_deals
        WHERE deal_date >= date('now', '-5 days')
        GROUP BY ticker
    """, label="acc")
    out: Dict[str, Tuple[float, str]] = {}
    for r in rows:
        net = float(r.get("buy_cr") or 0) - float(r.get("sell_cr") or 0)
        if net <= 0:
            continue
        # Log-scale: ₹5cr → 0.42, ₹50cr → 0.85, ₹500cr → ~1.0 (clamped)
        score = min(1.0, math.log1p(net) / math.log1p(500.0))
        out[r["ticker"]] = (
            score,
            f"₹{net:.1f}Cr net BUY across {int(r['n_deals'])} bulk deal(s) (last 5d)",
        )
    return out


def _insider_factor(db) -> Dict[str, Tuple[float, str]]:
    """Promoter / insider events in the last 30d, weighted by direction."""
    rows = _safe_query(db, """
        SELECT ticker, event_type, pct_before, pct_after, event_date
        FROM promoter_events
        WHERE event_date >= date('now', '-30 days')
    """, label="insider")
    accum: Dict[str, Dict] = {}
    for r in rows:
        et = (r.get("event_type") or "").lower()
        w = _INSIDER_WEIGHTS.get(et, 0.0)
        if w == 0:
            continue
        delta_pp = 0.0
        try:
            if r.get("pct_after") is not None and r.get("pct_before") is not None:
                delta_pp = float(r["pct_after"]) - float(r["pct_before"])
        except Exception:
            pass
        signed = w * (abs(delta_pp) if delta_pp else 0.5)
        rec = accum.setdefault(r["ticker"], {"sum": 0.0, "best": (None, 0.0, None)})
        rec["sum"] += signed
        if abs(signed) > abs(rec["best"][1]):
            rec["best"] = (et, signed, r.get("event_date"))
    out: Dict[str, Tuple[float, str]] = {}
    for ticker, rec in accum.items():
        if rec["sum"] <= 0:
            continue
        # Saturate around +3pp acquire over 30d → 1.0
        score = min(1.0, rec["sum"] / 3.0)
        et, signed, ed = rec["best"]
        sign = "+" if signed > 0 else ""
        out[ticker] = (
            score,
            f"Promoter {et} ({sign}{signed:.1f}pp signed) on {ed}",
        )
    return out


def _oi_buildup_factor(db) -> Dict[str, Tuple[float, str]]:
    """Aggregate fo_unusual signal_score over last 24h, scaled into [0,1]."""
    rows = _safe_query(db, """
        SELECT ticker,
               SUM(CASE WHEN kind='call' THEN signal_score ELSE 0 END) AS call_score,
               SUM(CASE WHEN kind='put'  THEN signal_score ELSE 0 END) AS put_score,
               COUNT(*) AS n_strikes
        FROM fo_unusual
        WHERE fetched_at >= datetime('now', '-24 hours')
        GROUP BY ticker
    """, label="oi")
    out: Dict[str, Tuple[float, str]] = {}
    for r in rows:
        cs = float(r.get("call_score") or 0)
        ps = float(r.get("put_score") or 0)
        net = cs - ps                        # bullish if calls dominate
        if net <= 0:
            continue
        score = min(1.0, net / 6.0)          # signal_score ~2 per strong strike
        out[r["ticker"]] = (
            score,
            f"OTM-call OI build · score {cs:.1f} vs put {ps:.1f} across {int(r['n_strikes'])} strike(s)",
        )
    return out


def _vol_divergence_factor(db) -> Dict[str, Tuple[float, str]]:
    """Volume anomaly (z-score >= 2) without an alpha-grade signal in 48h.
    Approximates the OBV-up-while-flat-price idea using already-stored anomaly
    z-scores; full OBV requires intraday OHLCV which the DB doesn't carry."""
    rows = _safe_query(db, """
        SELECT v.ticker, MAX(v.z_score) AS z, MAX(v.created_at) AS last_seen
        FROM volume_anomalies v
        WHERE v.created_at >= datetime('now', '-3 days')
          AND v.z_score >= 2.0
          AND NOT EXISTS (
            SELECT 1 FROM signals s
            WHERE s.ticker = v.ticker
              AND s.alpha_score >= 60
              AND s.created_at >= datetime('now', '-2 days')
          )
        GROUP BY v.ticker
    """, label="vol_div")
    out: Dict[str, Tuple[float, str]] = {}
    for r in rows:
        z = float(r.get("z") or 0)
        score = min(1.0, (z - 1.5) / 3.0) if z > 1.5 else 0.0
        if score <= 0:
            continue
        out[r["ticker"]] = (
            score,
            f"Volume z={z:.1f} with no high-α signal in 48h",
        )
    return out


def _catalyst_factor(db, ticker_to_sector: Dict[str, str]) -> Dict[str, Tuple[float, str]]:
    """Recent Tier-1 policy event in the last 7d whose sector matches the ticker.
    Mirrors the source patterns used in /api/policy/fast."""
    policy_sources = (
        "RBI", "SEBI", "MoF", "GST", "CBDT", "PIB", "NSE", "BSE",
        "Reserve Bank", "Securities Exchange Board",
    )
    where = " OR ".join(f"source LIKE '{p}%'" for p in policy_sources)
    rows = _safe_query(db, f"""
        SELECT ticker, alpha_score, headline, source, created_at
        FROM signals
        WHERE ({where})
          AND created_at >= datetime('now', '-7 days')
        ORDER BY alpha_score DESC
        LIMIT 200
    """, label="catalyst_direct")

    # Direct ticker hits → strong signal
    out: Dict[str, Tuple[float, str]] = {}
    for r in rows:
        t = r.get("ticker")
        if not t or t in out:
            continue
        a = float(r.get("alpha_score") or 0)
        # Saturate at α=60 (was 80) — a 60-alpha policy hit IS already a
        # full-strength catalyst. Anything beyond is gravy.
        score = min(1.0, a / 60.0)
        src = (r.get("source") or "").split(":")[0][:20]
        out[t] = (
            score,
            f"Tier-1 policy event ({src}) · α {a:.0f} · {(r.get('headline') or '')[:80]}",
        )

    # Sector-level fan-out: if a policy event for sector X is hot, apply a
    # smaller score to every ticker in sector X that didn't get a direct hit.
    sector_score: Dict[str, Tuple[float, str]] = {}
    for r in rows:
        t = r.get("ticker")
        sec = ticker_to_sector.get((t or "").upper())
        if not sec:
            continue
        a = float(r.get("alpha_score") or 0)
        prev = sector_score.get(sec)
        if not prev or a > prev[0] * 80:
            src = (r.get("source") or "").split(":")[0][:20]
            sector_score[sec] = (min(0.55, a / 100.0),
                                 f"Sector {sec} touched by {src} policy event · α {a:.0f}")
    for t, sec in ticker_to_sector.items():
        if t in out or sec not in sector_score:
            continue
        out[t] = sector_score[sec]
    return out


def _news_catalyst_factor(db) -> Dict[str, Tuple[float, str]]:
    """Tickers with multiple recent moderate-to-high alpha signals.

    Pre-mover concept: when a name accumulates several α≥40 catalysts within
    a few days, the next leg is more likely than for a one-off mention. Acts
    as a baseline factor when the specialized feeds (bulk deals, F&O, insider
    filings) haven't populated yet.
    """
    rows = _safe_query(db, """
        SELECT ticker,
               COUNT(*) AS n,
               MAX(alpha_score) AS top_alpha,
               AVG(alpha_score) AS avg_alpha
        FROM signals
        WHERE created_at >= datetime('now', '-7 days')
          AND alpha_score >= 40
          AND ticker IS NOT NULL AND ticker != ''
        GROUP BY ticker
        HAVING n >= 1
    """, label="news_catalyst")
    out: Dict[str, Tuple[float, str]] = {}
    for r in rows:
        n = int(r.get("n") or 0)
        top = float(r.get("top_alpha") or 0)
        avg = float(r.get("avg_alpha") or 0)
        # Score blends signal density (count) with quality (alpha):
        #   1 signal @ α60  → 0.45;  3 signals @ avg α55 → 0.71; 5+ @ α70 → ~1.0
        # Saturate density at 4 (was 6) and quality at α=60 (was 75) — the
        # old thresholds made it impossible for a single high-conviction
        # signal to reach a full news-catalyst sub-score.
        density = min(1.0, math.log1p(n) / math.log1p(4.0))
        quality = min(1.0, avg / 60.0)
        score = round(0.55 * quality + 0.45 * density, 3)
        if score <= 0:
            continue
        out[r["ticker"]] = (
            score,
            f"{n} catalyst(s) in 7d · top α{top:.0f} · avg α{avg:.0f}",
        )
    return out


def _valuation_factor(db, candidates: List[str]) -> Dict[str, Tuple[float, str]]:
    """yfinance-based PE/PB lookup for candidates, scored against sector median.
    Heavy: bounded by `candidates` set (post-filter, so ~50 tickers)."""
    if not candidates:
        return {}
    out: Dict[str, Tuple[float, str]] = {}
    pe_data: Dict[str, Tuple[Optional[float], Optional[float], Optional[str]]] = {}
    try:
        from stock_universe import STOCK_UNIVERSE
    except Exception:
        STOCK_UNIVERSE = {}

    try:
        import yfinance as yf
    except Exception:
        return {}

    for t in candidates[:60]:                # cap yfinance calls
        try:
            info = yf.Ticker(f"{t}.NS").fast_info
            pe = getattr(info, "trailingPe", None) or None
            pb = getattr(info, "priceToBook", None) or None
            sec = (STOCK_UNIVERSE.get(t, {}) or {}).get("sector")
            pe_data[t] = (pe, pb, sec)
        except Exception:
            continue

    # Sector medians (only across what we just fetched — best effort)
    by_sector: Dict[str, List[Tuple[float, float]]] = {}
    for t, (pe, pb, sec) in pe_data.items():
        if not sec or pe is None or pb is None or pe <= 0 or pb <= 0:
            continue
        by_sector.setdefault(sec, []).append((float(pe), float(pb)))

    def median(xs: List[float]) -> Optional[float]:
        if not xs: return None
        s = sorted(xs); n = len(s)
        return s[n//2] if n % 2 else (s[n//2-1] + s[n//2]) / 2

    sec_med = {s: (median([p for p,_ in vs]), median([b for _,b in vs]))
               for s, vs in by_sector.items() if len(vs) >= 3}

    for t, (pe, pb, sec) in pe_data.items():
        if not sec or pe is None or pb is None or pe <= 0 or pb <= 0:
            continue
        med = sec_med.get(sec)
        if not med or not med[0] or not med[1]:
            continue
        pe_med, pb_med = med
        # 0.85x of sector median = full score 1.0; >= median = 0
        pe_disc = max(0.0, (pe_med - pe) / pe_med)
        pb_disc = max(0.0, (pb_med - pb) / pb_med)
        score = min(1.0, (pe_disc + pb_disc) / 0.30)
        if score <= 0:
            continue
        out[t] = (
            score,
            f"PE {pe:.1f} (sector med {pe_med:.1f}) · PB {pb:.1f} (med {pb_med:.1f})",
        )
    return out


def _low_attention_factor(db, ticker_to_sector: Dict[str, str]) -> Dict[str, Tuple[float, str]]:
    """Tickers with notably few signals in 30d — under-covered names."""
    rows = _safe_query(db, """
        SELECT ticker, COUNT(*) AS n
        FROM signals
        WHERE created_at >= datetime('now', '-30 days')
        GROUP BY ticker
    """, label="attn")
    counts = {r["ticker"]: int(r.get("n") or 0) for r in rows}

    # Sector p25
    by_sector: Dict[str, List[int]] = {}
    for t, n in counts.items():
        sec = ticker_to_sector.get((t or "").upper())
        if sec:
            by_sector.setdefault(sec, []).append(n)
    sec_p25 = {}
    for sec, xs in by_sector.items():
        if len(xs) < 4:
            continue
        s = sorted(xs)
        sec_p25[sec] = s[len(s) // 4]

    out: Dict[str, Tuple[float, str]] = {}
    # Tickers absent from `counts` count as 0 — they're the most under-covered.
    universe = set(ticker_to_sector.keys()) | set(counts.keys())
    for t in universe:
        sec = ticker_to_sector.get(t.upper())
        if not sec or sec not in sec_p25:
            continue
        n = counts.get(t, 0)
        p25 = sec_p25[sec]
        if n < p25:
            score = min(1.0, (p25 - n) / max(1.0, float(p25)))
            out[t] = (score, f"{n} signals/30d vs sector p25 {p25}")
    return out


def _sector_tailwind_factor(db, ticker_to_sector: Dict[str, str]) -> Dict[str, Tuple[float, str]]:
    """Sector hot, ticker hasn't kept up — bucket-level only (best effort)."""
    rows = _safe_query(db, """
        SELECT ticker, AVG(alpha_score) AS aa, COUNT(*) AS n
        FROM signals
        WHERE status='active'
        GROUP BY ticker
    """, label="tail")
    ticker_alpha = {r["ticker"]: float(r.get("aa") or 0) for r in rows}

    by_sector: Dict[str, List[float]] = {}
    for t, a in ticker_alpha.items():
        sec = ticker_to_sector.get((t or "").upper())
        if sec:
            by_sector.setdefault(sec, []).append(a)
    sec_avg = {s: sum(xs)/len(xs) for s, xs in by_sector.items() if xs}
    if not sec_avg:
        return {}
    cutoff = sorted(sec_avg.values())[int(len(sec_avg) * 0.75)]   # top quartile

    out: Dict[str, Tuple[float, str]] = {}
    for t, sec in ticker_to_sector.items():
        avg = sec_avg.get(sec, 0)
        if avg < cutoff:
            continue
        my_alpha = ticker_alpha.get(t, 0)
        if my_alpha >= avg:
            continue
        gap = avg - my_alpha
        score = min(1.0, gap / 30.0)
        if score <= 0:
            continue
        out[t] = (
            score,
            f"Sector {sec} avg α {avg:.0f} (top quartile) · ticker α {my_alpha:.0f}",
        )
    return out


# ----------------------------------------------------------------------------
# Hard filters
# ----------------------------------------------------------------------------

def _surged_tickers(db, days: int = 5, threshold_pct: float = 5.0) -> Set[str]:
    """Tickers whose latest signal entry_price moved +threshold_pct% over the
    last `days`.  Best-effort — uses signals table as the only price source."""
    rows = _safe_query(db, f"""
        SELECT ticker,
               MAX(entry_price) AS hi,
               MIN(entry_price) AS lo
        FROM signals
        WHERE created_at >= datetime('now', '-{days} days')
        GROUP BY ticker
    """, label="surge")
    surged: Set[str] = set()
    for r in rows:
        hi = float(r.get("hi") or 0)
        lo = float(r.get("lo") or 0)
        if lo > 0 and (hi - lo) / lo * 100 > threshold_pct:
            surged.add(r["ticker"])
    return surged


def _forensic_dirty(db) -> Set[str]:
    rows = _safe_query(db, """
        SELECT DISTINCT ticker FROM manipulation_flags
        WHERE band IN ('likely_manipulated', 'suspicious')
          AND as_of >= datetime('now', '-30 days')
    """, label="forensic")
    return {r["ticker"] for r in rows if r.get("ticker")}


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------

def compute_premover_scores(db,
                            horizon: str = "5D",
                            limit: int = 50,
                            min_score: float = 30.0,
                            allow_universe: Optional[Set[str]] = None) -> Dict:
    """Rank the universe by PreMoverScore.

    Returns a dict ready to ship as JSON:
        {
            "horizon": "5D",
            "weights": {...},
            "data": [...candidates ranked desc...],
            "candidate_universe_size": N,
            "exclusion_counts": {"surged": .., "forensic": .., ...},
            "compute_ms": int,
        }
    """
    t0 = time.time()
    horizon = horizon if horizon in WEIGHTS else "5D"
    weights = WEIGHTS[horizon]

    # Build ticker → sector map from the existing universe (cheap import)
    try:
        from stock_universe import STOCK_UNIVERSE
    except Exception:
        STOCK_UNIVERSE = {}
    ticker_to_sector: Dict[str, str] = {
        t.upper(): (info or {}).get("sector", "")
        for t, info in STOCK_UNIVERSE.items()
    }
    if allow_universe:
        ticker_to_sector = {t: s for t, s in ticker_to_sector.items() if t in allow_universe}

    # ------------------------------------------------------------------
    # Hard filters (apply first; cheap)
    # ------------------------------------------------------------------
    excl = {"surged": 0, "forensic": 0, "no_universe": 0,
            "below_min_score": 0, "no_signal_data": 0}
    surged = _surged_tickers(db)
    dirty = _forensic_dirty(db)

    # ------------------------------------------------------------------
    # Per-factor sub-scores (cheap, run once for the whole universe)
    # ------------------------------------------------------------------
    factor_maps: Dict[str, Dict[str, Tuple[float, str]]] = {
        "accumulation":   _accumulation_factor(db),
        "insider":        _insider_factor(db),
        "oi_buildup":     _oi_buildup_factor(db),
        "vol_divergence": _vol_divergence_factor(db),
        "catalyst":       _catalyst_factor(db, ticker_to_sector),
        "news_catalyst":  _news_catalyst_factor(db),
        "low_attention":  _low_attention_factor(db, ticker_to_sector),
        "sector_tailwind":_sector_tailwind_factor(db, ticker_to_sector),
    }

    # Candidate set = tickers with at least one non-zero "substantive" factor.
    # low_attention and sector_tailwind alone are too weak to qualify (they'd
    # otherwise pull in every under-covered ticker from the universe).
    SUBSTANTIVE = ("accumulation", "insider", "oi_buildup",
                   "vol_divergence", "catalyst", "news_catalyst")
    candidate_set: Set[str] = set()
    for k in SUBSTANTIVE:
        candidate_set.update(factor_maps[k].keys())
    candidate_set = {t for t in candidate_set if t in ticker_to_sector}

    # Apply hard filters
    pre_n = len(candidate_set)
    candidate_set -= surged;        excl["surged"]    = pre_n - len(candidate_set)
    pre_n = len(candidate_set)
    candidate_set -= dirty;         excl["forensic"]  = pre_n - len(candidate_set)

    # Run yfinance valuation only for the survivors (post-hard-filter, capped
    # internally to 60 calls).  Critical: this is the slow path.
    factor_maps["valuation"] = _valuation_factor(db, sorted(candidate_set))

    # ------------------------------------------------------------------
    # Score each candidate
    # ------------------------------------------------------------------
    market_cap_lookup: Dict[str, Optional[float]] = {}      # filled lazily later

    results: List[Dict] = []
    for t in candidate_set:
        factors_out: Dict[str, Dict] = {}
        score = 0.0
        firing = 0          # how many substantive factors fired meaningfully
        strong_firing = 0   # how many fired at >= 0.7 (high conviction)
        for k, w in weights.items():
            sub_score, evidence = factor_maps.get(k, {}).get(t, (0.0, ""))
            factors_out[k] = {"score": round(sub_score, 3),
                              "weight": w,
                              "evidence": evidence}
            score += sub_score * w
            if k in SUBSTANTIVE and sub_score >= 0.35: firing += 1
            if k in SUBSTANTIVE and sub_score >= 0.70: strong_firing += 1
        score *= 100.0

        # Conviction bonus — when multiple independent factors agree the
        # stock is meaningfully more interesting than a single-factor hit.
        # This is what unlocks 60-90 scores; before the cap was ~50.
        #   2 firing:  +12%   3 firing:  +25%   4+ firing:  +40%
        #   plus +8% per strong-firing factor (sub_score >= 0.70)
        if firing >= 4:   score *= 1.40
        elif firing >= 3: score *= 1.25
        elif firing >= 2: score *= 1.12
        score *= (1.0 + 0.08 * strong_firing)
        score = min(100.0, score)

        if score < min_score:
            excl["below_min_score"] += 1
            continue
        results.append({
            "ticker":  t,
            "company": (STOCK_UNIVERSE.get(t, {}) or {}).get("name", t),
            "sector":  ticker_to_sector.get(t, ""),
            "score":   round(score, 1),
            "horizon": horizon,
            "factors": factors_out,
            "conviction": {"firing": firing, "strong_firing": strong_firing},
            "top_drivers": _top_drivers(factors_out),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:limit]

    return {
        "horizon": horizon,
        "weights": weights,
        "data": results,
        "candidate_universe_size": pre_n,
        "exclusion_counts": excl,
        "compute_ms": int((time.time() - t0) * 1000),
    }


def _top_drivers(factors_out: Dict[str, Dict], k: int = 2) -> List[str]:
    """Return the top-k factor names by weighted contribution, for the UI's
    1-line reasoning summary."""
    contribs = sorted(
        factors_out.items(),
        key=lambda kv: kv[1]["score"] * kv[1]["weight"],
        reverse=True,
    )
    return [name for name, fv in contribs[:k] if fv["score"] > 0]
