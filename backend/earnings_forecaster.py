"""Baseline earnings forecaster — pure-math layer.

Predicts next-quarter EPS estimate + 80% CI using:
  1. yfinance `earnings_dates` (gives forward EPS estimate when available)
  2. historical `earnings_reactions` rows (our archive of EPS estimate vs
     reported per quarter)

Indian-listed quarterly income statements are NOT exposed by yfinance, so
revenue forecasting is deferred to v1 (will plug in Screener.in or Tijori
scrape). v0 focuses on EPS — which is exactly what "beat or miss" depends
on.

The output of this module feeds Module 4 (`forecast_classifier.py`) which
turns the point estimate + historical surprise pattern into a beat/meet/miss
probability.

This is deterministic math. No training. No LLM. Runs in <2s per ticker.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class BaselineForecast:
    ticker: str
    target_earnings_date: Optional[str]      # ISO yyyy-mm-dd
    eps_estimate: Optional[float]             # consensus EPS for next earnings (from yfinance)
    eps_low: Optional[float]                  # 80% CI lower
    eps_high: Optional[float]                 # 80% CI upper
    eps_internal_estimate: Optional[float]    # our own derived estimate (independent of consensus)
    # Multi-metric forecasts (all values in ₹ crore unless _pct suffix)
    revenue_estimate: Optional[float] = None
    revenue_low: Optional[float] = None
    revenue_high: Optional[float] = None
    net_profit_estimate: Optional[float] = None
    net_profit_low: Optional[float] = None
    net_profit_high: Optional[float] = None
    ebitda_estimate: Optional[float] = None
    ebitda_low: Optional[float] = None
    ebitda_high: Optional[float] = None
    operating_income_estimate: Optional[float] = None
    operating_margin_pct: Optional[float] = None     # forecast %
    ebitda_margin_pct: Optional[float] = None         # forecast %
    revenue_growth_yoy_pct: Optional[float] = None
    revenue_growth_qoq_pct: Optional[float] = None
    trailing_quarters: int = 0                # how many historical rows we used
    trailing_beat_rate: Optional[float] = None
    trailing_avg_surprise_pct: Optional[float] = None
    trailing_surprise_std: Optional[float] = None
    eps_trend_qoq: Optional[float] = None     # avg QoQ growth from EPS history
    eps_trend_yoy: Optional[float] = None
    data_insufficient: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        def _r(v, n=2):
            return round(v, n) if v is not None and not (isinstance(v, float) and math.isnan(v)) else None
        return {
            "ticker": self.ticker,
            "target_earnings_date": self.target_earnings_date,
            "eps_estimate": _r(self.eps_estimate),
            "eps_low": _r(self.eps_low),
            "eps_high": _r(self.eps_high),
            "eps_internal_estimate": _r(self.eps_internal_estimate),
            "revenue_estimate": _r(self.revenue_estimate, 0),
            "revenue_low": _r(self.revenue_low, 0),
            "revenue_high": _r(self.revenue_high, 0),
            "net_profit_estimate": _r(self.net_profit_estimate, 0),
            "net_profit_low": _r(self.net_profit_low, 0),
            "net_profit_high": _r(self.net_profit_high, 0),
            "ebitda_estimate": _r(self.ebitda_estimate, 0),
            "ebitda_low": _r(self.ebitda_low, 0),
            "ebitda_high": _r(self.ebitda_high, 0),
            "operating_income_estimate": _r(self.operating_income_estimate, 0),
            "operating_margin_pct": _r(self.operating_margin_pct, 2),
            "ebitda_margin_pct": _r(self.ebitda_margin_pct, 2),
            "revenue_growth_yoy_pct": _r(self.revenue_growth_yoy_pct, 1),
            "revenue_growth_qoq_pct": _r(self.revenue_growth_qoq_pct, 1),
            "trailing_quarters": self.trailing_quarters,
            "trailing_beat_rate": _r(self.trailing_beat_rate, 2),
            "trailing_avg_surprise_pct": _r(self.trailing_avg_surprise_pct, 1),
            "trailing_surprise_std": _r(self.trailing_surprise_std, 1),
            "eps_trend_qoq": _r(self.eps_trend_qoq, 3),
            "eps_trend_yoy": _r(self.eps_trend_yoy, 3),
            "data_insufficient": self.data_insufficient,
            "notes": self.notes,
        }


def _to_nse_symbol(ticker: str) -> str:
    t = ticker.upper().strip()
    if t.endswith(".NS") or t.endswith(".BO"):
        return t
    return f"{t}.NS"


def _project_metric(series: List[float], lookback: int = 5) -> Optional[Dict[str, Optional[float]]]:
    """Given a quarterly series (newest first), return next-quarter point
    estimate, 80% CI band, and trend stats.

    Method: blend QoQ extrapolation (60%) with YoY extrapolation (40%);
    sigma from rolling std of QoQ growths. Returns None when series is
    too thin (need at least 2 non-null points).
    """
    clean = [x for x in series if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if len(clean) < 2:
        return None
    # Growth rates
    growths = []
    for i in range(len(clean) - 1):
        old, new = clean[i + 1], clean[i]
        if abs(old) < 1.0:  # avoid division by ~0
            continue
        growths.append((new - old) / abs(old))
    if not growths:
        return None
    mean_g = sum(growths) / len(growths)
    var_g = sum((g - mean_g) ** 2 for g in growths) / max(len(growths) - 1, 1)
    sigma_g = math.sqrt(var_g)

    last = clean[0]
    qoq_proj = last * (1 + mean_g)

    yoy_proj = None
    yoy_growth = None
    if len(clean) >= 5:
        yoy_anchor = clean[4]
        if abs(yoy_anchor) > 1.0:
            yoy_growth = (clean[0] - yoy_anchor) / abs(yoy_anchor)
            # Project: anchor × (1 + same YoY growth) → next-Q analog
            # use 1 sequential step on top of yoy growth pattern
            yoy_proj = yoy_anchor * (1 + yoy_growth) * (1 + mean_g / 4.0)

    if yoy_proj is not None:
        point = 0.6 * qoq_proj + 0.4 * yoy_proj
    else:
        point = qoq_proj

    # CI band: 1.28 sigma_g × last (one-quarter shock)
    band = 1.28 * sigma_g * abs(last)
    return {
        "point": point,
        "low": point - band,
        "high": point + band,
        "qoq_growth": mean_g,
        "yoy_growth": yoy_growth,
    }


def _pull_quarterly_metrics(yf_ticker: str) -> Dict[str, List[float]]:
    """Pull revenue, net income, EBITDA, operating income from yfinance
    quarterly financials. Returns dict of {metric_name: [values, newest first]}.

    Values are returned in raw currency units (₹) — caller converts to crore
    if displaying.
    """
    out: Dict[str, List[float]] = {}
    try:
        import yfinance as yf
        t = yf.Ticker(yf_ticker)
        fin = t.get_financials(freq="quarterly")
        if fin is None or fin.empty:
            return out
        wanted = {
            "TotalRevenue":     "revenue",
            "NetIncome":        "net_income",
            "EBITDA":           "ebitda",
            "OperatingIncome":  "operating_income",
        }
        for yf_name, our_name in wanted.items():
            if yf_name in fin.index:
                vals = fin.loc[yf_name].tolist()  # newest first
                out[our_name] = [float(v) if v is not None and not math.isnan(v) else None for v in vals]
    except Exception as e:
        logger.debug(f"quarterly metrics fetch failed for {yf_ticker}: {e}")
    return out


def _pull_info_metrics(yf_ticker: str) -> Dict[str, Optional[float]]:
    """TTM ratios + forward EPS from yfinance .info."""
    try:
        import yfinance as yf
        info = yf.Ticker(yf_ticker).info or {}
        return {
            "forward_eps":      info.get("forwardEps"),
            "trailing_eps":     info.get("trailingEps"),
            "operating_margin": info.get("operatingMargins"),  # TTM, fraction
            "ebitda_margin":    info.get("ebitdaMargins"),
            "profit_margin":    info.get("profitMargins"),
            "revenue_growth":   info.get("revenueGrowth"),
            "earnings_growth":  info.get("earningsQuarterlyGrowth"),
        }
    except Exception as e:
        logger.debug(f"info metrics fetch failed for {yf_ticker}: {e}")
        return {}


def _pull_earnings_dates(yf_ticker: str) -> Optional["pd.DataFrame"]:
    """Pull yfinance earnings_dates. Returns None on any failure.

    yfinance gives the FUTURE row for unreported earnings (Reported EPS = NaN,
    Surprise(%) = NaN, EPS Estimate populated). Historical rows have all 3.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(yf_ticker)
        ed = t.earnings_dates
        if ed is None or ed.empty:
            return None
        # Normalize timezone to naive so we can compare against datetime.now()
        if ed.index.tz is not None:
            ed.index = ed.index.tz_localize(None)
        return ed.sort_index(ascending=False)  # newest first
    except Exception as e:
        logger.debug(f"earnings_dates fetch failed for {yf_ticker}: {e}")
        return None


def _stats(arr: List[float]) -> Tuple[Optional[float], Optional[float]]:
    """Return (mean, std) ignoring NaN; returns (None, None) on empty input."""
    clean = [x for x in arr if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not clean:
        return None, None
    n = len(clean)
    mean = sum(clean) / n
    if n < 2:
        return mean, 0.0
    var = sum((x - mean) ** 2 for x in clean) / (n - 1)
    return mean, math.sqrt(var)


def _qoq_growth(eps_series: List[float]) -> Optional[float]:
    """Average sequential growth rate over the series (newest first)."""
    if len(eps_series) < 2:
        return None
    growths = []
    # eps_series[0] is the most recent; we want (prev / older) - 1
    for i in range(len(eps_series) - 1):
        new = eps_series[i]
        old = eps_series[i + 1]
        if old is None or new is None:
            continue
        if abs(old) < 0.001:
            continue
        growths.append((new - old) / abs(old))
    if not growths:
        return None
    return sum(growths) / len(growths)


def _yoy_growth(eps_series: List[float]) -> Optional[float]:
    """Year-over-year growth: latest vs the one 4 quarters back."""
    if len(eps_series) < 5:
        return None
    new = eps_series[0]
    old = eps_series[4]
    if new is None or old is None or abs(old) < 0.001:
        return None
    return (new - old) / abs(old)


def forecast(db, ticker: str, *, quarters_lookback: int = 8) -> BaselineForecast:
    """Compute the baseline forecast for `ticker`.

    Reads (1) historical EPS history from our `earnings_reactions` table,
    (2) the upcoming earnings date + consensus EPS from yfinance.

    `db` is a TickwaveDB-shaped object (has `.conn` + `.is_postgres`).
    """
    tk = ticker.upper().strip().replace(".NS", "").replace(".BO", "")
    yf_sym = _to_nse_symbol(tk)
    fc = BaselineForecast(ticker=tk, target_earnings_date=None,
                          eps_estimate=None, eps_low=None, eps_high=None,
                          eps_internal_estimate=None)

    # --- 1. yfinance: find next earnings date + consensus EPS estimate ----
    ed = _pull_earnings_dates(yf_sym)
    if ed is None or ed.empty:
        fc.notes.append("yfinance earnings_dates unavailable")
    else:
        now = datetime.utcnow()
        future_rows = ed[ed.index > now]
        if not future_rows.empty:
            # Pick the SOONEST future earnings (last in descending sort)
            soonest = future_rows.iloc[-1]
            fc.target_earnings_date = future_rows.index[-1].strftime("%Y-%m-%d")
            est = soonest.get("EPS Estimate")
            if est is not None and not (isinstance(est, float) and math.isnan(est)):
                fc.eps_estimate = float(est)
        else:
            fc.notes.append("no future earnings date in yfinance — using most-recent row")
            # Take whatever the latest row is (may be a result already dropped)
            soonest = ed.iloc[0]
            fc.target_earnings_date = ed.index[0].strftime("%Y-%m-%d")
            est = soonest.get("EPS Estimate")
            if est is not None and not (isinstance(est, float) and math.isnan(est)):
                fc.eps_estimate = float(est)

    # --- 2. Our archive: trailing history -------------------------------
    cur = db.conn.cursor()
    is_pg = getattr(db, "is_postgres", False)
    sql = ("SELECT earnings_date, eps_estimate, eps_reported, surprise_pct, ret_1d_pct "
           "FROM earnings_reactions "
           "WHERE UPPER(ticker) = ? "
           "ORDER BY earnings_date DESC LIMIT ?")
    if is_pg:
        sql = sql.replace("?", "%s")
    cur.execute(sql, (tk, quarters_lookback))
    rows = cur.fetchall()
    fc.trailing_quarters = len(rows)

    if not rows:
        fc.notes.append(f"no earnings_reactions history for {tk}")
        fc.data_insufficient = fc.eps_estimate is None
        return fc

    eps_reported = [r[2] for r in rows]
    surprises = [r[3] for r in rows if r[3] is not None]

    if surprises:
        mean_s, std_s = _stats(surprises)
        fc.trailing_avg_surprise_pct = mean_s
        fc.trailing_surprise_std = std_s
        fc.trailing_beat_rate = sum(1 for s in surprises if s > 0) / len(surprises)

    fc.eps_trend_qoq = _qoq_growth(eps_reported)
    fc.eps_trend_yoy = _yoy_growth(eps_reported)

    # --- 3. Derive an internal estimate (independent of consensus) ----
    # Blend QoQ extrapolation (weighted 0.6) with YoY extrapolation (weighted 0.4).
    # Fall through gracefully if components missing.
    eps_internal = None
    if eps_reported and eps_reported[0] is not None:
        last = eps_reported[0]
        qoq_proj = last * (1 + fc.eps_trend_qoq) if fc.eps_trend_qoq is not None else None
        yoy_anchor = eps_reported[3] if len(eps_reported) > 3 else None
        yoy_proj = (yoy_anchor * (1 + fc.eps_trend_yoy)
                    if (yoy_anchor is not None and fc.eps_trend_yoy is not None)
                    else None)
        if qoq_proj is not None and yoy_proj is not None:
            eps_internal = 0.6 * qoq_proj + 0.4 * yoy_proj
        elif qoq_proj is not None:
            eps_internal = qoq_proj
        elif yoy_proj is not None:
            eps_internal = yoy_proj
    fc.eps_internal_estimate = eps_internal

    # --- 4. CI band ------------------------------------------------------
    # 80% CI = est ± 1.28 × sigma, where sigma comes from historical
    # absolute deviation between estimate and reported.
    # If we have the consensus from yfinance, use that as the point;
    # otherwise fall back to eps_internal.
    point = fc.eps_estimate if fc.eps_estimate is not None else eps_internal
    if point is not None and rows:
        abs_devs = []
        for _, e, r, _, _ in rows:
            if e is not None and r is not None:
                abs_devs.append(abs(r - e))
        if abs_devs:
            sigma = sum(abs_devs) / len(abs_devs)  # MAD as a robust sigma proxy
            fc.eps_low = point - 1.28 * sigma
            fc.eps_high = point + 1.28 * sigma
        else:
            # No estimate/reported pairs — fall back to fractional CI
            fc.eps_low = point * 0.9
            fc.eps_high = point * 1.1
            fc.notes.append("CI based on ±10% heuristic (no estimate-vs-reported pairs)")

    # --- 5. Multi-metric quarterly forecasts ---------------------------
    # Pulls Revenue, Net Income, EBITDA, Operating Income from yfinance
    # quarterly financials and projects each forward one quarter.
    # Values come back in ₹ raw — we convert to ₹ crore (÷ 1e7) for storage.
    metrics = _pull_quarterly_metrics(yf_sym)
    CR = 1e7  # 1 crore = 10 million

    if "revenue" in metrics:
        proj = _project_metric(metrics["revenue"])
        if proj:
            fc.revenue_estimate = proj["point"] / CR
            fc.revenue_low = proj["low"] / CR
            fc.revenue_high = proj["high"] / CR
            fc.revenue_growth_qoq_pct = (proj["qoq_growth"] or 0) * 100
            if proj["yoy_growth"] is not None:
                fc.revenue_growth_yoy_pct = proj["yoy_growth"] * 100
    if "net_income" in metrics:
        proj = _project_metric(metrics["net_income"])
        if proj:
            fc.net_profit_estimate = proj["point"] / CR
            fc.net_profit_low = proj["low"] / CR
            fc.net_profit_high = proj["high"] / CR
    if "ebitda" in metrics:
        proj = _project_metric(metrics["ebitda"])
        if proj:
            fc.ebitda_estimate = proj["point"] / CR
            fc.ebitda_low = proj["low"] / CR
            fc.ebitda_high = proj["high"] / CR
    if "operating_income" in metrics:
        proj = _project_metric(metrics["operating_income"])
        if proj:
            fc.operating_income_estimate = proj["point"] / CR

    # Derived margin %s (only if we have both numerator + denominator)
    if fc.revenue_estimate and fc.revenue_estimate > 0:
        if fc.operating_income_estimate:
            fc.operating_margin_pct = 100 * fc.operating_income_estimate / fc.revenue_estimate
        if fc.ebitda_estimate:
            fc.ebitda_margin_pct = 100 * fc.ebitda_estimate / fc.revenue_estimate

    # If yfinance quarterly was empty, try the .info TTM margins as fallback
    if fc.operating_margin_pct is None or fc.ebitda_margin_pct is None:
        info = _pull_info_metrics(yf_sym)
        if fc.operating_margin_pct is None and info.get("operating_margin") is not None:
            fc.operating_margin_pct = info["operating_margin"] * 100
            fc.notes.append("operating_margin from TTM .info (no quarterly data)")
        if fc.ebitda_margin_pct is None and info.get("ebitda_margin") is not None:
            fc.ebitda_margin_pct = info["ebitda_margin"] * 100
            fc.notes.append("ebitda_margin from TTM .info (no quarterly data)")

    fc.data_insufficient = fc.trailing_quarters < 3 and fc.eps_estimate is None
    return fc


# --- CLI smoke test -------------------------------------------------------

def main():
    import argparse, json, os, sys
    p = argparse.ArgumentParser(description="Baseline earnings forecaster — smoke test")
    p.add_argument("ticker", nargs="?", default="RELIANCE", help="NSE ticker (without .NS)")
    p.add_argument("--quarters", type=int, default=8)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scraper'))
    from database_schema import TickwaveDB  # type: ignore
    db = TickwaveDB()
    db.init_schema()

    fc = forecast(db, args.ticker, quarters_lookback=args.quarters)
    print(json.dumps(fc.to_dict(), indent=2))


if __name__ == "__main__":
    main()
