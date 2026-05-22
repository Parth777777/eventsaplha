"""TickerWave Planning Assistant — multi-turn financial-only agent.

Routes (mounted from backend/api.py via register()):
    POST   /api/chat                — SSE: meta, deltas, suggestions, done
    GET    /api/chat/quota          — tier + remaining today
    GET    /api/chat/threads        — list user's threads (newest first)
    POST   /api/chat/threads        — create a new thread, return id
    GET    /api/chat/threads/<id>   — load thread + messages (ownership-checked)
    DELETE /api/chat/threads/<id>   — soft archive

Design (see [[financial-planner-agent]] memory):

    * Strict NSE/BSE-only scope. Off-topic → one-line redirect via AGENT_ROLE.
    * Analysis-only. Buy/sell/hold/target requests → structured analysis fallback.
    * Multi-turn RAG: last 3 turn-pairs (≤2000 tokens) prepended each turn.
    * Live context auto-injected each turn: watchlist + top 5 alpha events 4h +
      market regime. 60s in-process TTL keyed by user_id.
    * User profile (risk/horizon/sectors/goals) injected when present.
    * Auto-title via cheap groq_chat (prefer_fast) after first assistant turn.
    * 3 follow-up suggestions emitted via SSE before `done`.
    * Heuristic loggers (recommendation regex, finance-vocab) write to
      chat_refusals for analytics. LLM is the final gate.
    * Every Groq call routes through module="chat" so the governor's 15%
      TPD slice can throttle if exhausted.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from flask import Response, g, jsonify, request, stream_with_context

logger = logging.getLogger(__name__)


# ── Import shim ─────────────────────────────────────────────────────────
# Sibling modules (rag, ai_groq, freemium) sit in this same package. When
# the app starts via `python -m backend.api`, `backend.rag` resolves. When
# it starts via `python api.py` (dev), the backend dir is on sys.path and
# the bare-name import works. Use this helper everywhere we'd previously
# have written `from backend.X import Y` so chat_routes loads in both modes.
def _import_local(name: str):
    try:
        return __import__(f"backend.{name}", fromlist=["*"])
    except ImportError:
        return __import__(name)


# ── Agent role definition ────────────────────────────────────────────────
# Single source of truth for the agent's identity, scope, refusals, and
# grounding rules. Edit HERE to tune behavior — do not sprinkle rules across
# the codebase.

AGENT_ROLE = """# Identity
You are TickerWave Planning Assistant — a financial analysis agent for
Indian equity markets (NSE/BSE). You are NOT a general assistant.

# In-scope topics (answer)
- NSE/BSE stocks, sectors, indices
- Earnings, results, corporate actions, filings, IPOs, M&A
- Market events, news, alpha signals, regime, FII/DII flows
- User's watchlist, profile-aware framing
- Glossary / educational finance questions (Indian markets)
- Global macro ONLY as it bears on NSE/BSE (USDINR, crude, US rates → IT/banks)

# Out-of-scope (REFUSE with redirect)
- Anything unrelated to Indian equity markets (coding, jokes, trivia,
  personal life, news outside markets, sports, politics, cooking, etc.)
- Crypto, US/EU/Asian stock-specific questions (macro overlap is OK)
- Tax / legal / accounting advice
- Personal financial planning beyond stock analysis (insurance, loans, real estate)

If a question is out-of-scope, reply with EXACTLY one short sentence:
"I'm a financial planning analyst for NSE/BSE markets. I can help with
stocks, sectors, events, earnings, filings, or your watchlist — what
would you like to explore?"
Then STOP. Do not attempt the off-topic answer.

# Recommendation refusal (separate from off-topic)
If asked whether to buy/sell/hold a specific instrument, or for price
targets / entry / exit / stop-loss / position size, refuse the
recommendation and instead return a structured analysis:
- Catalysts (cite [source:id])
- Regime fit vs the user's profile
- Risk factors / watchpoints
Never produce price targets, entry/exit prices, stop-losses, or
position sizes. Never use words like "recommend", "should buy",
"target price", or "buy at".

# Grounding rules
- Use ONLY [LIVE CONTEXT], [USER PROFILE], [CONTEXT] chunks, and prior
  turns. Do NOT fall back to general knowledge.
- Cite [source:id] in brackets for every factual claim drawn from RAG.
- If context is insufficient, say so directly and suggest what would
  help — do NOT speculate.

# Tone & format
- Concise, plain English, concrete numbers (₹ crore, %, bps).
- 2-4 sentences for simple Qs; bullets for comparisons.
- Indian market conventions: lakhs/crores, NSE/BSE tickers in ALL CAPS.
"""


# ── Refusal heuristics (analytics only — LLM is the final gate) ──────────

_RECOMMENDATION_RE = re.compile(
    r"\b(should\s+i|recommend|recommendation|buy|sell|hold|"
    r"target\s*price|stop\s*loss|stoploss|entry\s*point|exit\s*point|"
    r"position\s*size|how\s*much\s*to\s*invest)\b",
    re.IGNORECASE,
)

# Compact vocab list — true if a query contains at least one finance-y
# token, a likely-ticker (ALLCAPS 3-12 chars), or a known company name
# fetched lazily from the `companies` table.
_FINANCE_VOCAB = frozenset({
    "stock", "stocks", "share", "shares", "equity", "equities", "market",
    "markets", "trading", "trader", "trade", "invest", "investor", "investment",
    "earnings", "result", "results", "quarterly", "quarter", "annual", "fy",
    "dividend", "dividends", "split", "bonus", "buyback", "qip", "fpo",
    "rights", "preference", "preferential",
    "nifty", "sensex", "banknifty", "bse", "nse", "midcap", "smallcap",
    "largecap", "index", "indices", "sector", "sectors",
    "fii", "dii", "mf", "etf", "fund", "fii_dii", "fpi", "promoter",
    "promoters", "pledge", "insider", "bulk", "block",
    "ipo", "ipos", "listing", "issue", "subscription", "sme",
    "merger", "acquisition", "demerger", "ma", "deal", "takeover",
    "alpha", "beta", "regime", "volatility", "vix", "drawdown", "support",
    "resistance", "breakout", "rally", "correction", "circuit",
    "eps", "pe", "p/e", "pb", "p/b", "roe", "roce", "debt", "margin",
    "revenue", "profit", "ebitda", "loss", "turnover", "valuation",
    "fundamental", "fundamentals", "technical", "technicals", "chart",
    "candle", "candles", "moving", "average", "rsi", "macd", "obv",
    "filing", "filings", "disclosure", "sebi", "rbi", "annual_report",
    "ar", "concall", "transcript", "guidance",
    "watchlist", "portfolio", "screener", "screen",
    "macro", "policy", "rate", "inflation", "gdp", "cpi", "wpi",
    "fii_flow", "fii_outflow", "fii_inflow",
    "futures", "options", "fno", "f&o", "oi", "open_interest", "put",
    "call", "strike", "expiry",
    "rupee", "rupees", "inr", "rs", "₹", "crore", "lakh", "lac",
    # global macro overlap
    "usdinr", "crude", "brent", "wti", "fed", "fomc", "treasury", "yield",
})


def _is_recommendation_query(q: str) -> bool:
    return bool(_RECOMMENDATION_RE.search(q or ""))


def _is_likely_financial(q: str, db=None) -> bool:
    """Heuristic — true if the question has at least one finance vocab token,
    a likely-ticker (3-12 uppercase chars), or a known company name match.

    Used ONLY for analytics logging. The LLM does the real refusal via
    AGENT_ROLE so false negatives here don't cause incorrect refusals."""
    if not q:
        return False
    s = q.lower()
    # Vocab hit
    for tok in re.findall(r"[a-z₹/&]+", s):
        if tok in _FINANCE_VOCAB:
            return True
    # Likely ticker (3-12 uppercase chars in original)
    for m in re.finditer(r"\b[A-Z][A-Z0-9&]{2,11}\b", q):
        if m.group(0) not in ("WHO", "WHAT", "WHEN", "WHERE", "WHY", "HOW", "AND",
                              "THE", "FOR", "WITH", "ABOUT", "FROM", "INTO"):
            return True
    # Known company name (cheap LIKE — last resort, only if DB given)
    if db is not None:
        try:
            cur = db.conn.cursor()
            # Strip query to lowercase letters; pick longest 4+ char word
            words = sorted([w for w in re.findall(r"[a-zA-Z]{4,}", q)],
                           key=len, reverse=True)[:3]
            for w in words:
                row = cur.execute(
                    "SELECT 1 FROM companies WHERE lower(name) LIKE ? LIMIT 1",
                    (f"%{w.lower()}%",),
                ).fetchone()
                if row:
                    return True
        except Exception:
            pass
    return False


# ── Schema bootstrap ─────────────────────────────────────────────────────
# chat_threads / chat_messages / user_profile / chat_refusals live in
# scraper/schema_ext.py; chat_usage is legacy (per-message quota), still
# created here so a partial DB still works.

_SCHEMA_ENSURED = False


def _ensure_chat_schema(db) -> None:
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return
    try:
        # Apply the canonical extension schema (idempotent). Try both
        # import paths — `scraper/` is on sys.path directly, but the
        # package form also works if the project root is.
        apply_schema_ext = None
        try:
            from schema_ext import apply as apply_schema_ext  # type: ignore
        except Exception:
            try:
                from scraper.schema_ext import apply as apply_schema_ext  # type: ignore
            except Exception as e:
                logger.debug("schema_ext import skipped: %s", e)
        if apply_schema_ext is not None:
            try:
                apply_schema_ext(db.conn)
            except Exception as e:
                logger.debug("schema_ext.apply failed: %s", e)
        # Legacy chat_usage (per-message quota counter)
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tokens INTEGER DEFAULT 0,
                ticker TEXT,
                question TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_chatusage_user_day "
            "ON chat_usage(user_id, used_at)"
        )
        db.conn.commit()
        _SCHEMA_ENSURED = True
    except Exception as e:
        logger.warning("chat schema ensure failed: %s", e)


# ── Quota ────────────────────────────────────────────────────────────────

def _count_today(db, user_id: str) -> int:
    try:
        cur = db.conn.cursor()
        n = cur.execute(
            "SELECT COUNT(*) FROM chat_usage WHERE user_id = ? "
            "AND used_at >= datetime('now', 'start of day')",
            (user_id,),
        ).fetchone()[0]
        return int(n or 0)
    except Exception:
        return 0


def _record_usage(db, user_id: str, ticker: Optional[str], question: str,
                  tokens: int) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO chat_usage (user_id, ticker, question, tokens) "
            "VALUES (?, ?, ?, ?)",
            (user_id, ticker, (question or "")[:500], int(tokens)),
        )
        db.conn.commit()
    except Exception as e:
        logger.debug("chat record_usage failed: %s", e)


# ── Thread CRUD helpers ──────────────────────────────────────────────────

def _create_thread(db, user_id: str, title: Optional[str] = None) -> int:
    cur = db.conn.cursor()
    cur.execute(
        "INSERT INTO chat_threads (user_id, title) VALUES (?, ?)",
        (user_id, title),
    )
    db.conn.commit()
    return int(cur.lastrowid)


def _thread_owned_by(db, thread_id: int, user_id: str) -> bool:
    try:
        cur = db.conn.cursor()
        row = cur.execute(
            "SELECT user_id FROM chat_threads WHERE id = ?", (thread_id,),
        ).fetchone()
        if not row:
            return False
        # Row may be tuple OR dict-row depending on driver
        owner = row[0] if not isinstance(row, dict) else row.get("user_id")
        return str(owner) == str(user_id)
    except Exception:
        return False


def _load_thread_messages(db, thread_id: int, limit_turns: int = 3,
                          max_tokens: int = 2000) -> List[Dict[str, str]]:
    """Last `limit_turns` user/assistant pairs, oldest first, dropped until
    within `max_tokens` (rough len//4 estimate to match _record_usage)."""
    try:
        cur = db.conn.cursor()
        # Fetch up to limit_turns*2 most recent rows, then re-order ascending
        rows = cur.execute(
            "SELECT role, content FROM chat_messages "
            "WHERE thread_id = ? ORDER BY created_at DESC LIMIT ?",
            (thread_id, limit_turns * 2),
        ).fetchall()
        msgs: List[Dict[str, str]] = []
        for r in rows:
            role = r[0] if not isinstance(r, dict) else r.get("role")
            content = r[1] if not isinstance(r, dict) else r.get("content")
            msgs.append({"role": str(role), "content": str(content or "")})
        msgs.reverse()
        # Trim oldest first if over budget
        while msgs and sum(len(m["content"]) // 4 for m in msgs) > max_tokens:
            msgs.pop(0)
        return msgs
    except Exception as e:
        logger.debug("load_thread_messages failed: %s", e)
        return []


def _persist_messages(db, thread_id: int, user_q: str, assistant_text: str,
                      citations: List[Dict[str, Any]], tokens: int) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (thread_id, role, content, tokens) "
            "VALUES (?, ?, ?, ?)",
            (thread_id, "user", (user_q or "")[:4000], 0),
        )
        cur.execute(
            "INSERT INTO chat_messages "
            "(thread_id, role, content, citations_json, tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            (thread_id, "assistant", (assistant_text or "")[:8000],
             json.dumps(citations or []), int(tokens)),
        )
        cur.execute(
            "UPDATE chat_threads SET last_active_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (thread_id,),
        )
        db.conn.commit()
    except Exception as e:
        logger.warning("persist_messages failed: %s", e)


def _log_refusal(db, user_id: str, thread_id: Optional[int],
                 question: str, reason: str) -> None:
    try:
        cur = db.conn.cursor()
        cur.execute(
            "INSERT INTO chat_refusals (user_id, thread_id, question, reason) "
            "VALUES (?, ?, ?, ?)",
            (user_id, thread_id, (question or "")[:500], reason),
        )
        db.conn.commit()
    except Exception as e:
        logger.debug("log_refusal failed: %s", e)


# ── Live-context injection (60s in-process TTL) ──────────────────────────

_LIVE_CTX_CACHE: Dict[str, Tuple[float, str]] = {}
_LIVE_CTX_TTL = 60.0  # seconds


def build_live_context(db, user_id: str) -> str:
    """Returns a compact `[LIVE CONTEXT]` block — watchlist tickers, top 5
    alpha events last 4h, market regime. ≤500 tokens. Cached 60s per user."""
    now = time.time()
    cached = _LIVE_CTX_CACHE.get(user_id)
    if cached and (now - cached[0]) < _LIVE_CTX_TTL:
        return cached[1]
    parts: List[str] = []
    cur = db.conn.cursor()

    # Watchlist
    try:
        rows = cur.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? LIMIT 20",
            (user_id,),
        ).fetchall()
        tickers = [r[0] if not isinstance(r, dict) else r.get("ticker")
                   for r in rows]
        tickers = [t for t in tickers if t]
        if tickers:
            parts.append("Watchlist: " + ", ".join(tickers[:20]))
    except Exception:
        pass

    # Top 5 alpha events last 4h
    try:
        rows = cur.execute(
            "SELECT ticker, title, event_type, alpha_score "
            "FROM signals WHERE created_at >= datetime('now','-4 hours') "
            "ORDER BY alpha_score DESC LIMIT 5"
        ).fetchall()
        events = []
        for r in rows:
            if isinstance(r, dict):
                t, ti, et, al = r.get("ticker"), r.get("title"), \
                    r.get("event_type"), r.get("alpha_score")
            else:
                t, ti, et, al = r[0], r[1], r[2], r[3]
            if t and ti:
                events.append(f"  - [{t}] α={int(al or 0)} {et or ''}: "
                              f"{(ti or '')[:120]}")
        if events:
            parts.append("Top alpha events (last 4h):\n" + "\n".join(events))
    except Exception:
        pass

    # Market regime
    try:
        row = cur.execute(
            "SELECT predicted_nifty_bias, confidence FROM overnight_snapshots "
            "ORDER BY snap_date DESC LIMIT 1"
        ).fetchone()
        if row:
            bias = row[0] if not isinstance(row, dict) else row.get("predicted_nifty_bias")
            conf = row[1] if not isinstance(row, dict) else row.get("confidence")
            if bias is not None:
                tag = "bullish" if bias > 0.2 else "bearish" if bias < -0.2 else "neutral"
                parts.append(f"Market regime: {tag} (nifty_bias={bias:+.2f}, "
                             f"conf={conf or 0:.2f})")
    except Exception:
        pass

    if not parts:
        block = ""
    else:
        block = "[LIVE CONTEXT]\n" + "\n".join(parts)
        # Hard cap ~500 tokens (2000 chars)
        if len(block) > 2000:
            block = block[:2000] + "\n…(truncated)"
    _LIVE_CTX_CACHE[user_id] = (now, block)
    return block


def build_user_profile_block(db, user_id: str) -> str:
    """Returns a `[USER PROFILE]` block if the user has a profile row."""
    try:
        cur = db.conn.cursor()
        row = cur.execute(
            "SELECT risk_profile, horizon, sectors_json, goals_text "
            "FROM user_profile WHERE user_id = ?", (user_id,),
        ).fetchone()
        if not row:
            return ""
        if isinstance(row, dict):
            risk = row.get("risk_profile")
            hor = row.get("horizon")
            sectors_json = row.get("sectors_json")
            goals = row.get("goals_text")
        else:
            risk, hor, sectors_json, goals = row[0], row[1], row[2], row[3]
        bits: List[str] = []
        if risk:
            bits.append(f"Risk tolerance: {risk}")
        if hor:
            bits.append(f"Horizon: {hor}")
        if sectors_json:
            try:
                sectors = json.loads(sectors_json)
                if isinstance(sectors, list) and sectors:
                    bits.append("Preferred sectors: " + ", ".join(sectors[:10]))
            except Exception:
                pass
        if goals:
            bits.append(f"Goals: {(goals or '')[:300]}")
        if not bits:
            return ""
        return "[USER PROFILE]\n" + "\n".join(bits)
    except Exception:
        return ""


# ── Auto-title + suggestions (cheap, fast-model groq calls) ──────────────

def _gen_thread_title(question: str) -> Optional[str]:
    try:
        groq_chat = _import_local("ai_groq").groq_chat
        title = groq_chat(
            module="chat",
            prefer_fast=True,
            messages=[
                {"role": "system",
                 "content": "Return a 3-6 word title for the user's question. "
                            "No quotes, no trailing punctuation. Title Case."},
                {"role": "user", "content": question[:500]},
            ],
            max_tokens=20,
            est_tokens=200,
            temperature=0.2,
            timeout_secs=8,
        )
        if title:
            title = title.strip().strip('"').strip("'")[:80]
        return title or None
    except Exception:
        return None


def _gen_suggestions(question: str, answer: str) -> List[str]:
    try:
        groq_chat = _import_local("ai_groq").groq_chat
        raw = groq_chat(
            module="chat",
            prefer_fast=True,
            messages=[
                {"role": "system",
                 "content": "Suggest 3 short follow-up questions a stock-market "
                            "researcher might ask next. NSE/BSE context. "
                            "One per line, no numbering, no quotes, "
                            "≤10 words each. Output ONLY the 3 lines."},
                {"role": "user",
                 "content": f"Q: {question[:300]}\nA: {(answer or '')[:600]}"},
            ],
            max_tokens=80,
            est_tokens=400,
            temperature=0.5,
            timeout_secs=8,
        )
        if not raw:
            return []
        lines = [ln.strip(" -•*\t").rstrip("?") + "?"
                 for ln in raw.splitlines() if ln.strip()]
        # Drop empties + cap length per line
        out = [ln for ln in lines if 5 <= len(ln) <= 120][:3]
        return out
    except Exception:
        return []


# ── Route registration ───────────────────────────────────────────────────

def register(app, get_db: Callable, optional_auth, require_auth):
    """Mount /api/chat + /api/chat/threads + /api/chat/quota on the Flask app."""

    # ─── POST /api/chat ──────────────────────────────────────────────────
    @app.route("/api/chat", methods=["POST"])
    @optional_auth
    def chat_endpoint():
        rag_search = _import_local("rag").search
        groq_chat_stream = _import_local("ai_groq").groq_chat_stream
        _frm = _import_local("freemium")
        get_user_tier, chat_daily_cap = _frm.get_user_tier, _frm.chat_daily_cap

        body = request.get_json(silent=True) or {}
        question = (body.get("q") or body.get("question") or "").strip()
        ticker = (body.get("ticker") or None)
        thread_id_in = body.get("thread_id")
        if not question or len(question) > 800:
            return jsonify({"success": False,
                            "error": "question required (1-800 chars)"}), 400

        db = get_db()
        _ensure_chat_schema(db)
        user_id = str(g.user_id)
        tier = get_user_tier(db, user_id)
        cap = chat_daily_cap(tier)
        used_today = _count_today(db, user_id)
        if used_today >= cap:
            return jsonify({
                "success": False,
                "error": "chat_quota_exceeded",
                "tier": tier,
                "limit": cap,
                "upgrade_url": "/app/pricing.html",
            }), 429

        # Resolve / create thread
        thread_id: Optional[int] = None
        is_first_turn = False
        if thread_id_in:
            try:
                tid = int(thread_id_in)
                if _thread_owned_by(db, tid, user_id):
                    thread_id = tid
            except Exception:
                thread_id = None
        if thread_id is None:
            thread_id = _create_thread(db, user_id, title=None)
            is_first_turn = True

        # If existing thread but no messages yet, also treat as first turn
        if not is_first_turn:
            try:
                cur = db.conn.cursor()
                n = cur.execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()[0]
                if int(n or 0) == 0:
                    is_first_turn = True
            except Exception:
                pass

        # Pre-LLM refusal heuristics — analytics only, never blocks
        if _is_recommendation_query(question):
            _log_refusal(db, user_id, thread_id, question, "recommendation")
        elif not _is_likely_financial(question, db):
            _log_refusal(db, user_id, thread_id, question, "off_topic")

        # RAG retrieval (grounding chunks)
        try:
            chunks = rag_search(db, question, k=8, ticker=ticker)
        except Exception as e:
            logger.warning("chat rag.search failed: %s", e)
            chunks = []

        context_block = "\n".join(
            f"[{c.get('source')}:{c.get('source_id') or c.get('id')}] "
            f"{c.get('chunk','')[:600]}" for c in chunks
        ) if chunks else "(no relevant context found)"

        # Build the message list:
        #   system: AGENT_ROLE
        #   system: LIVE CONTEXT (if non-empty)
        #   system: USER PROFILE (if non-empty)
        #   ...prior turns (last 3 pairs, ≤2000 tokens)
        #   user: current turn with RAG context
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": AGENT_ROLE},
        ]
        live_ctx = build_live_context(db, user_id)
        if live_ctx:
            messages.append({"role": "system", "content": live_ctx})
        profile_block = build_user_profile_block(db, user_id)
        if profile_block:
            messages.append({"role": "system", "content": profile_block})

        history = _load_thread_messages(db, thread_id, limit_turns=3,
                                        max_tokens=2000)
        messages.extend(history)

        user_turn = (
            f"[CONTEXT — {len(chunks)} chunks]\n{context_block}\n\n"
            f"Question: {question}"
        )
        if ticker:
            user_turn += f"\nTicker focus: {ticker}"
        messages.append({"role": "user", "content": user_turn})

        def _stream() -> Generator[str, None, None]:
            # Preamble
            meta = {
                "type": "meta",
                "thread_id": thread_id,
                "chunks": [
                    {"source": c.get("source"),
                     "id": c.get("source_id") or c.get("id")}
                    for c in chunks
                ],
                "tier": tier,
                "remaining_today": max(0, cap - used_today - 1),
            }
            yield f"data: {json.dumps(meta)}\n\n"

            # Stream main response
            answer_parts: List[str] = []
            total_chars = 0
            try:
                for delta in groq_chat_stream(
                    module="chat",
                    messages=messages,
                    max_tokens=400,
                    est_tokens=3500,  # AGENT_ROLE + ctx + profile + history + RAG
                    temperature=0.25,
                    timeout_secs=60,
                ):
                    answer_parts.append(delta)
                    total_chars += len(delta)
                    yield f"data: {json.dumps({'type':'delta','t':delta})}\n\n"
            except Exception as e:
                logger.warning("chat stream error: %s", e)

            answer_text = "".join(answer_parts).strip()
            est_tokens = max(50, total_chars // 4)

            # Persist + quota
            try:
                _persist_messages(
                    db, thread_id, question, answer_text,
                    [{"source": c.get("source"),
                      "id": c.get("source_id") or c.get("id")}
                     for c in chunks],
                    est_tokens,
                )
                _record_usage(db, user_id, ticker, question, est_tokens)
            except Exception as e:
                logger.warning("chat persist/usage failed: %s", e)

            # Auto-title (only first turn; cheap fast-model call)
            if is_first_turn and answer_text:
                t = _gen_thread_title(question)
                if t:
                    try:
                        cur = db.conn.cursor()
                        cur.execute(
                            "UPDATE chat_threads SET title = ? "
                            "WHERE id = ? AND (title IS NULL OR title = '')",
                            (t, thread_id),
                        )
                        db.conn.commit()
                        yield (f"data: "
                               f"{json.dumps({'type':'title','title':t})}\n\n")
                    except Exception as e:
                        logger.debug("auto-title persist failed: %s", e)

            # Suggestions (skipped silently if governor refuses)
            sug = _gen_suggestions(question, answer_text)
            if sug:
                yield (f"data: "
                       f"{json.dumps({'type':'suggestions','items':sug})}\n\n")

            yield f"data: {json.dumps({'type':'done'})}\n\n"

        return Response(
            stream_with_context(_stream()),
            mimetype="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    # ─── GET /api/chat/quota ─────────────────────────────────────────────
    @app.route("/api/chat/quota", methods=["GET"])
    @optional_auth
    def chat_quota():
        _frm = _import_local("freemium")
        get_user_tier, chat_daily_cap = _frm.get_user_tier, _frm.chat_daily_cap
        db = get_db()
        _ensure_chat_schema(db)
        user_id = str(getattr(g, "user_id", "legacy"))
        tier = get_user_tier(db, user_id)
        cap = chat_daily_cap(tier)
        used = _count_today(db, user_id) if user_id != "legacy" else 0
        return jsonify({
            "success": True,
            "tier": tier,
            "limit": cap,
            "used_today": used,
            "remaining": max(0, cap - used),
        })

    # ─── GET /api/chat/threads ───────────────────────────────────────────
    @app.route("/api/chat/threads", methods=["GET"])
    @optional_auth
    def chat_threads_list():
        db = get_db()
        _ensure_chat_schema(db)
        user_id = str(g.user_id)
        try:
            cur = db.conn.cursor()
            rows = cur.execute(
                "SELECT id, title, created_at, last_active_at "
                "FROM chat_threads "
                "WHERE user_id = ? AND (archived = 0 OR archived IS NULL) "
                "ORDER BY last_active_at DESC LIMIT 50",
                (user_id,),
            ).fetchall()
            items = []
            for r in rows:
                if isinstance(r, dict):
                    items.append({
                        "id": r.get("id"),
                        "title": r.get("title") or "New chat",
                        "created_at": r.get("created_at"),
                        "last_active_at": r.get("last_active_at"),
                    })
                else:
                    items.append({
                        "id": r[0], "title": r[1] or "New chat",
                        "created_at": str(r[2]), "last_active_at": str(r[3]),
                    })
            return jsonify({"success": True, "threads": items})
        except Exception as e:
            logger.warning("chat_threads_list failed: %s", e)
            return jsonify({"success": False, "threads": []}), 200

    # ─── POST /api/chat/threads ──────────────────────────────────────────
    @app.route("/api/chat/threads", methods=["POST"])
    @optional_auth
    def chat_threads_create():
        db = get_db()
        _ensure_chat_schema(db)
        user_id = str(g.user_id)
        try:
            tid = _create_thread(db, user_id, title=None)
            return jsonify({"success": True, "id": tid, "title": "New chat"})
        except Exception as e:
            logger.warning("chat_threads_create failed: %s", e)
            return jsonify({"success": False, "error": "create_failed"}), 500

    # ─── GET /api/chat/threads/<id> ──────────────────────────────────────
    @app.route("/api/chat/threads/<int:tid>", methods=["GET"])
    @optional_auth
    def chat_thread_get(tid: int):
        db = get_db()
        _ensure_chat_schema(db)
        user_id = str(g.user_id)
        if not _thread_owned_by(db, tid, user_id):
            return jsonify({"success": False, "error": "not_found"}), 404
        try:
            cur = db.conn.cursor()
            head = cur.execute(
                "SELECT id, title, created_at, last_active_at "
                "FROM chat_threads WHERE id = ?", (tid,),
            ).fetchone()
            rows = cur.execute(
                "SELECT role, content, citations_json, created_at "
                "FROM chat_messages WHERE thread_id = ? "
                "ORDER BY created_at ASC LIMIT 200", (tid,),
            ).fetchall()
            messages: List[Dict[str, Any]] = []
            for r in rows:
                if isinstance(r, dict):
                    role, content = r.get("role"), r.get("content")
                    cj, ts = r.get("citations_json"), r.get("created_at")
                else:
                    role, content, cj, ts = r[0], r[1], r[2], r[3]
                cites = []
                if cj:
                    try:
                        cites = json.loads(cj)
                    except Exception:
                        cites = []
                messages.append({
                    "role": role,
                    "content": content,
                    "citations": cites,
                    "created_at": str(ts),
                })
            if isinstance(head, dict):
                head_d = {"id": head.get("id"),
                          "title": head.get("title") or "New chat",
                          "created_at": str(head.get("created_at")),
                          "last_active_at": str(head.get("last_active_at"))}
            else:
                head_d = {"id": head[0],
                          "title": head[1] or "New chat",
                          "created_at": str(head[2]),
                          "last_active_at": str(head[3])}
            return jsonify({"success": True,
                            "thread": head_d, "messages": messages})
        except Exception as e:
            logger.warning("chat_thread_get failed: %s", e)
            return jsonify({"success": False, "error": "load_failed"}), 500

    # ─── DELETE /api/chat/threads/<id> ───────────────────────────────────
    @app.route("/api/chat/threads/<int:tid>", methods=["DELETE"])
    @optional_auth
    def chat_thread_delete(tid: int):
        db = get_db()
        _ensure_chat_schema(db)
        user_id = str(g.user_id)
        if not _thread_owned_by(db, tid, user_id):
            return jsonify({"success": False, "error": "not_found"}), 404
        try:
            cur = db.conn.cursor()
            cur.execute(
                "UPDATE chat_threads SET archived = 1 WHERE id = ?", (tid,),
            )
            db.conn.commit()
            return jsonify({"success": True})
        except Exception as e:
            logger.warning("chat_thread_delete failed: %s", e)
            return jsonify({"success": False, "error": "delete_failed"}), 500

    logger.info("chat_routes: registered /api/chat (SSE) + threads + quota")

    # DEBUG: list every /api/chat* rule actually mounted on this app
    try:
        chat_rules = [
            (str(r), sorted(r.methods - {"HEAD", "OPTIONS"}))
            for r in app.url_map.iter_rules() if str(r).startswith("/api/chat")
        ]
        logger.info("chat_routes RULE DUMP on app=%s: %s", id(app), chat_rules)
    except Exception as _e:
        logger.warning("chat_routes rule dump failed: %s", _e)
