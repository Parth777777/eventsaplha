# Indian-Market Sentiment Engine — Plan

## Context

The current pipeline already tags every event with `bullish / bearish / neutral`, but the classifier was trained on **generic English news**. It misfires on the language Indian markets actually speak in:

- **Regulator-speak**: "RBI in dovish hold", "MPC stays accommodative", "SEBI tightens MII norms"
- **Index shorthand**: "Nifty stalls at 24,500", "BankNifty drags 300pts", "Sensex melts up", "Mid-cap rout"
- **Budget / policy idioms**: "fiscal-deficit comfort", "capex cycle revival", "GST cess sunset"
- **Market mood phrases**: "muhurat pop", "FII selling pressure", "sideways grind", "halt-on-circuit"
- **Punjabi-Marathi-Gujarati transliterations** that retail press uses: "rakhi rally", "diwali muhurat"

Generic classifiers map these to *neutral* or *off-topic* and the alpha pipeline loses signal. The goal: a **lightweight, India-tuned sentiment model** that takes a headline + body and outputs:

```
{ sentiment: "bullish" | "bearish" | "neutral" | "sideways" | "detached",
  confidence: 0..1,
  vocab_hits: [{phrase, polarity, weight}, ...],   // explainable
  intensity: 0..10 }                                 // "budget-day-heart-attack" = 10
```

Plus a **sector roll-up** so the Sectors page shows real sentiment, not just signal counts.

---

## Architecture (lean, ship-able in two phases)

### Phase 1 — Lexicon-augmented zero-shot (1-2 days, no training)

A **lexicon + rules** layer that wraps the existing sentiment field. Cheap, no GPU, deterministic. Lives in [scraper/sentiment_india.py](scraper/sentiment_india.py).

- **Curated Indian-market lexicon** (~600 phrases) keyed to polarity + intensity:
  - Bullish: `re-rating, FII inflow, capex revival, beat estimates, order win, buyback announced, raised guidance, dividend hike, muhurat pop, breakout above`
  - Bearish: `de-rating, FII selling pressure, miss estimates, guidance cut, halt-on-circuit, distribution, bear hug, melt-down, rout, slipping below support`
  - Sideways: `range-bound, consolidation, choppy, narrow band, lacklustre, indecisive`
  - Detached: `decoupled, diverging from globals, immune to, ignoring, shrugging off`
  - Intensity modifiers: `marginal +1`, `sharp +3`, `historic +5`, `panic +5`, `crash +6`, `tank +6`, `nosedive +6`, `heart-attack +7`
- **Regex patterns** for numeric magnitudes: `(\d+(?:\.\d+)?)\s*(?:%|bps|pts|cr)` → maps size to intensity
- **Source-tier override**: filings (SEBI/RBI/BSE) get +1 confidence vs aggregator news.
- **Negation handling**: 3-word window before each lexicon hit; `not, never, no, without, denied, rejected` flips polarity.
- Outputs the unified shape above. Falls back to the existing sentiment field when no lexicon hits.

**Why phase 1 alone is shippable**: it's transparent (every classification cites which phrases drove it), zero infra cost, and immediately reduces "neutral when actually bearish" misses.

### Phase 2 — Fine-tuned classifier (1-2 weeks)

Once we have labelled data from Phase 1 in production, train a small transformer:

- **Base**: `ai4bharat/IndicBERT-v2` (12-layer, ~250M params, supports en-IN) — already pre-trained on Indian-language and Indian-English corpora.
- **Head**: 5-class softmax (bullish / bearish / neutral / sideways / detached) + intensity regression.
- **Training data**:
  - **Bootstrap (~30k labels)**: use Phase 1's lexicon classifier on the historical `events` table; treat high-confidence outputs as silver labels.
  - **Gold (~3k labels)**: hand-label a stratified sample (across regulator releases, RSS, social, filings) via a tiny Streamlit annotation UI.
  - Hold out 500 budget-day headlines as a regression test ("heart-attack day").
- **Loss**: cross-entropy on class + 0.3 × MSE on intensity (normalized).
- **Serving**: ONNX-export the fine-tuned model, run via `onnxruntime` (CPU, ~15ms per inference) — no GPU dep, no Groq budget consumed.
- Hosted next to existing scrapers in [scraper/sentiment_india.py](scraper/sentiment_india.py) behind the same `classify(text)` API so Phase 1 → Phase 2 is a drop-in swap.

---

## Data sources (already in the codebase)

- **Events table** in [scraper/database_schema.py](scraper/database_schema.py) — every ingested item already has `title`, `summary`, `source`, `sentiment` (generic), `companies`, `event_type`. Joining sector via [scraper/stock_universe.py](scraper/stock_universe.py) gives sector context for free.
- **Source-tier weights** from [scraper/source_tiering.py](scraper/source_tiering.py) — already feeds the alpha pipeline; reuse for sentiment confidence weighting.

No new scraping needed.

---

## Sector roll-up

A new backend endpoint `GET /api/sectors/sentiment` that:
1. Pulls last-N-hours of scored events from the `events` table.
2. Joins `companies → sector` from `stock_universe`.
3. For each sector, computes:
   ```
   bullish_share = bullish_events / total_events
   bearish_share = bearish_events / total_events
   intensity_avg = mean(intensity)
   net_score     = (bullish_share - bearish_share) * intensity_avg * 10   // -100..+100
   regime        = "risk-on"  if net_score > 30
                   "risk-off" if net_score < -30
                   "sideways" if |net_score| < 10 and intensity_avg < 4
                   "detached" if intensity_avg < 2  (few events; can't call)
                   else "mixed"
   ```
4. Returns `{sector, net_score, regime, bull, bear, total, top_drivers: [...], top_risks: [...]}` per sector — sorted by net_score desc.

This replaces the existing `/api/sectors` heat colours (which today use signal *counts* only, not sentiment) on the Newsroom sidebar heatmap and the Sectors page.

---

## UI surface

### A new "Mood" view on the existing **Sectors page** ([app/sectors.html](app/sectors.html))

Top: a single-glance **Market Mood meter** (compass-style gauge):
```
←—— Bearish ——— Sideways ——— Bullish ——→
                    ↑
                  +42  (Risk-on, light)
```
Sub-text: *"NIFTY +0.8% · 14 bullish drivers · 5 bearish risks · top mover: IT sector"*

Below: a **sector heat-strip** sorted by net_score, each cell shows:
- Sector name
- Net score (-100..+100)
- 3 top drivers as 1-line tooltips
- Sparkline of last 30 days' net_score (already have history via events table)

### A pill on every event card

The shared event-card already renders sentiment as a coloured side-rail. Phase 1 adds a `vocab_hits` tooltip on hover — clicking shows *"This was tagged bearish because: 'halt-on-circuit' (×2), 'guidance cut' (×1)"*. Makes the model auditable in the UI itself.

---

## Phased rollout

| Step | Deliverable | LOC | Time |
|---|---|---|---|
| 1 | [scraper/sentiment_india.py](scraper/sentiment_india.py) — lexicon + rules + negation | ~400 | 1d |
| 2 | Backfill historical events through Phase 1 | (script) | 0.5d |
| 3 | `/api/sectors/sentiment` endpoint + cache | ~120 | 0.5d |
| 4 | Market Mood compass on `sectors.html` | ~250 | 1d |
| 5 | Event-card `vocab_hits` tooltip | ~60 | 0.5d |
| 6 | Annotation UI for gold labels | (Streamlit) | 1d |
| 7 | IndicBERT fine-tune + ONNX export | — | 1w |
| 8 | Swap classify() implementation | ~20 | 0.5d |

**Stop after step 5 and ship.** Phase 2 only if measurable gap remains.

---

## Verification

- **Unit tests** for the lexicon: 100 labelled headlines covering each polarity, negation, and intensity tier; CI fails if accuracy drops below 85%.
- **Budget-day regression**: replay 500 budget-day 2020-2024 headlines; expected bearish-heavy intensity ≥ 6 on the Feb 1 close-of-session corpus.
- **A/B on alpha hit-rate**: keep the existing sentiment field; run both side-by-side for two weeks; promote the new one only if 5d hit-rate ≥ existing model.

---

## Files to create / touch

| File | Action |
|---|---|
| [scraper/sentiment_india.py](scraper/sentiment_india.py) | **NEW** — `classify(text, source=None) -> SentimentResult` |
| [scraper/sentiment_lexicon.json](scraper/sentiment_lexicon.json) | **NEW** — curated phrase → (polarity, weight) map |
| [scraper/hybrid_scraper.py](scraper/hybrid_scraper.py) | call new classifier after the existing one; persist `sentiment_intensity` + `vocab_hits` JSON on events table |
| [scraper/database_schema.py](scraper/database_schema.py) | add `sentiment_intensity REAL`, `vocab_hits TEXT` columns |
| [backend/api_v3.py](backend/api_v3.py) | new `/api/sectors/sentiment` endpoint with TTL cache |
| [app/sectors.html](app/sectors.html) | Market Mood compass + sector heat-strip |
| [app/shared/js/event-card.js](app/shared/js/event-card.js) | vocab_hits tooltip on hover |

No new dependencies for Phase 1. Phase 2 adds `onnxruntime` (CPU wheel, ~15MB).
