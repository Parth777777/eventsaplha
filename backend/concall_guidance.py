"""Concall guidance tone scanner — rule-based regex pass.

Scans concall_paragraphs for forward-looking management language and assigns
a tone score in [-1, +1] per filing. Powers feature #7 of the earnings
forecast model (concall_guidance_tone).

Two passes:
  - Bull lexicon: "we expect", "outlook strong", "double-digit growth",
    "guidance raised/upgraded", "FY growth/expansion"
  - Bear lexicon: "headwind", "challenging", "softness", "weakness",
    "guidance cut/reduced/withdrawn", "cautious outlook"

A single hedge phrase doesn't move the needle — require >=2 matches per
filing to commit a non-zero score. Caching: ttl_cache 1h per (ticker,
filing_id).

This is the regex (cheap, deterministic) tier. An LLM tier-2 pass lives in
`backend/forecast_llm.py` and runs async on the top-30% by regex score.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# --- Lexicons -------------------------------------------------------------
# Each pattern is (regex, weight, label). Weight gets summed (signed) and
# normalised by total matches at the end. Patterns chosen to be specific
# enough that bond/disclaimer language doesn't false-fire.

BULL_PATTERNS: List[Tuple[re.Pattern, float, str]] = [
    (re.compile(r"\bwe (?:expect|anticipate|believe|are confident)\b", re.I), 1.0, "we_expect"),
    (re.compile(r"\b(?:outlook|guidance) (?:remains|is) (?:strong|positive|robust|healthy)\b", re.I), 1.5, "outlook_strong"),
    (re.compile(r"\b(?:double[\s-]?digit|strong|healthy|robust)\s+growth\b", re.I), 1.2, "growth_lang"),
    (re.compile(r"\bguidance\s+(?:raise(?:d)?|upgraded|revised\s+upward|revised\s+up)\b", re.I), 2.0, "guidance_raised"),
    (re.compile(r"\b(?:FY|H[12]|Q[1-4])\s*\d{2,4}\b.{0,40}\b(?:growth|expansion|momentum|pickup)\b", re.I), 1.0, "fy_growth"),
    (re.compile(r"\b(?:order\s+book|pipeline)\s+(?:remains|is|continues)\s+(?:strong|robust|healthy|record|all-?time\s+high)\b", re.I), 1.3, "orderbook_strong"),
    (re.compile(r"\bmargin\s+(?:expansion|improvement|tailwind)\b", re.I), 1.2, "margin_expansion"),
    (re.compile(r"\b(?:demand|volume)\s+(?:trajectory|momentum)\s+(?:remains|is)\s+(?:strong|positive|encouraging)\b", re.I), 1.0, "demand_strong"),
    (re.compile(r"\b(?:capex|capacity\s+expansion|new\s+facility|greenfield)\s+(?:on\s+track|commissioned|completed|underway)\b", re.I), 0.8, "capex_progress"),
    (re.compile(r"\bachieve(?:d|ing)?\s+(?:targets?|guidance|consensus)\b", re.I), 0.7, "on_target"),
]

BEAR_PATTERNS: List[Tuple[re.Pattern, float, str]] = [
    (re.compile(r"\b(?:headwind|softness|weakness)\b", re.I), 1.0, "headwind"),
    (re.compile(r"\b(?:challenging|difficult|tough)\s+(?:environment|quarter|year|outlook|conditions?)\b", re.I), 1.2, "challenging"),
    (re.compile(r"\bguidance\s+(?:cut|reduced?|withdrawn|lowered|revised\s+downward|revised\s+down)\b", re.I), 2.0, "guidance_cut"),
    (re.compile(r"\b(?:cautious|conservative|measured)\s+outlook\b", re.I), 1.3, "cautious_outlook"),
    (re.compile(r"\bmargin\s+(?:compression|pressure|contraction|erosion)\b", re.I), 1.2, "margin_compression"),
    (re.compile(r"\bdemand\s+(?:weakness|softness|slowdown|moderation|tepid)\b", re.I), 1.1, "demand_weak"),
    (re.compile(r"\binput\s+cost\s+(?:pressure|inflation|escalation)\b", re.I), 1.0, "input_pressure"),
    (re.compile(r"\b(?:miss(?:ed)?|below)\s+(?:consensus|guidance|expectations?)\b", re.I), 1.5, "missed"),
    (re.compile(r"\b(?:we\s+expect|likely)\s+.{0,30}\b(?:decline|fall|drop|contract)\b", re.I), 1.2, "decline_expected"),
    (re.compile(r"\b(?:supply|inventory|destocking)\s+(?:disruption|issue|overhang|correction)\b", re.I), 0.9, "supply_issue"),
]


# --- Output dataclass -----------------------------------------------------

@dataclass
class GuidanceMatch:
    label: str
    weight: float
    snippet: str  # 60-char window around the match for QA / display


@dataclass
class GuidanceScore:
    ticker: Optional[str]
    filing_id: Optional[int]
    filed_at: Optional[str]
    tone_score: float          # in [-1, +1]; 0 = no signal
    bull_matches: int
    bear_matches: int
    total_matches: int
    matches: List[GuidanceMatch] = field(default_factory=list)
    paragraphs_scanned: int = 0
    insufficient: bool = False  # True if total_matches < 2 (then tone_score is 0)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "filing_id": self.filing_id,
            "filed_at": self.filed_at,
            "tone_score": round(self.tone_score, 3),
            "bull_matches": self.bull_matches,
            "bear_matches": self.bear_matches,
            "total_matches": self.total_matches,
            "paragraphs_scanned": self.paragraphs_scanned,
            "insufficient": self.insufficient,
            "matches": [
                {"label": m.label, "weight": m.weight, "snippet": m.snippet}
                for m in self.matches[:20]  # cap so the payload stays small
            ],
        }


# --- Core scan ------------------------------------------------------------

def _scan_text(text: str) -> Tuple[List[GuidanceMatch], List[GuidanceMatch]]:
    """Return (bull_matches, bear_matches) found in a single string."""
    bull, bear = [], []
    if not text:
        return bull, bear
    for pat, w, label in BULL_PATTERNS:
        for m in pat.finditer(text):
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            snippet = text[start:end].replace("\n", " ").strip()
            bull.append(GuidanceMatch(label=label, weight=w, snippet=snippet))
    for pat, w, label in BEAR_PATTERNS:
        for m in pat.finditer(text):
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            snippet = text[start:end].replace("\n", " ").strip()
            bear.append(GuidanceMatch(label=label, weight=w, snippet=snippet))
    return bull, bear


def score_text(text: str, ticker: Optional[str] = None) -> GuidanceScore:
    """Score a single text blob (typically one concall transcript stitched together)."""
    bull, bear = _scan_text(text)
    bull_w = sum(m.weight for m in bull)
    bear_w = sum(m.weight for m in bear)
    total = len(bull) + len(bear)
    if total < 2:
        # Insufficient signal — emit zero (don't commit a tone on a single hedge phrase).
        return GuidanceScore(
            ticker=ticker, filing_id=None, filed_at=None,
            tone_score=0.0, bull_matches=len(bull), bear_matches=len(bear),
            total_matches=total, matches=bull + bear, paragraphs_scanned=1,
            insufficient=True,
        )
    # Normalise to [-1, +1]: (bull - bear) / (bull + bear).
    raw = (bull_w - bear_w) / max(bull_w + bear_w, 0.001)
    return GuidanceScore(
        ticker=ticker, filing_id=None, filed_at=None,
        tone_score=max(-1.0, min(1.0, raw)),
        bull_matches=len(bull), bear_matches=len(bear),
        total_matches=total,
        matches=bull + bear,
        paragraphs_scanned=1,
        insufficient=False,
    )


def score_filing(db, filing_id: int) -> GuidanceScore:
    """Score one filing by reading its concall_paragraphs rows."""
    if not filing_id:
        return GuidanceScore(ticker=None, filing_id=filing_id, filed_at=None,
                             tone_score=0.0, bull_matches=0, bear_matches=0,
                             total_matches=0, insufficient=True)
    cur = db.conn.cursor()
    is_pg = getattr(db, "is_postgres", False)
    sql = ("SELECT ticker, filed_at, text FROM concall_paragraphs "
           "WHERE filing_id = ? ORDER BY paragraph_idx")
    if is_pg:
        sql = sql.replace("?", "%s")
    cur.execute(sql, (filing_id,))
    rows = cur.fetchall()
    if not rows:
        return GuidanceScore(ticker=None, filing_id=filing_id, filed_at=None,
                             tone_score=0.0, bull_matches=0, bear_matches=0,
                             total_matches=0, insufficient=True)

    ticker = rows[0][0]
    filed_at = str(rows[0][1]) if rows[0][1] else None
    all_bull: List[GuidanceMatch] = []
    all_bear: List[GuidanceMatch] = []
    paragraphs = 0
    for _, _, text in rows:
        if not text:
            continue
        paragraphs += 1
        b, br = _scan_text(text)
        all_bull.extend(b)
        all_bear.extend(br)

    bull_w = sum(m.weight for m in all_bull)
    bear_w = sum(m.weight for m in all_bear)
    total = len(all_bull) + len(all_bear)
    if total < 2:
        return GuidanceScore(
            ticker=ticker, filing_id=filing_id, filed_at=filed_at,
            tone_score=0.0, bull_matches=len(all_bull), bear_matches=len(all_bear),
            total_matches=total, matches=all_bull + all_bear,
            paragraphs_scanned=paragraphs, insufficient=True,
        )
    raw = (bull_w - bear_w) / max(bull_w + bear_w, 0.001)
    return GuidanceScore(
        ticker=ticker, filing_id=filing_id, filed_at=filed_at,
        tone_score=max(-1.0, min(1.0, raw)),
        bull_matches=len(all_bull), bear_matches=len(all_bear),
        total_matches=total,
        matches=all_bull + all_bear,
        paragraphs_scanned=paragraphs,
        insufficient=False,
    )


def latest_for_ticker(db, ticker: str, since: Optional[str] = None) -> Optional[GuidanceScore]:
    """Return the GuidanceScore for the most recent concall filing for this ticker.

    Useful as a feature input to the earnings forecast model: "what did
    management say on the last call?".
    """
    if not ticker:
        return None
    cur = db.conn.cursor()
    is_pg = getattr(db, "is_postgres", False)
    sql = ("SELECT DISTINCT filing_id FROM concall_paragraphs "
           "WHERE UPPER(ticker) = ? ")
    params: List = [ticker.upper()]
    if since:
        sql += "AND filed_at >= ? "
        params.append(since)
    sql += "ORDER BY MAX(filed_at) OVER (PARTITION BY filing_id) DESC " if is_pg else ""
    sql += "LIMIT 1"
    # SQLite doesn't support window in this position; use a simpler query
    if not is_pg:
        sql = ("SELECT filing_id FROM concall_paragraphs "
               "WHERE UPPER(ticker) = ? ")
        params = [ticker.upper()]
        if since:
            sql += "AND filed_at >= ? "
            params.append(since)
        sql += "ORDER BY filed_at DESC LIMIT 1"
    if is_pg:
        sql = sql.replace("?", "%s")
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return None
    return score_filing(db, row[0])


# --- CLI smoke test -------------------------------------------------------

def main():
    import argparse, os, sys, json
    p = argparse.ArgumentParser(description="Score concall transcripts for forward-looking tone")
    p.add_argument("--ticker", help="Score the most recent concall for this ticker")
    p.add_argument("--filing-id", type=int, help="Score a specific filing_id")
    p.add_argument("--sample", action="store_true", help="Score 10 random concall filings as a smoke test")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scraper'))
    from database_schema import TickwaveDB  # type: ignore
    db = TickwaveDB()
    db.init_schema()

    if args.filing_id:
        s = score_filing(db, args.filing_id)
        print(json.dumps(s.to_dict(), indent=2))
        return
    if args.ticker:
        s = latest_for_ticker(db, args.ticker)
        if not s:
            print(f"No concall found for {args.ticker}")
            return
        print(json.dumps(s.to_dict(), indent=2))
        return
    if args.sample:
        cur = db.conn.cursor()
        cur.execute("SELECT DISTINCT filing_id FROM concall_paragraphs ORDER BY RANDOM() LIMIT 10")
        for (fid,) in cur.fetchall():
            s = score_filing(db, fid)
            print(f"filing={fid} ticker={s.ticker} tone={s.tone_score:+.2f} "
                  f"(bull={s.bull_matches} bear={s.bear_matches} para={s.paragraphs_scanned} "
                  f"insufficient={s.insufficient})")
        return
    p.print_help()


if __name__ == "__main__":
    main()
