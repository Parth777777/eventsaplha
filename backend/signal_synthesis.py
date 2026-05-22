"""
signal_synthesis — deterministic, copyright-safe one-line summaries.

Produces OUR factual paraphrase of a signal instead of reproducing the
publisher's RSS summary text. Templates are keyed on `event_type` so the
phrasing reflects the actual event class (earnings, order win, M&A,
policy, insider) rather than the wire's prose.

Why this exists (Addendum 2026-05-18):
  Republishing publisher-provided text — even RSS-supplied summaries —
  carries copyright risk under the Indian Copyright Act. Showing facts
  (numbers, event types, ticker names, source counts) is fair-use
  aggregation. Showing the wire's prose is not.

Public API:
  synthesize_summary(signal_dict) -> str
      Returns a short factual line, max ~180 chars, never reproduces wire
      text. Falls back to a generic template when fields are missing.

Zero LLM cost — all template substitution.
"""
from __future__ import annotations

from typing import Dict, Optional


# Per-event_type templates. Each is a callable so we can branch on field
# availability inline instead of building a giant string format spec.

def _t_earnings(s: Dict) -> str:
    tk = s.get("ticker") or s.get("symbol") or "company"
    mag = s.get("magnitude_pct") or s.get("magnitude")
    sources_n = int(s.get("cluster_size") or 1)
    sources_clause = (
        f"{sources_n} independent source{'s' if sources_n != 1 else ''}"
        if sources_n >= 2 else "Single source"
    )
    if mag is not None:
        try:
            sign = "+" if float(mag) >= 0 else ""
            return (f"Earnings event for {tk}: model magnitude {sign}{float(mag):.1f}%. "
                    f"{sources_clause} confirmed.")
        except (TypeError, ValueError):
            pass
    return f"Earnings event for {tk}. {sources_clause} confirmed."


def _t_order_win(s: Dict) -> str:
    tk = s.get("ticker") or "company"
    amt = s.get("order_value_cr") or s.get("amount_cr")
    sources_n = int(s.get("cluster_size") or 1)
    sources_clause = f"{sources_n} source{'s' if sources_n != 1 else ''}"
    amt_clause = ""
    if amt:
        try:
            amt_clause = f" worth ₹{float(amt):.0f} crore"
        except (TypeError, ValueError):
            pass
    return f"Order intimation for {tk}{amt_clause}. Reported by {sources_clause}."


def _t_ma_deal(s: Dict) -> str:
    tk = s.get("ticker") or "company"
    counterparty = s.get("counterparty") or s.get("target")
    sources_n = int(s.get("cluster_size") or 1)
    sources_clause = f"{sources_n} source{'s' if sources_n != 1 else ''}"
    if counterparty:
        return f"M&A event: {tk} involved with {counterparty}. {sources_clause} reporting."
    return f"M&A activity reported for {tk}. {sources_clause} reporting."


def _t_policy(s: Dict) -> str:
    tk = s.get("ticker") or "ticker"
    body = s.get("policy_body") or s.get("regulator") or "policy/regulatory body"
    sources_n = int(s.get("cluster_size") or 1)
    sources_clause = f"{sources_n} source{'s' if sources_n != 1 else ''}"
    return f"Policy/regulatory event from {body} affecting {tk}'s sector. {sources_clause}."


def _t_insider(s: Dict) -> str:
    tk = s.get("ticker") or "company"
    tag = s.get("intent") or s.get("insider_tag") or "activity"
    return f"Insider activity for {tk}: {tag.replace('_', ' ')}. SEBI disclosure confirmed."


def _t_corporate_action(s: Dict) -> str:
    tk = s.get("ticker") or "company"
    action = s.get("intent") or s.get("action") or "corporate action"
    return f"Corporate action for {tk}: {action.replace('_', ' ')}."


def _t_credit_rating(s: Dict) -> str:
    tk = s.get("ticker") or "company"
    agency = s.get("agency") or "rating agency"
    action = s.get("rating_action") or "rating action"
    return f"{agency} {action} for {tk}."


def _t_concall(s: Dict) -> str:
    tk = s.get("ticker") or "company"
    return f"Concall transcript / investor call available for {tk}."


def _t_default(s: Dict) -> str:
    tk = s.get("ticker") or "ticker"
    et = (s.get("event_type") or "event").replace("_", " ")
    sources_n = int(s.get("cluster_size") or 1)
    sources_clause = (
        f"Confirmed by {sources_n} independent source{'s' if sources_n != 1 else ''}"
        if sources_n >= 2 else "Single source so far"
    )
    return f"{et.capitalize()} event for {tk}. {sources_clause}."


_TEMPLATES = {
    "earnings":          _t_earnings,
    "earnings_beat":     _t_earnings,
    "earnings_miss":     _t_earnings,
    "earnings_whisper":  _t_earnings,
    "order_win":         _t_order_win,
    "order_intimation":  _t_order_win,
    "contract":          _t_order_win,
    "ma_deal":           _t_ma_deal,
    "merger":            _t_ma_deal,
    "acquisition":       _t_ma_deal,
    "policy":            _t_policy,
    "regulatory":        _t_policy,
    "insider":           _t_insider,
    "insider_buy":       _t_insider,
    "insider_sell":      _t_insider,
    "pit_buy":           _t_insider,
    "sast_acquire":      _t_insider,
    "promoter_buy":      _t_insider,
    "corporate_action":  _t_corporate_action,
    "dividend":          _t_corporate_action,
    "bonus":             _t_corporate_action,
    "split":             _t_corporate_action,
    "buyback":           _t_corporate_action,
    "credit_rating":     _t_credit_rating,
    "rating_upgrade":    _t_credit_rating,
    "rating_downgrade":  _t_credit_rating,
    "concall_transcript":_t_concall,
    "investor_meet":     _t_concall,
}


def synthesize_summary(signal: Dict) -> str:
    """Return a 1-line factual paraphrase. Never reproduces wire prose.

    Caps output at 180 chars. Always returns a non-empty string — falls
    back to generic template when event_type is unknown or missing.
    """
    if not isinstance(signal, dict):
        return "Market event detected. Multiple sources monitored."
    et = (signal.get("event_type") or "").lower().strip()
    fn = _TEMPLATES.get(et, _t_default)
    try:
        out = fn(signal) or _t_default(signal)
    except Exception:
        out = _t_default(signal)
    # Hard cap so a freak template can't blow up the card layout
    return out[:180].rstrip()


def synthesize_headline(signal: Dict) -> str:
    """Use canonical_headline (from event_clusters) when available, else
    the signal's own headline, else a templated string. Caps at 140 chars.
    Single source code path so the API layer + frontend pick the same one.
    """
    if not isinstance(signal, dict):
        return "Market event"
    canon = signal.get("canonical_headline")
    if canon and str(canon).strip():
        return str(canon).strip()[:140]
    h = signal.get("headline") or signal.get("title")
    if h and str(h).strip():
        return str(h).strip()[:140]
    tk = signal.get("ticker") or "ticker"
    et = (signal.get("event_type") or "event").replace("_", " ").title()
    return f"{et} · {tk}"
