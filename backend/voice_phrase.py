"""Compact a verbose Indian filing headline into a short, TTS-friendly phrase.

Designed for the audio-squawk pipeline on app/terminal.html, which speaks each
broadcast via window.speechSynthesis. The browser reads punctuation as pauses,
so the output is shaped as 3–4 short clauses separated by full stops.

Example:
    Input:  "Tata Power Company Limited bags order worth Rs 500,00,00,000
             from Government"
            company="Tata Power Company Limited"
            event_type="order_win"
    Output: "Alert. Tata Power. Order Win. bags order worth 500 Crores
             from Government."

Public API:
    compact(headline, *, company=None, category=None, max_len=120) -> str
    compact_from_event(event_dict, max_len=120) -> str

Pure functions. No I/O, no logging. Safe to call from broadcast paths.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional


# Corporate suffixes — stripped from any token sequence at the head of a
# company name. Order matters: "Private Limited" must match before "Limited".
_CORP_SUFFIX = re.compile(
    r"\s+(?:"
    r"private\s+limited|pvt\.?\s*ltd\.?|"
    r"limited|ltd\.?|"
    r"corporation|corp\.?|"
    r"incorporated|inc\.?|"
    r"company|co\.?|"
    r"enterprises?|industries|industry|holdings?|group"
    r")\b\.?",
    re.IGNORECASE,
)


# Filler phrases common in BSE/NSE filings that add length but no information
# when read aloud. Each tuple is (pattern, replacement).
_FILLERS = [
    (re.compile(r"\bin\s+respect\s+of\b", re.I),                          "for"),
    (re.compile(r"\bwith\s+respect\s+to\b", re.I),                        "for"),
    (re.compile(r"\bpursuant\s+to\s+(?:regulation\s+\d+\s+of\s+)?", re.I), ""),
    (re.compile(r"\bin\s+terms\s+of\b", re.I),                            "under"),
    (re.compile(r"\bsubmission\s+of\b", re.I),                            ""),
    (re.compile(r"\bdisclosure\s+(?:of|under)\s+", re.I),                 ""),
    (re.compile(r"\bintimat(?:ion|ing)\s+(?:of|about|regarding)\s+", re.I), ""),
    (re.compile(r"\binforms?\s+(?:the\s+)?exchange(?:s)?\s+(?:about|of|regarding|on)\s+", re.I), ""),
    (re.compile(r"\bannounc(?:es|ing|ement\s+of)\s+(?:that\s+)?", re.I),  ""),
    (re.compile(r"\bdated\s+\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}\b", re.I),  ""),
    (re.compile(r"\bunder\s+regulation\s+\d+(?:\(\w+\))?\s+of\s+sebi[^,]*regulations?,?\s+\d{4}", re.I), ""),
    # collapse leftover doubled spaces produced by deletions
    (re.compile(r"\s{2,}"), " "),
]


# Money: handle both digit forms and word forms.
#   "Rs 500,00,00,000"         → 500 Crores
#   "Rs. 5 crore" / "₹5 Cr"    → 5 Crores
#   "INR 50 lakh"              → 50 Lakhs
_CURRENCY = r"(?:Rs\.?|INR|₹)"
_NUM = r"(\d{1,3}(?:[,\d]*\d)?(?:\.\d+)?)"

_RE_INR_CRORE = re.compile(rf"{_CURRENCY}?\s*{_NUM}\s*(?:cr\.?|crore[s]?)\b", re.I)
_RE_INR_LAKH  = re.compile(rf"{_CURRENCY}?\s*{_NUM}\s*(?:lac\.?|lakh[s]?)\b", re.I)
_RE_INR_DIGIT = re.compile(rf"{_CURRENCY}\s*{_NUM}\b")


def _fmt(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _parse_num(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _to_indian_unit(n: float) -> str:
    if n >= 1e7:
        return f"{_fmt(n / 1e7)} Crore{'s' if n / 1e7 != 1 else ''}"
    if n >= 1e5:
        return f"{_fmt(n / 1e5)} Lakh{'s' if n / 1e5 != 1 else ''}"
    return f"Rs {_fmt(n)}"


def _normalize_money(text: str) -> str:
    def _crore(m: re.Match) -> str:
        v = _parse_num(m.group(1))
        if v is None:
            return m.group(0)
        return f"{_fmt(v)} Crore{'s' if v != 1 else ''}"

    def _lakh(m: re.Match) -> str:
        v = _parse_num(m.group(1))
        if v is None:
            return m.group(0)
        return f"{_fmt(v)} Lakh{'s' if v != 1 else ''}"

    def _digit(m: re.Match) -> str:
        v = _parse_num(m.group(1))
        if v is None or v < 1000:
            return m.group(0)
        return _to_indian_unit(v)

    text = _RE_INR_CRORE.sub(_crore, text)
    text = _RE_INR_LAKH.sub(_lakh, text)
    text = _RE_INR_DIGIT.sub(_digit, text)
    return text


def _strip_suffix(name: str) -> str:
    if not name:
        return name
    prev = None
    cur = name
    # Iterate — a name may have stacked suffixes ("Tata Power Company Limited")
    while prev != cur:
        prev = cur
        cur = _CORP_SUFFIX.sub("", cur).rstrip(" .,")
    return cur.strip()


def _humanize_event_type(event_type: Optional[str]) -> Optional[str]:
    if not event_type:
        return None
    return " ".join(w.capitalize() for w in re.split(r"[_\s\-]+", str(event_type)) if w)


def _clean_headline(text: str) -> str:
    if not text:
        return ""
    out = _normalize_money(text)
    for pat, repl in _FILLERS:
        out = pat.sub(repl, out)
    out = _CORP_SUFFIX.sub("", out)
    return re.sub(r"\s+", " ", out).strip(" .,;:-")


def compact(
    headline: str,
    *,
    company: Optional[str] = None,
    category: Optional[str] = None,
    max_len: int = 120,
) -> str:
    """Build a TTS-friendly phrase from a headline + optional company/category.

    Format: "Alert. {company}. {category}. {cleaned_tail}."
    Sections without input are omitted. Output is always <= max_len chars
    and ends with a single period for natural prosody.
    """
    if not headline or not isinstance(headline, str):
        return ""

    short_company = _strip_suffix(company) if company else None
    cleaned = _clean_headline(headline)

    # If the headline starts with the company name, drop the prefix from the
    # informative tail so we don't say "Tata Power. Tata Power bags order…".
    tail = cleaned
    if short_company:
        tail = re.sub(
            rf"^\s*{re.escape(short_company)}\b[\s,:-]*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip(" .,;:-")

    parts = ["Alert"]
    if short_company:
        parts.append(short_company)
    if category:
        parts.append(category)
    # Avoid trivial repetition: don't append the tail if it just restates the
    # category (e.g., category="Order Win" and tail="order win").
    if tail and (not category or tail.lower() != category.lower()):
        parts.append(tail)

    phrase = ". ".join(parts).rstrip(" .") + "."

    if len(phrase) > max_len:
        # Trim at the last sentence boundary that still fits, else hard cut.
        trimmed = phrase[: max_len - 1]
        cut = trimmed.rfind(". ")
        if cut >= 12:
            phrase = trimmed[: cut + 1]
        else:
            phrase = trimmed.rstrip(" ,;:-.") + "."

    return phrase


def compact_from_event(event: Dict[str, Any], max_len: int = 120) -> str:
    """Convenience wrapper that pulls fields from a broadcast payload.

    Accepts either a raw-stage event (title/tickers/event_type) or a scored
    event (headline/company/event_type). Returns "" on empty/invalid input.
    """
    if not isinstance(event, dict):
        return ""

    headline = (
        event.get("headline")
        or event.get("title")
        or event.get("summary")
        or ""
    )
    if not headline:
        return ""

    company = event.get("company")
    if not company:
        tickers = event.get("tickers") or []
        if tickers and isinstance(tickers[0], str):
            sym = tickers[0]
            # Strip exchange suffix; TTS reads "RELIANCE.NS" as letters.
            for suf in (".NS", ".BO", ".BSE", ".NSE"):
                if sym.upper().endswith(suf):
                    sym = sym[: -len(suf)]
                    break
            company = sym.title() if sym else None

    category = _humanize_event_type(event.get("event_type"))

    return compact(headline, company=company, category=category, max_len=max_len)
