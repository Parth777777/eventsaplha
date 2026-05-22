"""Smoke-tests for scraper/entity_linker.py.

Hits the user-reported failure ("Mstar order tagged to BSE") and a battery
of common Indian-news ticker-tagging traps. Run: python tools/test_entity_linker.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))

from entity_linker import (
    score, is_exchange_context, filter_candidates,
    classify_news_type, is_promotable_to_signal,
)

PASS, FAIL = 0, 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  | {detail}")


print("\n=== EXCHANGE-CONTEXT DETECTION ===")
check("'BSE-listed' is exchange context",
      is_exchange_context("BSE", "Mstar wins order from a BSE-listed company"))
check("'on the NSE' is exchange context",
      is_exchange_context("NSE", "Stock is traded on the NSE main board"))
check("'BSE Sensex' is exchange context",
      is_exchange_context("BSE", "BSE Sensex closed flat today"))
check("'NSE filing' is exchange context",
      is_exchange_context("NSE", "Company made an NSE filing yesterday"))
check("'BSE Ltd Q4 result' is NOT exchange context",
      not is_exchange_context("BSE", "BSE Ltd reports Q4 profit jumped 20 percent"))
check("Non-exchange ticker short-circuits",
      not is_exchange_context("TCS", "TCS-listed company won contract"))


print("\n=== USER-REPORTED FAILURE: Mstar/BSE ===")
text = "Mstar Co wins fresh order from a BSE-listed company"
conf, ev = score("BSE", text, title=text[:120])
check(f"BSE in 'wins order from BSE-listed' is rejected (got conf={conf})",
      conf == 0.0,
      detail=f"reasons={ev.get('reasons')}")


print("\n=== TITLE-PROMOTION CASES ===")
text = "Reliance Industries Q4 Results: Profit jumps 12% YoY"
conf, ev = score("RELIANCE", text, title="Reliance Industries Q4 Results: Profit jumps 12% YoY",
                 short_names_map={"Reliance Industries": "RELIANCE"})
check(f"Reliance in title = high confidence (got {conf})",
      conf >= 0.6,
      detail=f"reasons={ev.get('reasons')}")


print("\n=== AGENT-ROLE REJECTION ===")
text = ("TCS quarterly result was audited by Deloitte. The IT major posted "
        "a 9% growth. Deloitte's audit fees were not disclosed.")
conf, ev = score("DELOITTE", text, title="TCS Q4 audited results", )
check(f"Deloitte as auditor = low confidence (got {conf})",
      conf < 0.5,
      detail=f"reasons={ev.get('reasons')}")


print("\n=== LEAD-ONLY MENTION (not in title) ===")
text = "Cement demand rises this quarter. UltraTech Cement reported strong dispatches in Q4."
conf, ev = score("ULTRACEMCO", text, title="Cement demand rises this quarter",
                 short_names_map={"UltraTech Cement": "ULTRACEMCO"})
check(f"UltraTech mentioned in lead = mid confidence (got {conf})",
      0.2 <= conf <= 0.7,
      detail=f"reasons={ev.get('reasons')}")


print("\n=== FILTER_CANDIDATES ===")
text = "Mstar wins order from BSE-listed company. Mstar shares rose 3%."
kept = filter_candidates(["MSTAR", "BSE"], text, title=text[:80], min_conf=0.45,
                         short_names_map={"Mstar": "MSTAR"})
kept_tickers = [t for t, c, e in kept]
check(f"BSE dropped, MSTAR kept (got {kept_tickers})",
      "BSE" not in kept_tickers and "MSTAR" in kept_tickers,
      detail=f"full={kept}")


print("\n=== NEWS-TYPE CLASSIFICATION ===")
check("BSE link -> filing",
      classify_news_type("BSE", "https://bseindia.com/xml-data/corpfiling/foo.xml") == "filing")
check("NSE link -> filing",
      classify_news_type("NSE", "https://nseindia.com/announcements/12345") == "filing")
check("Reddit -> social_buzz",
      classify_news_type("Reddit", "https://reddit.com/r/IndiaInvestments/x") == "social_buzz")
check("ET -> news_article (fallback)",
      classify_news_type("Economic Times", "https://economictimes.com/...") == "news_article")
check("Concall transcript link -> concall",
      classify_news_type("Trendlyne", "https://trendlyne.com/concall/transcript-XYZ") == "concall")


print("\n=== PROMOTABILITY GATE ===")
check("Filing + 0.4 conf -> promotable",
      is_promotable_to_signal("filing", 0.4))
check("News article + 0.5 conf -> NOT promotable",
      not is_promotable_to_signal("news_article", 0.5))
check("News article + 0.65 conf -> promotable",
      is_promotable_to_signal("news_article", 0.65))
check("Social buzz -> never promotable",
      not is_promotable_to_signal("social_buzz", 1.0))


print(f"\n=== RESULTS: {PASS} passed, {FAIL} failed ===\n")
sys.exit(0 if FAIL == 0 else 1)
