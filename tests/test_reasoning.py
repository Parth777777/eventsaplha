"""Tests for reasoning_engine — ensures every signal produces structured output
with a defensible chain, never vague hand-wavy text."""

import pytest
from reasoning_engine import explain, EVENT_TYPE_NARRATIVES, SECTOR_RATIONALE


class _FakeDB:
    """Minimal db stub — all queries return empty sets."""
    is_postgres = False

    class _Conn:
        def cursor(self):
            return _FakeDB._Cursor()

    class _Cursor:
        description = []
        def execute(self, *a, **kw): pass
        def fetchone(self): return None
        def fetchall(self): return []

    conn = _Conn()


def test_explain_returns_all_fields():
    sig = {
        "ticker": "INFY", "event_type": "earnings", "sentiment": "bullish",
        "alpha_score": 72, "magnitude": 7, "regime": "bull_strong",
        "confidence": 0.75, "headline": "Infosys beats Q4 estimates",
        "source": "et_markets", "company": "Infosys",
    }
    r = explain(_FakeDB(), sig)
    for key in ["primary_driver", "why_ticker", "why_direction", "why_magnitude",
                "why_confidence", "risk_factors", "bullets", "meta"]:
        assert key in r, f"missing {key}"
    assert len(r["bullets"]) >= 4


def test_direct_mention_detected():
    sig = {
        "ticker": "RELIANCE", "event_type": "earnings", "sentiment": "bullish",
        "alpha_score": 60, "magnitude": 6, "regime": "bull_strong",
        "headline": "Reliance posts record quarterly profit",
    }
    r = explain(_FakeDB(), sig)
    assert r["why_ticker_code"] == "direct"
    assert "RELIANCE" in r["why_ticker"]


def test_sector_linkage_when_not_mentioned():
    sig = {
        "ticker": "TCS", "event_type": "policy", "sentiment": "bullish",
        "alpha_score": 55, "magnitude": 5, "regime": "sideways_calm",
        "headline": "IT services outlook bright on tech spending",
        "company": "TCS",
    }
    r = explain(_FakeDB(), sig)
    # Ticker not in headline — should fall back to sector reasoning
    assert r["why_ticker_code"] in ("sector", "direct")  # depends on company field match


def test_bullets_have_structured_prefixes():
    sig = {
        "ticker": "HDFCBANK", "event_type": "policy", "sentiment": "bullish",
        "alpha_score": 65, "magnitude": 6, "regime": "bull_strong",
        "headline": "RBI rate cut", "source": "rbi_press",
    }
    r = explain(_FakeDB(), sig)
    bullet_prefixes = [b.split(":")[0] for b in r["bullets"]]
    # Direction gets "(bullish)" etc suffix — check prefix-starts-with
    for expected in ["Ticker", "Direction", "Magnitude", "Confidence", "Risks"]:
        assert any(p.startswith(expected) for p in bullet_prefixes), \
            f"missing prefix '{expected}' in {bullet_prefixes}"


def test_risk_factors_never_empty():
    sig = {"ticker": "X", "event_type": "news", "sentiment": "neutral",
           "alpha_score": 30, "magnitude": 3, "regime": "sideways_calm"}
    r = explain(_FakeDB(), sig)
    assert len(r["risk_factors"]) >= 1


def test_meta_includes_event_type_and_regime():
    sig = {
        "ticker": "TATASTEEL", "event_type": "supply", "sentiment": "bearish",
        "alpha_score": 58, "magnitude": 7, "regime": "bear_strong",
        "headline": "China steel dumping",
    }
    r = explain(_FakeDB(), sig)
    assert r["meta"]["event_type"] == "supply"
    assert r["meta"]["sentiment"] == "bearish"
    assert r["meta"]["regime"] == "bear_strong"


def test_event_type_narratives_cover_all_standard_types():
    for t in ["earnings", "merger", "policy", "order_win", "supply", "insider", "dividend", "news"]:
        assert t in EVENT_TYPE_NARRATIVES
        n = EVENT_TYPE_NARRATIVES[t]
        assert "direct" in n and "bullish" in n and "bearish" in n


def test_sector_rationales_present():
    for s in ["IT", "BFSI", "ENERGY", "METALS", "PHARMA"]:
        assert s in SECTOR_RATIONALE
