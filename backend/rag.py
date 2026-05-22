"""RAG infrastructure: embed, index, search.

Embedding model: local sentence-transformers `all-MiniLM-L6-v2` (~90 MB,
runs on ARM CPU, zero ongoing cost, no rate limit). The chosen model
produces 384-dim vectors, which fit a pgvector(384) column on Supabase
or an in-process numpy fallback for local development.

Storage:
    Postgres (Supabase) — uses pgvector extension. Create table:
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS kb_chunks (
            id           bigserial PRIMARY KEY,
            source       text NOT NULL,
            source_id    text,
            ticker       text,
            chunk        text NOT NULL,
            embedding    vector(384),
            created_at   timestamptz DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx
            ON kb_chunks USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100);

    SQLite (local dev) — falls back to a TEXT column with JSON-encoded
    floats; cosine similarity is computed in Python. Acceptable for <10k
    chunks; production must use Postgres + pgvector.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MODEL = None
_DIM = 384


def _get_model():
    """Lazy-load the sentence-transformers model."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        model_name = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        _MODEL = SentenceTransformer(model_name)
        logger.info("rag: loaded embedding model %s", model_name)
        return _MODEL
    except ImportError:
        logger.warning("rag: sentence-transformers not installed — falling back to bag-of-words hash")
        return None


def embed(text: str) -> List[float]:
    """Return a unit-norm embedding vector. Falls back to a hash-based
    pseudo-embedding when sentence-transformers is missing — this lets
    the wiring proceed in dev but won't produce useful similarity."""
    model = _get_model()
    if model is not None:
        vec = model.encode(text or "", normalize_embeddings=True).tolist()
        return [float(x) for x in vec]
    # Fallback: deterministic hash-bag (NOT semantically meaningful)
    import hashlib
    h = hashlib.sha256((text or "").encode("utf-8")).digest()
    out = [0.0] * _DIM
    for i, b in enumerate(h * (_DIM // 32 + 1)):
        if i >= _DIM:
            break
        out[i] = (b / 255.0) - 0.5
    n = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / n for x in out]


def _is_postgres(db) -> bool:
    return bool(getattr(db, "is_postgres", False))


_ENSURED = False


def _ensure_schema(db) -> None:
    global _ENSURED
    if _ENSURED:
        return
    try:
        cur = db.conn.cursor()
        if _is_postgres(db):
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id          bigserial PRIMARY KEY,
                    source      text NOT NULL,
                    source_id   text,
                    ticker      text,
                    chunk       text NOT NULL,
                    embedding   vector(384),
                    created_at  timestamptz DEFAULT now()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx "
                "ON kb_chunks USING ivfflat (embedding vector_cosine_ops) "
                "WITH (lists = 100)"
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS kb_chunks_unique "
                "ON kb_chunks(source, source_id)"
            )
        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    source      TEXT NOT NULL,
                    source_id   TEXT,
                    ticker      TEXT,
                    chunk       TEXT NOT NULL,
                    embedding   TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS kb_chunks_unique "
                "ON kb_chunks(source, source_id)"
            )
        db.conn.commit()
        _ENSURED = True
    except Exception as e:
        logger.warning("rag schema ensure failed: %s", e)


def index_chunk(db, *, source: str, source_id: Optional[str], ticker: Optional[str],
                chunk: str) -> Optional[int]:
    """Insert one chunk + embedding. Idempotent on (source, source_id)."""
    _ensure_schema(db)
    vec = embed(chunk)
    try:
        cur = db.conn.cursor()
        if _is_postgres(db):
            cur.execute(
                "INSERT INTO kb_chunks (source, source_id, ticker, chunk, embedding) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (source, source_id) DO NOTHING RETURNING id",
                (source, source_id, ticker, chunk, vec),
            )
            row = cur.fetchone()
            db.conn.commit()
            return int(row[0]) if row else None
        else:
            try:
                cur.execute(
                    "INSERT INTO kb_chunks (source, source_id, ticker, chunk, embedding) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (source, source_id, ticker, chunk, json.dumps(vec)),
                )
                db.conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None  # duplicate
    except Exception as e:
        logger.warning("rag index_chunk failed: %s", e)
        return None


def search(db, query: str, *, k: int = 8, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return the top-k most similar chunks. Filters by ticker if given."""
    _ensure_schema(db)
    qvec = embed(query)
    try:
        cur = db.conn.cursor()
        if _is_postgres(db):
            params: List[Any] = [qvec]
            sql = "SELECT id, source, source_id, ticker, chunk, " \
                  "1 - (embedding <=> %s::vector) AS sim FROM kb_chunks "
            if ticker:
                sql += "WHERE ticker = %s "
                params.append(ticker)
            sql += "ORDER BY embedding <=> %s::vector LIMIT %s"
            params.extend([qvec, int(k)])
            rows = cur.execute(sql, params).fetchall()
            return [
                {"id": r[0], "source": r[1], "source_id": r[2], "ticker": r[3],
                 "chunk": r[4], "similarity": float(r[5])}
                for r in rows
            ]
        else:
            sql = "SELECT id, source, source_id, ticker, chunk, embedding FROM kb_chunks"
            params = []
            if ticker:
                sql += " WHERE ticker = ?"
                params.append(ticker)
            rows = cur.execute(sql, params).fetchall()
            scored: List[Tuple[float, Dict[str, Any]]] = []
            for r in rows:
                try:
                    vec = json.loads(r[5] or "[]")
                except Exception:
                    continue
                if not vec:
                    continue
                sim = sum(a * b for a, b in zip(qvec, vec))
                scored.append((sim, {
                    "id": r[0], "source": r[1], "source_id": r[2],
                    "ticker": r[3], "chunk": r[4], "similarity": float(sim),
                }))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [s[1] for s in scored[:k]]
    except Exception as e:
        logger.warning("rag search failed: %s", e)
        return []


# ── Queue handler ─────────────────────────────────────────────────────
# Indexes a batch of chunks for a given source. Payload:
#   {"source": "event", "source_id": "12345", "ticker": "RELIANCE",
#    "chunks": ["...", "..."]}

def index_chunks_handler(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    source = payload.get("source") or "unknown"
    source_id = payload.get("source_id")
    ticker = payload.get("ticker")
    chunks = payload.get("chunks") or []
    if isinstance(chunks, str):
        chunks = [chunks]
    indexed = 0
    for i, ch in enumerate(chunks):
        sid = source_id if i == 0 else f"{source_id}#{i}"
        if index_chunk(db, source=source, source_id=sid, ticker=ticker, chunk=ch):
            indexed += 1
    return {"ok": True, "indexed": indexed, "source": source}


def stats(db) -> Dict[str, Any]:
    """Counts by source for the admin dashboard."""
    _ensure_schema(db)
    try:
        cur = db.conn.cursor()
        rows = cur.execute(
            "SELECT source, COUNT(*) FROM kb_chunks GROUP BY source"
        ).fetchall()
        total = cur.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
        return {
            "total": int(total),
            "by_source": {r[0]: int(r[1]) for r in rows},
            "model": os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        }
    except Exception as e:
        return {"error": str(e)}
