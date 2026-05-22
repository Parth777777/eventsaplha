# PRD — Geopolitical Intelligence Map (Trader Edition)

**Status:** v1.0 (MVP shipped)
**Owner:** Tickwave
**Last updated:** 2026-05-16

---

## 1. Problem

Indian traders react to global shocks (Israel-Iran flare-up, OPEC cut, Red Sea
attacks, Russia sanctions, Taiwan semiconductor halt, Fed pivot) — but the
existing tools all dump these as text news with no spatial / causal context.
A trader who holds **ONGC** or **GAIL** doesn't immediately know *Hormuz traffic
is down 40%* until the move has already happened. Conversely, a benign-looking
event in Vienna (OPEC meeting) might be the single most-important catalyst for
the energy sector that week.

The **Map** turns geopolitical and macro events into a *spatial intelligence
surface*: where the event happened, what flows through that region, which Indian
sectors and stocks have exposure, and what the live alpha is.

## 2. Goals

1. **Spatial situational awareness** — every active geopolitical event plotted
   on a world map with severity + recency cues.
2. **Sector exposure mapping** — clicking a region surfaces the Indian
   sectors / tickers whose alpha is statistically tied to that location.
3. **Flow lanes** — visualise critical commodity routes (oil, LNG, copper, rare
   earths) with live disruption status.
4. **Click → drill in** — region click opens a side panel with: recent news,
   affected stocks (alpha-graded), commodity price moves, currency stress.
5. **No-context-switching** — trader never leaves the page to check the news
   that's moving their book.

## 3. Non-goals

- Full GIS analytics (route optimisation, distance calculations).
- Real-time vessel-level AIS data (out of scope for v1; v2 candidate via
  Marine Traffic API).
- War-game / scenario simulation tools.
- Drawing / annotation tools.

## 4. Users

- **Retail Indian trader** holding 5-20 names with sectoral concentration —
  needs to spot tail risk fast.
- **Discretionary fund analyst** screening macro setups before opening a
  position.
- **Quant** wanting a quick overlay of geopolitical regime on positions.

## 5. User stories

1. *As a trader watching ONGC, I want to see live disruption risk on Hormuz
   so I can sit tight / size up before the move.*
2. *As an analyst, I want to click on China and see every Indian ticker with
   meaningful supply-chain or revenue exposure to China, sorted by alpha.*
3. *As a long-equity holder, I want a global heat map of which regions are
   stressed today so I can rotate sector exposure.*
4. *As a swing trader, I want to filter the map by horizon (1D / 5D / 20D)
   so I see only events likely to play out on my timeframe.*

## 6. MVP feature set (this PR)

| Feature | Status |
|---|---|
| World map (Leaflet + dark tiles) embedded in `/map.html` | ✅ |
| 12+ seeded hotspots: Hormuz, Suez, Red Sea, Russia, Ukraine, China, Taiwan, OPEC HQ, US Fed, ECB, Israel-Iran, North Korea | ✅ |
| Severity-colored markers (low / medium / high / critical) | ✅ |
| Click marker → side panel: title, body, severity, last update, exposed sectors, exposed tickers (with live alpha) | ✅ |
| Flow lanes — Strait of Hormuz, Suez, Cape route, Trans-Siberian | ✅ |
| `/api/geo/map/hotspots`, `/api/geo/map/flows`, `/api/geo/map/exposure/<region>` | ✅ |
| Sidebar entry "Map" | ✅ |
| Refresh every 60s | ✅ |

## 7. V2 (next, post-MVP)

- **Live commodity prices overlay** (Brent / WTI / gold / copper / LNG)
  pinned to producing regions.
- **Currency stress overlay** — USD/INR, EUR, RUB, CNY pinned to capitals.
- **Central bank meeting calendar** — countdown badges on Fed/ECB/BoJ/RBI.
- **Event timeline scrubber** — drag a slider to see hotspots at any moment
  in the last 30 days.
- **Watchlist mode** — only highlight hotspots affecting MY positions.
- **Alert subscription** — "ping me if Hormuz severity ≥ HIGH".

## 8. V3 (long-term)

- **Shipping AIS** — real-time tanker positions on flow lanes (paid API).
- **Refinery capacity heatmap** (gulf refineries, Jamnagar).
- **Election / political calendar** with mapped tickers (e.g. PSU defensives
  pre-election).
- **Replay mode** — pick a date, see the map as it was then + how positions
  played out (training tool).
- **AI narrative** — Groq summary of "what changed on the map today".

---

## 9. **What makes this *actually* useful for a trader** (the user's ask)

Below is the spec for the differentiating features — what separates this map
from a generic "news on a globe" gimmick:

### 9.1 Oil flow lane intelligence

For each major route (Hormuz, Suez, Cape of Good Hope, Bab-el-Mandeb, Malacca):
- **Daily flow rate** (mbpd estimate from OPEC + EIA monthly + scrape).
- **Disruption multiplier** — `flow_today / 90d avg`. <0.6 → flag as serious.
- **Indian impact map** — `ONGC, RIL, IOC, BPCL, HPCL, GAIL, IGL, MGL` each
  with an `exposure_pct` field (revenue/production share affected).
- **News attribution** — every flow change shows the specific event that
  caused it.

### 9.2 Commodity choke risk

- Each chokepoint has a `risk_score 0-100` updated daily from:
  - News intensity (Tickwave alpha scores for region keywords)
  - Insurance war-risk premium (Lloyd's published weekly)
  - Maritime incident count (last 14d)
- Indian sector mapping pre-wired:
  - **ENERGY** → Hormuz, Strait of Malacca
  - **METALS** → Australia / Russia / China rail
  - **PHARMA** → China API imports
  - **IT** → US/EU customer regions, undersea cable map
  - **AUTO** → Taiwan (semis), Germany (premium platforms)

### 9.3 Central-bank countdown badges

Pinned to Washington / Frankfurt / Tokyo / Mumbai / Beijing:
- Days until next rate decision
- Consensus expected delta
- Probability distribution (from rate futures)
- Indian sectors most rate-sensitive (REALESTATE, NBFC, AUTO)

### 9.4 Currency stress map

Plot currency markers at capitals with colour from 5d change:
- USD/INR — affects IT (positive), PHARMA (positive), OIL importers (neg)
- EUR/USD — affects export competitiveness
- JPY/USD — carry trade unwind risk
- CNY/USD — global EM proxy

### 9.5 Geopolitical regime classifier

Auto-tagged daily based on hotspot severity sum:
- `CALM`     → factor weights tilt toward fundamentals
- `STRESSED` → tilt toward defensives + gold + dollar
- `CRISIS`   → tilt toward energy, defence, safe-haven

The classifier output feeds into the existing `quant_layer.FactorOverlay`
`regime` parameter so portfolio weights auto-adjust on rotation days.

### 9.6 Sector rotation cues

When `regime` transitions (e.g. CALM → STRESSED on a Hormuz incident), the map
shows a translucent "rotation cue" panel:
> "Hormuz risk ↑ HIGH. Suggested rotation: cut PHARMA/IT exports, add ONGC,
> ICICIBANK (PSU pref), GOLDBEES, defence (HAL, BEL)."

This is the moment that justifies the feature — most retail users miss this
window entirely.

### 9.7 Smart-money positioning hint

Each hotspot side panel shows:
- **Bulk-deal activity** in exposed tickers in the last 7d
- **F&O OI buildup** (long / short) for the affected sector futures
- **FII/DII flow** the day after similar past events (historical analogues)

### 9.8 Historical analogue lookup

When a hotspot fires, surface "last time this happened":
- 2019 Saudi Aramco attack → ONGC +4%, RIL +2.8% in 3d
- 2022 Russia invasion → metals +18% in 1mo, IT -7% in 2w
- 2023 Red Sea attacks → JSL container surcharge cycle

Gives the trader a base-rate intuition without scrolling through old news.

### 9.9 Re-allocation simulator

Hovering a hotspot shows a tiny inline simulation:
> "If you hold [TCS 30%, INFY 20%, RELIANCE 25%, HDFCBANK 15%, ITC 10%],
> and Hormuz disruption persists 1 week, est. portfolio drag is -2.4%."

Computed from sector betas + the hotspot's pre-mapped sector deltas.

### 9.10 Open-ended overlay layers

Toggleable layers (right-side legend panel):
- ✅ Hotspots
- ✅ Flow lanes
- ⬜ Commodity prices
- ⬜ Currency stress
- ⬜ Central bank calendar
- ⬜ Watchlist filter
- ⬜ Earnings calendar
- ⬜ Historical analogues

Each layer is a separate API endpoint so adding more later is trivial.

---

## 10. Tech notes

- **Map library:** Leaflet (~140KB) + a free dark-tile provider (CARTO dark).
  No GIS server; static JSON for geometries.
- **Coordinate system:** OSM Web Mercator (EPSG:3857) — leaflet default.
- **Data refresh:** 60s poll for hotspots; flows update on 5min interval.
- **Persistence:** existing `geo_events` table + new `geo_hotspots` static
  config in code (regions don't change daily).
- **Performance:** marker clustering not needed below ~50 hotspots.
- **Mobile:** map collapses to a top half / side panel bottom half on narrow
  viewports.

## 11. API surface

```
GET /api/geo/map/hotspots          → list of active hotspots
GET /api/geo/map/flows             → list of commodity flow lanes
GET /api/geo/map/exposure/<region> → sectors + tickers exposed, alpha-graded
GET /api/geo/india                 → (existing) India-domestic geo signals
```

## 12. Success metrics

- **Engagement:** % of DAU clicking ≥ 1 marker → target 35%.
- **Retention:** trader returns to map on ≥ 3 of 5 trading days.
- **Conversion:** % of users who add a stock to watchlist *from* the map
  side panel → target 12%.
- **Alpha link:** correlation of map-surfaced tickers with subsequent 5-day
  abnormal return → target ρ ≥ 0.15 (vs ρ ≈ 0.04 for a generic news scroll).

---

## 13. Out-of-scope risks

- Reliance on free OSM tiles — could rate-limit; cache aggressively or
  pre-bundle a coarse static tileset.
- Leaflet adds ~140KB; keep the rest of the page lean.
- Severity scoring is heuristic in v1; users may disagree with how high a
  hotspot is graded. V2 lets users tune the weights.
