"""
Smoke-test: every new Phase 1.5 module imports cleanly.

Catches syntax errors, circular imports, and missing optional deps.
Modules that need optional third-party packages (praw, telethon, yfinance)
should import but not execute network code at import time — this test proves
that contract.
"""
import importlib
import pytest

MODULES = [
    "net",
    "ratelimit",
    "metrics",
    "schema_ext",
    "quant_layer",
    "orchestrator_ext",
    "jargon.loader",
    "jargon.aho_corasick",
    "social.common",
    "social.reddit_collector",
    "social.twitter_collector",
    "social.telegram_collector",
    "social.publish_priority",
    "social.handle_scorer",
    "fundamentals.promoter_scraper",
    "fundamentals.promoter_events",
    "fundamentals.bse_scrip_map",
    "forensics.llm_intent",
    "forensics.bse_filing_fetcher",
    "forensics.ir_pdf_fetcher",
    "forensics.sebi_disclosures",
    "forensics.number_matcher",
    "forensics.forensic_scorer",
    "commodities.impact",
    "global_market.overnight_model",
]


@pytest.mark.parametrize("mod", MODULES)
def test_import(mod):
    importlib.import_module(mod)
