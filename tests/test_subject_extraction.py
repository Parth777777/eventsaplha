"""Tests for the wrong-subject bug fix in scraper subject extraction.

The bug: an article headlined "Tata Steel sees higher steel prices..." was
matching TCS (Tata Consultancy Services) and getting published as a TCS pick,
because both `stock_universe.UNIVERSE_SHORT_NAMES` and
`entity_linker._name_forms` registered "tata" as a valid TCS match form.

These tests pin the fix from two angles:
  1. Universe-level: UNIVERSE_SHORT_NAMES must NOT map "tata"/"bajaj"/"adani"
     /"hdfc"/"reliance" to a single ticker — those brand prefixes head 2+
     companies and need bigram disambiguation.
  2. Scorer-level: entity_linker.score(TCS, "Tata Steel sees ...") must
     return 0.0 confidence. entity_linker.score(TATASTEEL, same text) must
     return high confidence.
"""
import pytest

from entity_linker import (
    _ambiguous_first_tokens,
    _name_forms,
    filter_candidates,
    score,
)


# A miniature companies map covering the bug scenarios — same shape as
# scraper.config.STOCK_COMPANIES so the production code path is exercised
# faithfully.
COMPANIES = {
    "TCS":         "Tata Consultancy Services",
    "TATASTEEL":   "Tata Steel",
    "TATAPOWER":   "Tata Power",
    "TATAMOTORS":  "Tata Motors",
    "TATACONSUM":  "Tata Consumer Products",
    "BAJFINANCE":  "Bajaj Finance",
    "BAJAJFINSV":  "Bajaj Finserv",
    "BAJAJ-AUTO":  "Bajaj Auto",
    "ADANIENT":    "Adani Enterprises",
    "ADANIPORTS":  "Adani Ports",
    "HDFCBANK":    "HDFC Bank",
    "HDFCLIFE":    "HDFC Life Insurance",
    "INFY":        "Infosys",                   # unambiguous first token
    "WIPRO":       "Wipro",                     # unambiguous first token
}


# ---- ambiguous-token detection -------------------------------------------

def test_detects_ambiguous_brand_prefixes():
    amb = _ambiguous_first_tokens(COMPANIES)
    assert "tata"     in amb        # heads 5 companies
    assert "bajaj"    in amb        # heads 3 companies
    assert "adani"    in amb        # heads 2 companies
    assert "hdfc"     in amb        # heads 2 companies
    assert "infosys"  not in amb    # only one Infosys
    assert "wipro"    not in amb


def test_unique_first_token_still_included_as_form():
    """Companies whose first token is unique keep the bare first-token form."""
    forms = _name_forms("INFY", companies_map=COMPANIES)
    assert "infosys" in forms
    assert "infy" in forms


def test_ambiguous_first_token_NOT_in_form_for_TCS():
    """The actual bug — 'tata' must NOT be registered as a TCS match form."""
    forms = _name_forms("TCS", companies_map=COMPANIES)
    assert "tata" not in forms                  # the bug
    assert "tata consultancy" in forms          # the fix
    assert "tcs" in forms                       # ticker symbol always works


def test_ambiguous_first_token_NOT_in_form_for_TATASTEEL():
    forms = _name_forms("TATASTEEL", companies_map=COMPANIES)
    assert "tata" not in forms
    assert "tata steel" in forms
    assert "tatasteel" in forms


def test_ambiguous_first_token_NOT_in_form_for_BAJFINANCE():
    forms = _name_forms("BAJFINANCE", companies_map=COMPANIES)
    assert "bajaj" not in forms
    assert "bajaj finance" in forms


# ---- score() behaviour on the bug scenarios ------------------------------

TATA_STEEL_HEADLINE = "Tata Steel sees higher steel prices, expects UK business to turn profitable in 2026"
TATA_STEEL_BODY = ("TV Narendran, CEO & MD, Tata Steel, said the company expects its UK business "
                   "to turn profitable in 2026 as steel prices recover globally.")


def test_score_TCS_on_TataSteel_article_is_zero():
    """The exact bug case: TCS being scored as the subject of a Tata Steel
    article must return 0.0 confidence."""
    c, ev = score(
        "TCS",
        TATA_STEEL_HEADLINE + ". " + TATA_STEEL_BODY,
        title=TATA_STEEL_HEADLINE,
        companies_map=COMPANIES,
    )
    assert c == 0.0
    assert "no_mentions" in ev["reasons"]


def test_score_TATASTEEL_on_TataSteel_article_is_high():
    """The correct subject must score high (title hit + lead hit + multiple
    mentions)."""
    c, ev = score(
        "TATASTEEL",
        TATA_STEEL_HEADLINE + ". " + TATA_STEEL_BODY,
        title=TATA_STEEL_HEADLINE,
        companies_map=COMPANIES,
    )
    assert c >= 0.6
    assert ev.get("title_hit") is True


def test_filter_candidates_drops_TCS_keeps_TATASTEEL():
    """When both tickers are extracted as candidates (the upstream Pass-2
    bug), the scorer must drop TCS and keep TATASTEEL."""
    kept = filter_candidates(
        ["TCS", "TATASTEEL"],
        TATA_STEEL_HEADLINE + ". " + TATA_STEEL_BODY,
        title=TATA_STEEL_HEADLINE,
        min_conf=0.30,
        companies_map=COMPANIES,
    )
    tickers = [t for t, c, ev in kept]
    assert "TATASTEEL" in tickers
    assert "TCS" not in tickers


def test_score_BAJFINANCE_on_BajajAuto_article_is_zero():
    """Same class of bug for Bajaj group."""
    headline = "Bajaj Auto reports record Q4 sales, raises FY27 export guidance"
    c, ev = score("BAJFINANCE", headline, title=headline, companies_map=COMPANIES)
    assert c == 0.0


def test_score_ADANIENT_on_AdaniPorts_article_is_zero():
    """And for Adani group."""
    headline = "Adani Ports announces record cargo throughput for FY26"
    c, ev = score("ADANIENT", headline, title=headline, companies_map=COMPANIES)
    assert c == 0.0


def test_score_unique_brand_unchanged():
    """Regression guard: unambiguous brands like Infosys still score normally
    when mentioned in their own article."""
    headline = "Infosys wins multi-year cloud transformation deal worth $200m"
    c, ev = score("INFY", headline + ". " + headline, title=headline, companies_map=COMPANIES)
    assert c >= 0.45


# ---- universe-level: verify the fix in stock_universe.py -----------------

def test_universe_short_names_no_bare_brand_prefixes():
    """stock_universe.UNIVERSE_SHORT_NAMES must not map a bare brand prefix
    (tata/bajaj/adani/hdfc/reliance/godrej/mahindra/birla/aditya) to a
    single ticker — that was the original bug."""
    from stock_universe import UNIVERSE_SHORT_NAMES
    for prefix in ("tata", "bajaj", "adani", "hdfc", "reliance",
                   "godrej", "mahindra", "birla", "aditya"):
        assert prefix not in UNIVERSE_SHORT_NAMES, (
            f"UNIVERSE_SHORT_NAMES[{prefix!r}] = {UNIVERSE_SHORT_NAMES[prefix]!r} "
            f"— bare brand prefix must use bigram form, e.g. {prefix!r} 'consultancy'."
        )


def test_universe_short_names_has_bigram_overrides():
    """The replacement bigram overrides must be present."""
    from stock_universe import UNIVERSE_SHORT_NAMES
    assert UNIVERSE_SHORT_NAMES.get("tata consultancy") == "TCS"
    assert UNIVERSE_SHORT_NAMES.get("tata steel") == "TATASTEEL"
    assert UNIVERSE_SHORT_NAMES.get("tata power") == "TATAPOWER"
    assert UNIVERSE_SHORT_NAMES.get("bajaj finance") == "BAJFINANCE"
    assert UNIVERSE_SHORT_NAMES.get("adani enterprises") == "ADANIENT"
    assert UNIVERSE_SHORT_NAMES.get("hdfc bank") == "HDFCBANK"
