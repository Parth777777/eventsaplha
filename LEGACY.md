# LEGACY · TickerWave v1

This repo (`eventsaplha`) is the **v1 archive** of TickerWave. Active development has moved
to a separate repo, `tickerwave-v2`, which preserves and reuses every backend module
from here while replacing the frontend with a Next.js + PWA experience.

## Recover the v1 site

Two equivalent ways to bring this version back online, fully working:

### Option A — Check out the immutable tag (recommended for reference)
```bash
git clone https://github.com/Parth777777/eventsaplha.git
cd eventsaplha
git checkout v1-classic
pip install -r requirements.txt
RUN_SCHEDULER=false python backend/api.py
# → http://localhost:5000
```

### Option B — Check out the archive branch (use this if you want to patch v1)
```bash
git clone -b legacy-v1 https://github.com/Parth777777/eventsaplha.git
cd eventsaplha
pip install -r requirements.txt
RUN_SCHEDULER=false python backend/api.py
# → http://localhost:5000
```

The `legacy-v1` branch tracks the same commit as `v1-classic` but is mutable —
push hotfixes there if you ever need to keep v1 alive in production.

## What's preserved

Everything that was in `main` at commit
[`0db4b02`](https://github.com/Parth777777/eventsaplha/commit/0db4b02) on the date of
the v2 pivot, including:

- **40 page surfaces** (Home, Events, Earnings, IPO, M&A, F&O, Stock, Map, Forensics,
  Screeners, Compare, Portfolio, Paper, Simulator, Watchlist, Alerts, Pricing,
  Quant Playground, Methodology, Research, API Docs, etc.)
- **Quant Playground** — Black-Scholes pricer, full Greeks, payoff builder (12 strategy
  templates), Monte Carlo with animated Brownian-motion paths, Kelly sizing, portfolio
  analytics (Sharpe / Sortino / Calmar / drawdown / beta / correlation heatmap),
  event-window backtester, ~45-term searchable glossary, AI coach drawer
- **Site-wide premium design pass** — strict slate palette, mint/rose semantic-only
  colours, no-glow no-pulse aesthetic, shared motion engine (fade-up, layout
  smooth-expand, number flash + count-up), restrained typography
- **Backend** — Flask APIs (`backend/api.py`, `api_v2.py`, `api_v3.py`, `api_ext.py`,
  `api_public.py`), event clustering (`scraper/cluster_persister.py`), causal map
  (`scraper/causal_map.py`), forensic scorer (`scraper/forensics/forensic_scorer.py`),
  geo-intelligence (`backend/geo_map.py`), social tiering
  (`scraper/social/publish_priority.py`), F&O signals (`scraper/fo_signals.py`),
  event backtest engine (`backend/event_backtest.py`)

## What's next

Continuing development is on `tickerwave-v2` — a Next.js 14 + PWA frontend that
talks to a vendored, extended copy of this Flask backend. v2 doesn't delete any v1
feature; every page here is **reincarnated** as a layer of the new hub-feed /
journal / FAFO-learning / multi-channel-alerts experience. See the v2 repo's
`README.md` for the reincarnation map.

## Rules

- **`v1-classic` is immutable.** Never re-point or delete this tag.
- **`legacy-v1` is mutable.** OK to patch if v1 needs to keep running in prod.
- **`main` may diverge.** This repo's `main` may eventually receive v1 cleanup
  commits (LEGACY.md updates, dependency bumps, security patches) without affecting
  `v1-classic`.

Pivot commit: [`0db4b02`](https://github.com/Parth777777/eventsaplha/commit/0db4b02) · Pivot tag: `v1-classic`
