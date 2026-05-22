"""Async LLM augmentation for the earnings forecast — FREE providers only.

Adds a 2-sentence narrative summary to a forecast row. Runs as a fire-and-
forget background pass after `forecast_orchestrator.upsert()`. Failure
modes — missing API key, governor refusal, timeout, parse error — all
silently fall through to the rule-based forecast (no narrative_summary,
llm_generated=False on the row).

The LLM ONLY authors English connective tissue. All numbers in the
narrative come from the structured forecast row (template-injected), so
the LLM cannot hallucinate revenue/EPS/probability figures.

Provider: Groq (already integrated via `backend/ai_groq.py` + budget governor
in `scraper/groq_governor.py`). Model: llama-3.3-70b-versatile, free tier.

NOT in any hot path — only called from `forecast_job.py` and the on-demand
endpoint AFTER the structured forecast has been built and stored.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(__file__)
for p in (_HERE, os.path.join(_HERE, '..', 'scraper')):
    if p not in sys.path:
        sys.path.insert(0, p)


def _format_pct(v, default="—") -> str:
    if v is None:
        return default
    try:
        return f"{float(v):+.1f}%"
    except Exception:
        return default


def _format_eps(low, high, default="—") -> str:
    if low is None or high is None:
        return default
    return f"₹{float(low):.1f}–{float(high):.1f}"


def _template_narrative(fc: Dict[str, Any]) -> str:
    """Pure template fallback when LLM is unavailable. No connective English
    flair, but the same facts."""
    ticker = fc.get("ticker", "?")
    hit = (fc.get("predicted_hit") or "meet").lower()
    conf = fc.get("hit_confidence") or 0
    eps_range = _format_eps(fc.get("eps_low"), fc.get("eps_high"))
    ret = fc.get("expected_ret_1d_pct")
    ew = fc.get("entry_window") or {}
    action = ew.get("recommended_action", "wait")
    wait_min = ew.get("wait_minutes_estimate", 0)

    parts = []
    parts.append(f"{ticker}: predicted {hit} with {conf*100:.0f}% confidence (EPS range {eps_range}).")
    if action == "enter_now":
        parts.append("Clean signal — enter on the open.")
    elif action == "wait":
        parts.append(f"Moderate chase risk — wait ~{wait_min} min after open.")
    elif action == "skip":
        parts.append("Miss expected — avoid the trade.")
    if ret is not None:
        parts.append(f"Expected day-1 reaction: {_format_pct(ret)}.")
    return " ".join(parts)


def _llm_narrative(fc: Dict[str, Any]) -> Optional[str]:
    """Try the LLM. Returns None on any failure (caller falls back to template)."""
    try:
        from ai_groq import groq_chat  # type: ignore
    except ImportError:
        try:
            from backend.ai_groq import groq_chat  # type: ignore
        except ImportError:
            logger.debug("ai_groq not importable; skipping LLM narrative")
            return None

    if not os.getenv("GROQ_API_KEY"):
        return None

    ticker = fc.get("ticker", "?")
    hit = fc.get("predicted_hit", "meet")
    conf = fc.get("hit_confidence", 0) or 0
    eps_low = fc.get("eps_low")
    eps_high = fc.get("eps_high")
    expected_ret = fc.get("expected_ret_1d_pct")
    ew = fc.get("entry_window") or {}
    drivers = fc.get("top_drivers") or []

    # Template-inject every number into the prompt — LLM ONLY writes English.
    facts = {
        "ticker": ticker,
        "predicted_hit": hit,
        "hit_confidence_pct": round(conf * 100, 0),
        "eps_range": _format_eps(eps_low, eps_high) if eps_low and eps_high else "n/a",
        "expected_day1_reaction": _format_pct(expected_ret) if expected_ret is not None else "n/a",
        "entry_action": ew.get("recommended_action", "wait"),
        "wait_minutes": ew.get("wait_minutes_estimate", 0),
        "fade_probability_pct": round((ew.get("fade_probability") or 0) * 100, 0),
        "top_drivers": [
            {"name": d.get("name"), "summary": d.get("summary")}
            for d in drivers[:3]
        ],
    }

    system = (
        "You are a concise Indian-equity analyst. Write a TWO-sentence "
        "summary of an earnings forecast. Use ONLY the facts provided — "
        "DO NOT invent any numbers, dates, or claims. Plain English, no "
        "hedging language, no bullet points, no markdown. Maximum 50 words "
        "total across both sentences."
    )
    user = (
        f"Facts:\n{json.dumps(facts, indent=2)}\n\n"
        "Sentence 1: state the prediction and its key driver. "
        "Sentence 2: give the entry-timing recommendation."
    )

    try:
        out = groq_chat(
            module="summarize",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=120,
            est_tokens=400,
            temperature=0.2,
            timeout_secs=10,
        )
        if not out:
            return None
        text = out.strip().strip('"').strip()
        # Hard-limit length so a chatty model can't blow past
        if len(text) > 600:
            text = text[:600].rsplit(".", 1)[0] + "."
        return text
    except Exception as e:
        logger.debug(f"groq_chat failed for {ticker}: {e}")
        return None


def add_narrative(fc: Dict[str, Any]) -> Dict[str, Any]:
    """Augment a forecast dict in-place with `narrative_summary` and
    `llm_generated` fields. Returns the same dict for chaining."""
    text = _llm_narrative(fc)
    if text:
        fc["narrative_summary"] = text
        fc["llm_generated"] = True
    else:
        fc["narrative_summary"] = _template_narrative(fc)
        fc["llm_generated"] = False
    return fc


# --- CLI smoke test ------------------------------------------------------

def main():
    import argparse, json as _j
    p = argparse.ArgumentParser()
    p.add_argument("ticker", nargs="?", default="SBIN")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from database_schema import TickwaveDB  # type: ignore
    from forecast_orchestrator import compute_full_forecast
    db = TickwaveDB()
    db.init_schema()
    fc = compute_full_forecast(db, args.ticker)
    fc = add_narrative(fc)
    print(fc.get("narrative_summary"))
    print(f"(llm_generated: {fc.get('llm_generated')})")


if __name__ == "__main__":
    main()
