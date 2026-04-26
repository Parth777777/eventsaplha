"""
Article-number vs filing-number cross-check.

For every article classified as earnings/order_win, we extract numeric claims
and match them against the most recent relevant filing for the same ticker.
Writes discrepancies into article_discrepancies and returns a per-event flag.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from forensics.bse_filing_fetcher import extract_numbers_from_text

logger = logging.getLogger(__name__)


DEFAULT_TOLERANCE = {
    "revenue": 5.0,           # 5 % tolerance on revenue
    "net_profit": 5.0,
    "operating_profit": 5.0,
    "order_value": 7.5,
    "eps": 5.0,
    "margin": 2.0,            # margin quoted in absolute percentage points
}


def _match_within_tolerance(article_val: float, filing_val: float, field: str) -> Tuple[float, float, bool]:
    tol_pct = DEFAULT_TOLERANCE.get(field, 5.0)
    if field == "margin":
        diff = abs(article_val - filing_val)
        return diff, tol_pct, diff > tol_pct
    diff_pct = abs(article_val - filing_val) / max(abs(filing_val), 1e-9) * 100.0
    return diff_pct, tol_pct, diff_pct > tol_pct


class NumberMatcher:
    def __init__(self, db):
        self.db = db

    def _placeholder(self) -> str:
        return "%s" if getattr(self.db, "is_postgres", False) else "?"

    def _recent_filing_numbers(self, ticker: str, event_dt: Optional[datetime],
                                window_days: int = 2) -> List[Dict]:
        """Return filing_numbers rows for filings within ±window_days of event_dt."""
        p = self._placeholder()
        event_dt = event_dt or datetime.utcnow()
        lo = event_dt - timedelta(days=window_days)
        hi = event_dt + timedelta(days=window_days)
        try:
            cursor = self.db.conn.cursor()
            if self.db.is_postgres:
                cursor.execute(
                    """SELECT fn.field_name, fn.value, fn.unit, f.id as filing_id
                         FROM filings f
                         JOIN filing_numbers fn ON fn.filing_id = f.id
                        WHERE f.ticker = %s AND f.filed_at BETWEEN %s AND %s""",
                    (ticker, lo, hi),
                )
            else:
                cursor.execute(
                    f"""SELECT fn.field_name, fn.value, fn.unit, f.id as filing_id
                         FROM filings f
                         JOIN filing_numbers fn ON fn.filing_id = f.id
                        WHERE f.ticker = {p} AND f.filed_at BETWEEN {p} AND {p}""",
                    (ticker, lo.isoformat(), hi.isoformat()),
                )
            rows = cursor.fetchall()
            out = []
            for r in rows:
                if isinstance(r, dict):
                    out.append(r)
                else:
                    out.append({"field_name": r[0], "value": r[1], "unit": r[2], "filing_id": r[3]})
            return out
        except Exception as exc:
            logger.debug("recent filing numbers query failed: %s", exc)
            return []

    def check_article(self, event_id: str, ticker: str, article_text: str,
                      event_dt: Optional[datetime] = None) -> Dict:
        """Run the cross-check and persist any discrepancies.

        Returns a dict with:
          flagged: bool,
          discrepancies: [{field, article_value, filing_value, diff_pct, tolerance_pct}, ...]
        """
        article_numbers = extract_numbers_from_text(article_text or "")
        if not article_numbers:
            return {"flagged": False, "discrepancies": [], "reason": "no_numbers"}
        filing_numbers = self._recent_filing_numbers(ticker, event_dt)
        if not filing_numbers:
            return {"flagged": False, "discrepancies": [], "reason": "no_filing"}

        # Group filing numbers by field — take median to handle multi-extractions
        by_field: Dict[str, List[Dict]] = {}
        for fn in filing_numbers:
            by_field.setdefault(fn["field_name"], []).append(fn)

        discrepancies: List[Dict] = []
        p = self._placeholder()
        for anum in article_numbers:
            fld = anum["field_name"]
            candidates = by_field.get(fld)
            if not candidates:
                continue
            vals = sorted(c["value"] for c in candidates)
            filing_val = vals[len(vals) // 2]
            filing_id = candidates[0]["filing_id"]
            diff, tol, flagged = _match_within_tolerance(anum["value"], filing_val, fld)
            rec = {
                "field": fld,
                "article_value": anum["value"],
                "filing_value": filing_val,
                "diff_pct": round(diff, 2),
                "tolerance_pct": tol,
                "flagged": flagged,
            }
            discrepancies.append(rec)
            try:
                if self.db.is_postgres:
                    self.db.conn.cursor().execute(
                        """INSERT INTO article_discrepancies
                             (event_id, filing_id, field_name, article_value, filing_value,
                              diff_pct, tolerance_pct, flagged)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (event_id, filing_id, fld, anum["value"], filing_val, diff, tol, flagged),
                    )
                else:
                    self.db.conn.execute(
                        f"""INSERT INTO article_discrepancies
                             (event_id, filing_id, field_name, article_value, filing_value,
                              diff_pct, tolerance_pct, flagged)
                             VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})""",
                        (event_id, filing_id, fld, anum["value"], filing_val, diff, tol, 1 if flagged else 0),
                    )
                self.db.conn.commit()
            except Exception as exc:
                logger.debug("discrepancy insert failed: %s", exc)
                self.db.conn.rollback()

        any_flag = any(d["flagged"] for d in discrepancies)
        return {"flagged": any_flag, "discrepancies": discrepancies, "reason": "checked"}
