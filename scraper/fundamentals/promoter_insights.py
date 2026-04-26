"""
Promoter insights — derived intelligence on top of raw shareholding data.

Produces:
  - current snapshot (latest quarter)
  - 8-quarter history
  - trend flags with human-readable explanations
  - red flags (severity: info | warn | critical)
  - key promoters + recent SAST/PIT events
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _placeholder(db) -> str:
    return "%s" if getattr(db, "is_postgres", False) else "?"


def _rows(cursor) -> List[Dict]:
    out: List[Dict] = []
    rows = cursor.fetchall()
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
        else:
            try:
                out.append(dict(r))
            except Exception:
                out.append({})
    return out


def _quarters(db, ticker: str, limit: int = 8) -> List[Dict]:
    p = _placeholder(db)
    cursor = db.conn.cursor()
    cursor.execute(
        f"""SELECT quarter_end, promoter_pct, promoter_pledge_pct, fii_pct, dii_pct,
                   public_pct, mutual_fund_pct, insurance_pct
              FROM promoter_holdings
             WHERE ticker = {p}
             ORDER BY quarter_end DESC LIMIT {p}""",
        (ticker.upper(), limit),
    )
    out: List[Dict] = []
    for r in cursor.fetchall():
        d = r if isinstance(r, dict) else dict(zip([c[0] for c in cursor.description], r))
        # normalise quarter_end to iso string
        qe = d.get("quarter_end")
        d["quarter_end"] = str(qe)[:10] if qe is not None else None
        out.append(d)
    # Return oldest-first for display
    out.reverse()
    return out


def _recent_events(db, ticker: str, limit: int = 20) -> List[Dict]:
    p = _placeholder(db)
    cursor = db.conn.cursor()
    cursor.execute(
        f"""SELECT event_date, event_type, person_name, quantity, pct_before, pct_after,
                   reason, link
              FROM promoter_events
             WHERE ticker = {p}
             ORDER BY event_date DESC LIMIT {p}""",
        (ticker.upper(), limit),
    )
    out: List[Dict] = []
    for r in cursor.fetchall():
        d = r if isinstance(r, dict) else dict(zip([c[0] for c in cursor.description], r))
        d["event_date"] = str(d.get("event_date"))[:10] if d.get("event_date") else None
        out.append(d)
    return out


def _key_persons(db, ticker: str) -> List[Dict]:
    p = _placeholder(db)
    cursor = db.conn.cursor()
    cursor.execute(
        f"""SELECT person_name, role, din, other_directorships
              FROM promoters
             WHERE ticker = {p}
             ORDER BY person_name LIMIT 20""",
        (ticker.upper(),),
    )
    out: List[Dict] = []
    for r in cursor.fetchall():
        d = r if isinstance(r, dict) else dict(zip([c[0] for c in cursor.description], r))
        out.append(d)
    return out


def _recent_sebi(db, ticker: str, limit: int = 10) -> List[Dict]:
    p = _placeholder(db)
    cursor = db.conn.cursor()
    cursor.execute(
        f"""SELECT transaction_date, disclosure_type, person_name, transaction_type,
                   quantity, pct_before, pct_after, link
              FROM sebi_disclosures
             WHERE ticker = {p}
             ORDER BY transaction_date DESC LIMIT {p}""",
        (ticker.upper(), limit),
    )
    out: List[Dict] = []
    for r in cursor.fetchall():
        d = r if isinstance(r, dict) else dict(zip([c[0] for c in cursor.description], r))
        d["transaction_date"] = str(d.get("transaction_date"))[:10] if d.get("transaction_date") else None
        out.append(d)
    return out


def _red_flags(quarters: List[Dict], events: List[Dict], sebi: List[Dict]) -> List[Dict]:
    flags: List[Dict] = []
    if len(quarters) >= 2:
        latest, prev = quarters[-1], quarters[-2]
        d_pledge = (latest.get("promoter_pledge_pct") or 0) - (prev.get("promoter_pledge_pct") or 0)
        d_prom = (latest.get("promoter_pct") or 0) - (prev.get("promoter_pct") or 0)
        d_fii = (latest.get("fii_pct") or 0) - (prev.get("fii_pct") or 0)

        if d_pledge >= 5:
            flags.append({
                "severity": "critical",
                "code": "pledge_surge",
                "label": "Pledge surged",
                "detail": f"Promoter pledge rose {d_pledge:.1f} pp QoQ — typical pump-dump precursor.",
            })
        elif d_pledge >= 2:
            flags.append({
                "severity": "warn",
                "code": "pledge_rising",
                "label": "Pledge rising",
                "detail": f"Promoter pledge up {d_pledge:.1f} pp QoQ.",
            })

        if d_prom <= -2:
            flags.append({
                "severity": "critical",
                "code": "promoter_dilution",
                "label": "Promoter dilution",
                "detail": f"Promoter stake fell {abs(d_prom):.1f} pp QoQ.",
            })
        elif d_prom >= 0.5:
            flags.append({
                "severity": "info",
                "code": "promoter_increase",
                "label": "Promoter increased stake",
                "detail": f"Promoter stake rose {d_prom:.1f} pp QoQ — conviction signal.",
            })

        if d_fii <= -3:
            flags.append({
                "severity": "warn",
                "code": "fii_exit",
                "label": "FII exit",
                "detail": f"FII holding dropped {abs(d_fii):.1f} pp QoQ.",
            })
        elif d_fii >= 3:
            flags.append({
                "severity": "info",
                "code": "fii_inflow",
                "label": "FII inflow",
                "detail": f"FII holding rose {d_fii:.1f} pp QoQ.",
            })

        if (latest.get("promoter_pledge_pct") or 0) >= 25:
            flags.append({
                "severity": "critical",
                "code": "high_pledge",
                "label": "High pledge level",
                "detail": f"{latest.get('promoter_pledge_pct'):.1f}% of promoter holding is pledged.",
            })

    # Recent insider-sell streak
    sells = [s for s in sebi if s.get("transaction_type") == "sell"]
    if len(sells) >= 3:
        flags.append({
            "severity": "warn",
            "code": "insider_selling",
            "label": "Insider selling streak",
            "detail": f"{len(sells)} insider sell disclosures in recent window.",
        })
    elif len(sells) >= 1:
        flags.append({
            "severity": "info",
            "code": "insider_sell",
            "label": "Recent insider sale",
            "detail": f"{len(sells)} insider sale disclosed recently.",
        })

    # Recent acquire streak
    buys = [s for s in sebi if s.get("transaction_type") == "buy"]
    if len(buys) >= 2:
        flags.append({
            "severity": "info",
            "code": "insider_buying",
            "label": "Insider buying",
            "detail": f"{len(buys)} insider buy disclosures — bullish conviction.",
        })

    return flags


def compute(db, ticker: str) -> Dict[str, Any]:
    ticker = ticker.upper()
    quarters = _quarters(db, ticker, limit=12)
    events = _recent_events(db, ticker, limit=30)
    persons = _key_persons(db, ticker)
    sebi = _recent_sebi(db, ticker, limit=20)

    current = quarters[-1] if quarters else None
    summary: Dict[str, Any] = {
        "ticker": ticker,
        "has_data": bool(quarters),
        "current": current,
        "quarters": quarters,
        "events": events,
        "sebi_disclosures": sebi,
        "key_persons": persons,
        "red_flags": _red_flags(quarters, events, sebi),
    }

    # Insight lines for the narrative section
    insights: List[str] = []
    if current:
        pr = current.get("promoter_pct") or 0
        pl = current.get("promoter_pledge_pct") or 0
        fi = current.get("fii_pct") or 0
        di = current.get("dii_pct") or 0
        insights.append(f"Promoter holding: {pr:.1f}% (pledge {pl:.1f}%).")
        insights.append(f"FII: {fi:.1f}% · DII: {di:.1f}%.")
        if len(quarters) >= 4:
            first = quarters[0]
            d1 = pr - (first.get("promoter_pct") or 0)
            arrow = "up" if d1 > 0 else "down" if d1 < 0 else "flat"
            insights.append(f"4-quarter change: promoter {arrow} {abs(d1):.1f}pp from {first.get('quarter_end')}.")
    if events:
        last = events[0]
        insights.append(f"Last promoter event ({last.get('event_date')}): {last.get('event_type')}.")
    if persons:
        insights.append(f"{len(persons)} key promoter(s) on record.")

    summary["insights"] = insights
    return summary
