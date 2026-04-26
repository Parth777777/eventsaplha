"""
Glossary loader — reads glossary.json + glossary_cache table + optional LLM fallback.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from jargon.aho_corasick import AhoCorasick

logger = logging.getLogger(__name__)

GLOSSARY_PATH = Path(__file__).parent / "glossary.json"


class Glossary:
    """In-memory glossary with reload support.

    Terms + aliases are indexed by lower-cased surface form pointing at the
    canonical entry. AhoCorasick matcher is rebuilt on reload.
    """

    def __init__(self):
        self._entries: List[Dict[str, Any]] = []
        self._lookup: Dict[str, Dict[str, Any]] = {}
        self._matcher: Optional[AhoCorasick] = None
        self._lock = threading.RLock()
        self._mtime: float = 0.0
        self._db = None
        self.reload()

    def attach_db(self, db) -> None:
        """Optional: attach TickwaveDB so unknown-term hits can be cached."""
        self._db = db

    def reload(self) -> None:
        with self._lock:
            entries: List[Dict[str, Any]] = []
            if GLOSSARY_PATH.exists():
                try:
                    entries = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
                    self._mtime = GLOSSARY_PATH.stat().st_mtime
                except Exception as exc:
                    logger.error("glossary load failed: %s", exc)
                    entries = []
            self._entries = entries
            lookup: Dict[str, Dict[str, Any]] = {}
            patterns: List[str] = []
            for e in entries:
                term = e.get("term")
                if not term:
                    continue
                lookup[term.lower()] = e
                patterns.append(term)
                for alias in e.get("aliases", []) or []:
                    lookup[alias.lower()] = e
                    patterns.append(alias)
            self._lookup = lookup
            self._matcher = AhoCorasick(patterns)

    def maybe_reload(self) -> None:
        try:
            m = GLOSSARY_PATH.stat().st_mtime
            if m != self._mtime:
                self.reload()
        except FileNotFoundError:
            pass

    def lookup(self, term: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._lookup.get(term.lower())
            if entry:
                return entry
        # fallback: db cache
        if self._db is not None:
            try:
                placeholder = "%s" if getattr(self._db, "is_postgres", False) else "?"
                cursor = self._db.conn.cursor()
                cursor.execute(
                    f"SELECT plain_english, example, verified_by FROM glossary_cache WHERE term = {placeholder}",
                    (term.lower(),),
                )
                row = cursor.fetchone()
                if row:
                    pe = row[0] if not isinstance(row, dict) else row.get("plain_english")
                    ex = row[1] if not isinstance(row, dict) else row.get("example")
                    vb = row[2] if not isinstance(row, dict) else row.get("verified_by")
                    # bump hit count
                    cursor.execute(
                        f"UPDATE glossary_cache SET hit_count = hit_count + 1 WHERE term = {placeholder}",
                        (term.lower(),),
                    )
                    self._db.conn.commit()
                    return {
                        "term": term,
                        "plain_english": pe,
                        "one_line_example": ex,
                        "registers": ["auto"],
                        "verified_by": vb or "llm",
                    }
            except Exception as exc:
                logger.debug("glossary cache lookup failed: %s", exc)
        return None

    def cache_put(self, term: str, plain_english: str, example: str = "") -> None:
        if self._db is None:
            return
        try:
            is_pg = getattr(self._db, "is_postgres", False)
            placeholder = "%s" if is_pg else "?"
            if is_pg:
                self._db.conn.cursor().execute(
                    f"""INSERT INTO glossary_cache (term, plain_english, example, verified_by, hit_count)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, 'llm', 1)
                        ON CONFLICT (term) DO UPDATE SET plain_english = EXCLUDED.plain_english""",
                    (term.lower(), plain_english, example),
                )
            else:
                self._db.conn.execute(
                    f"""INSERT OR REPLACE INTO glossary_cache (term, plain_english, example, verified_by, hit_count)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, 'llm', 1)""",
                    (term.lower(), plain_english, example),
                )
            self._db.conn.commit()
        except Exception as exc:
            logger.warning("glossary cache put failed: %s", exc)

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        q = (query or "").lower().strip()
        if not q:
            return []
        hits: List[Dict[str, Any]] = []
        with self._lock:
            for e in self._entries:
                surfaces = [e.get("term", "").lower()] + [a.lower() for a in e.get("aliases", []) or []]
                if any(q in s for s in surfaces) or q in e.get("plain_english", "").lower():
                    hits.append(e)
                    if len(hits) >= limit:
                        break
        return hits

    def annotate(self, text: str) -> List[Dict[str, Any]]:
        """Return non-overlapping (start, end, term, entry) matches for the glossary."""
        self.maybe_reload()
        with self._lock:
            matcher = self._matcher
            lookup = self._lookup
        if matcher is None or not text:
            return []
        out: List[Dict[str, Any]] = []
        for start, end, pat in matcher.find(text):
            entry = lookup.get(pat.lower())
            if entry is None:
                continue
            out.append({
                "start": start,
                "end": end,
                "surface": text[start:end],
                "term": entry.get("term"),
                "plain_english": entry.get("plain_english"),
            })
        return out

    def all_terms(self) -> List[str]:
        with self._lock:
            return [e.get("term", "") for e in self._entries if e.get("term")]


_GLOSSARY: Optional[Glossary] = None
_INIT_LOCK = threading.Lock()


def get_glossary() -> Glossary:
    global _GLOSSARY
    if _GLOSSARY is None:
        with _INIT_LOCK:
            if _GLOSSARY is None:
                _GLOSSARY = Glossary()
    return _GLOSSARY


def llm_define(term: str, groq_api_key: str = "") -> Optional[Dict[str, str]]:
    """Ask Groq for a one-line definition. Returns {'plain_english', 'one_line_example'} or None."""
    if not groq_api_key:
        groq_api_key = os.getenv("GROQ_API_KEY", "")
    if not groq_api_key:
        return None
    try:
        from groq_governor import governor as _gov
    except Exception:
        _gov = None
    if _gov is not None and not _gov.can_spend("jargon", est_tokens=400):
        return None
    try:
        import requests

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_api_key}", "Content-Type": "application/json"},
            json={
                "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You define Indian stock-market trading terms for retail investors. "
                            "Reply with a JSON object: {\"plain_english\": str, \"example\": str}. "
                            "Keep plain_english under 30 words, example under 20 words. "
                            "If the term is not a real trading term, return null."
                        ),
                    },
                    {"role": "user", "content": f"Define: {term}"},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            },
            timeout=12,
        )
        if r.status_code == 429 and _gov is not None:
            _gov.record_429("jargon")
            return None
        r.raise_for_status()
        data = r.json()
        if _gov is not None:
            try:
                tokens = int((data.get("usage") or {}).get("total_tokens", 400))
            except Exception:
                tokens = 400
            _gov.record_spend("jargon", tokens)
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict) or "plain_english" not in parsed:
            return None
        return {
            "plain_english": str(parsed.get("plain_english", "")).strip(),
            "one_line_example": str(parsed.get("example", "")).strip(),
        }
    except Exception as exc:
        logger.warning("llm_define failed term=%s err=%s", term, exc)
        return None
