"""TickerWave — concall transcript indexer.

Walks the existing `filings` table, identifies concall / earnings-call /
investor-presentation filings via title + filing_type heuristics, and
splits each row's `raw_text` into searchable paragraphs stored in
`concall_paragraphs`.

No PDF download / Whisper / embeddings here — we deliberately stay cheap
and ship today. The data we already have (raw_text) is enough to power
useful queries like:
    "Every concall where management mentioned 'capex'"
    "Show me Reliance management's tone on Jio over the last 4 quarters"

Idempotent on (filing_id, paragraph_idx) so a re-run picks up new filings
without re-inserting old paragraphs.

CLI:
    python concall_indexer.py                  # default: index all unindexed
    python concall_indexer.py --reindex        # wipe + rebuild
    python concall_indexer.py --since 2026-01-01
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

# Heuristic patterns: a filing is "concall-like" if any of these hit on
# filing_type or title. Keep regex side cheap — this runs across ~thousands
# of rows; we shouldn't compile a Russian-novel pattern.
_CONCALL_TITLE_RE = re.compile(
    r"\b(?:earnings\s*call|conference\s*call|concall|con\.?\s*call|"
    r"investor\s*(?:meet|presentation|day|conference)|"
    r"analyst\s*(?:meet|day|presentation)|"
    r"results?\s*(?:call|presentation)|capital\s+markets?\s+day)\b",
    re.IGNORECASE,
)
_CONCALL_TYPES = {
    "concall_transcript", "concall", "earnings_call", "conference_call",
    "investor_presentation", "analyst_meet",
}

# Paragraph splitter: prefer existing line breaks; fall back to sentence
# boundaries if the raw_text is a wall. Cap each paragraph at ~700 chars so
# search snippets stay tight.
_MAX_PARA_CHARS = 700
_MIN_PARA_CHARS = 40


def _is_concall(filing: dict) -> bool:
    """True if this filings row looks like a concall transcript."""
    ftype = (filing.get("filing_type") or "").lower()
    if ftype in _CONCALL_TYPES:
        return True
    title = filing.get("title") or ""
    return bool(_CONCALL_TITLE_RE.search(title))


def _split_paragraphs(text: str) -> List[str]:
    """Split raw_text into paragraph-sized chunks.

    Two passes:
      1. Break on blank lines / double newlines (the natural transcript shape).
      2. Anything still longer than _MAX_PARA_CHARS gets sentence-split.
    Anything shorter than _MIN_PARA_CHARS gets dropped (page numbers, table
    of contents fragments, etc.).
    """
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of whitespace inside lines but preserve paragraph breaks
    chunks = re.split(r"\n\s*\n+", text)

    out: List[str] = []
    for ch in chunks:
        ch = re.sub(r"\s+", " ", ch).strip()
        if len(ch) < _MIN_PARA_CHARS:
            continue
        if len(ch) <= _MAX_PARA_CHARS:
            out.append(ch)
            continue
        # Sentence-split for over-long paragraphs
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", ch)
        buf = ""
        for s in sentences:
            if len(buf) + len(s) + 1 <= _MAX_PARA_CHARS:
                buf = (buf + " " + s).strip() if buf else s
            else:
                if len(buf) >= _MIN_PARA_CHARS:
                    out.append(buf)
                buf = s
        if len(buf) >= _MIN_PARA_CHARS:
            out.append(buf)
    return out


def _extract_pdf_text(pdf_url: str, timeout: float = 30.0) -> Optional[str]:
    """Download and extract text from a PDF. Returns None on any failure
    (network, parse, empty). Uses pypdf — pure-python, no system deps.
    Audio files (.mp3 / .wav / .m4a referenced in some concall filings)
    are skipped silently since text extraction obviously isn't applicable.
    """
    if not pdf_url:
        return None
    if pdf_url.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg")):
        return None
    try:
        import requests
        from io import BytesIO
        from pypdf import PdfReader
        # BSE blocks non-browser UAs — use a real Chrome string. Direct (non-streamed)
        # download is faster here; we enforce the 8 MB cap on the response body after
        # the fact rather than chunking.
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "application/pdf,*/*;q=0.8",
            "Referer": "https://www.bseindia.com/",
        }
        r = requests.get(pdf_url, timeout=timeout, headers=headers)
        if r.status_code != 200:
            logger.debug(f"PDF fetch HTTP {r.status_code}: {pdf_url}")
            return None
        max_bytes = 8 * 1024 * 1024
        body = r.content
        if len(body) > max_bytes:
            body = body[:max_bytes]
        if not body.lstrip().startswith(b"%PDF"):
            logger.debug(f"Not a PDF (magic bytes missing): {pdf_url}")
            return None
        reader = PdfReader(BytesIO(body))
        parts = []
        for page in reader.pages[:80]:  # cap pages too
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n\n".join(p for p in parts if p.strip())
        return text if len(text) >= 200 else None
    except Exception as e:
        logger.debug(f"PDF extract failed for {pdf_url}: {e}")
        return None


def _persist_raw_text(db, filing_id: int, raw_text: str) -> None:
    """Write extracted PDF text back to filings.raw_text so subsequent runs
    skip the download entirely. Idempotent — only updates if currently NULL
    or very short."""
    try:
        cur = db.conn.cursor()
        if getattr(db, "is_postgres", False):
            cur.execute(
                "UPDATE filings SET raw_text = %s WHERE id = %s AND "
                "(raw_text IS NULL OR LENGTH(raw_text) < 200)",
                (raw_text, filing_id))
        else:
            cur.execute(
                "UPDATE filings SET raw_text = ? WHERE id = ? AND "
                "(raw_text IS NULL OR LENGTH(raw_text) < 200)",
                (raw_text, filing_id))
        db.conn.commit()
    except Exception as e:
        logger.debug(f"raw_text write-back failed (filing={filing_id}): {e}")


def _fetch_candidates(db, since: Optional[str], wipe: bool,
                      *, extract_missing: bool = True) -> List[dict]:
    """Pull filings rows that look like concalls and aren't already indexed.

    Two phases:
      1. Rows that already have raw_text >= 200 chars — directly indexable.
      2. Rows without raw_text but with a pdf_url — fetch + extract on the
         fly (and write back to filings.raw_text so future runs are fast).
    """
    cur = db.conn.cursor()
    # Phase 1 + 2 read everything; classify after.
    sql = """SELECT id, ticker, filing_type, title, filed_at, raw_text, pdf_url
             FROM filings"""
    params: list = []
    if since:
        sql += " WHERE (filed_at >= ? OR created_at >= ?)"
        params.extend([since, since])
    sql += " ORDER BY filed_at DESC"
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    candidates = [r for r in rows if _is_concall(r)]
    if not candidates:
        return []

    # Phase 2: extract raw_text for candidates that don't have it.
    if extract_missing:
        needs_extract = [r for r in candidates
                         if (not r.get("raw_text") or len(r.get("raw_text") or "") < 200)
                         and r.get("pdf_url")]
        if needs_extract:
            logger.info(f"PDF extract: {len(needs_extract)} filings need raw_text fetched")
            extracted = 0
            for f in needs_extract:
                text = _extract_pdf_text(f["pdf_url"])
                if text:
                    f["raw_text"] = text
                    _persist_raw_text(db, f["id"], text)
                    extracted += 1
                time.sleep(0.4)  # courteous rate limit on BSE/NSE
            logger.info(f"PDF extract: {extracted}/{len(needs_extract)} successful")

    # Final filter: only candidates with usable raw_text
    candidates = [r for r in candidates
                  if r.get("raw_text") and len(r.get("raw_text") or "") >= 200]
    if not candidates:
        return []

    if wipe:
        # Hard rebuild — drop everything, re-index from scratch
        cur.execute("DELETE FROM concall_paragraphs")
        db.conn.commit()
        return candidates

    # Skip filings already fully indexed (at least one paragraph stored).
    ids = [r["id"] for r in candidates]
    placeholders = ",".join(["?"] * len(ids))
    cur.execute(
        f"SELECT DISTINCT filing_id FROM concall_paragraphs WHERE filing_id IN ({placeholders})",
        ids)
    done = {row[0] for row in cur.fetchall()}
    return [r for r in candidates if r["id"] not in done]


def _upsert_paragraphs(db, filing: dict, paragraphs: List[str]) -> int:
    """Insert paragraphs for one filing. Uses UNIQUE(filing_id, paragraph_idx)."""
    if not paragraphs:
        return 0
    is_pg = getattr(db, "is_postgres", False)
    cur = db.conn.cursor()
    n = 0
    for idx, para in enumerate(paragraphs):
        try:
            if is_pg:
                cur.execute(
                    """INSERT INTO concall_paragraphs
                          (filing_id, ticker, filed_at, paragraph_idx, text, char_count)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (filing_id, paragraph_idx) DO UPDATE SET
                          text = EXCLUDED.text,
                          char_count = EXCLUDED.char_count""",
                    (filing["id"], (filing.get("ticker") or "").upper() or None,
                     filing.get("filed_at"), idx, para, len(para)))
            else:
                cur.execute(
                    """INSERT INTO concall_paragraphs
                          (filing_id, ticker, filed_at, paragraph_idx, text, char_count)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(filing_id, paragraph_idx) DO UPDATE SET
                          text = excluded.text,
                          char_count = excluded.char_count""",
                    (filing["id"], (filing.get("ticker") or "").upper() or None,
                     filing.get("filed_at"), idx, para, len(para)))
            n += 1
        except Exception as e:
            logger.warning(f"para upsert failed (filing={filing['id']}, idx={idx}): {e}")
    db.conn.commit()
    return n


def run_indexer(db, *, since: Optional[str] = None, wipe: bool = False,
                limit: Optional[int] = None) -> dict:
    """Index all unindexed concall-like filings.

    Returns a summary suitable for the admin endpoint.
    """
    started = time.time()
    candidates = _fetch_candidates(db, since, wipe)
    if limit:
        candidates = candidates[:limit]
    logger.info(f"concall_indexer: {len(candidates)} filings to process")

    total_paragraphs = 0
    files_indexed = 0
    skipped_empty = 0
    for i, f in enumerate(candidates):
        paras = _split_paragraphs(f.get("raw_text") or "")
        if not paras:
            skipped_empty += 1
            continue
        n = _upsert_paragraphs(db, f, paras)
        if n:
            files_indexed += 1
            total_paragraphs += n
        if (i + 1) % 25 == 0:
            logger.info(f"  [{i+1}/{len(candidates)}] {files_indexed} indexed, "
                        f"{total_paragraphs} paragraphs")

    elapsed = time.time() - started
    summary = {
        "candidates_seen": len(candidates),
        "filings_indexed": files_indexed,
        "paragraphs_upserted": total_paragraphs,
        "skipped_empty": skipped_empty,
        "elapsed_seconds": round(elapsed, 1),
        "completed_at": datetime.utcnow().isoformat() + "Z",
    }
    logger.info(f"concall_indexer complete: {summary}")
    return summary


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", type=str, default=None,
                   help="Only index filings filed after this ISO date (YYYY-MM-DD).")
    p.add_argument("--reindex", action="store_true",
                   help="Wipe concall_paragraphs and re-index from scratch.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap on filings to process (debugging).")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scraper'))
    from database_schema import TickwaveDB  # type: ignore
    db = TickwaveDB()
    db.init_schema()  # ensure concall_paragraphs exists

    summary = run_indexer(db, since=args.since, wipe=args.reindex, limit=args.limit)
    import json as _json
    print(_json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
