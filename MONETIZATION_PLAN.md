# Tickwave Monetization Roadmap — Long-Term Plan

## Context

Tickwave (AlphaEvent-Trad) has 50+ shipped features, partial tier scaffolding, and zero revenue. The user wants a comprehensive long-term roadmap covering **all four** revenue streams while staying SEBI-unregistered (data-only, no buy/sell calls, no performance claims).

**Constraints driving the plan:**
- Founder feedback: "app is too complex, simplify FTUE." Implication: monetize what's built; don't add new features unless they're the wedge.
- SEBI: cannot recommend stocks, set targets, or claim returns. Must position as a *data tool*.
- Indian market price anchor: Tickertape ₹2,399/yr (~₹200/mo effective). Pro must come in below this.
- Existing scaffolding: `TIER_LIMITS` matrix at [api_v3.py:1555-1582](backend/api_v3.py#L1555), `@require_tier` decorator at line 1617, `/api/tier` endpoint at line 1638, payment stub at line 1650.
- **Bug in existing scaffolding:** `require_tier` at line 1625 hardcodes `current == "pro"` — won't accept future Team-tier users through Pro gates. Must change to ordered hierarchy.

**Outcome we're driving toward:** clean Pro tier shipping in 4-6 weeks, ₹25L-50L Year-1 non-subscription revenue layered on top, no SEBI exposure.

---

## 1. Subscription Tier Strategy

### Free — "Tickwave Lite"
*Real-time NSE/BSE intelligence, free forever.*

| Feature | Limit |
|---|---|
| Watchlist | 20 symbols (down from 50 — scarcity) |
| Smart alerts | 3 active |
| Screener | 50 rows, no CSV |
| Stock detail | Price/Chart/News only |
| Premover | Top-5 preview (already implemented) |
| Policy events | Last 24h |
| Paper trading | 1 portfolio (full functionality — never gate the wow loop) |
| Social, IPO, sector heatmap, fundamentals lite, alpha feed | Open |
| F&O | OI only, no unusual activity |

### Pro — "Tickwave Pro" (THE WEDGE)
*Pre-market edge: see what's moving before the bell.*

**Pricing:** ₹299/mo or ₹2,499/yr (30% annual discount). Founder-member pricing ₹1,999/yr for first 1,000 users.

| Feature | Limit |
|---|---|
| **Premover** | **Full ranked list (100) + 8:30am email/push digest** ← primary wedge |
| Smart alerts | 50 active |
| Watchlist | 1,000 symbols |
| Screener | 500 rows + CSV export |
| Stock detail | Financials + Ratios + Shareholding tabs unlocked |
| F&O | Unusual activity feed |
| Commodity × macro matrix | Unlocked |
| Global spillover | Unlocked |
| Policy events | 30-day window |
| Paper trading | 5 portfolios |
| Forensics | Manipulation flags + auditor-changes |

**Why people upgrade:** F&O traders + active retail check premarket movers daily 8:30-9:15. Free shows 5; Pro shows 100 with score, reason, sector, plus a delivered digest. Highest-frequency repeat trigger in the app. Stock detail tab gating is the secondary close — anyone researching hits a paywall daily.

### Team — "Tickwave Team"
*Same Pro, shared across a desk.*

**Pricing:** ₹999/mo or ₹8,999/yr for 5 seats (~₹200/seat/mo).

All Pro features + shared watchlist/alert namespace + admin seat management. **Defer until Pro has 100+ paying users** — don't build in v1.

---

## 2. Subscription Enforcement Plan (priority order)

**Week 1 — highest leverage, lowest effort. All edits in [api_v3.py](backend/api_v3.py):**

1. **Fix the tier decorator bug** at line 1617-1635 — replace `current == "pro"` with ordered hierarchy `{"free":0, "pro":1, "team":2}` so Team passes Pro gates.
2. **Premover full list gate** at line 2026 — already has `pre_mover_preview` truncation logic; verify it's slicing correctly.
3. **Stock detail tabs** — wrap `/api/stock/<t>/financials`, `/ratios`, `/shareholding`, `/corp-actions` with `@require_tier("pro")`. Frontend [stock.html](app/stock.html) reads `/api/tier` and renders lock overlay on gated tabs.
4. **CSV export** — gate `/api/screener/run?export=csv` with the existing `screener_export_csv` flag.
5. **Watchlist size** — enforce on POST `/api/watchlist/add`, return 402 with `upgrade_url` at the boundary.
6. **Alerts active count** — enforce in [smart_alerts.py](backend/smart_alerts.py) `create_alert` against `TIER_LIMITS[tier]['alerts_active']`.

**Week 2:**
7. F&O unusual activity (`/api/fo/unusual`) — apply `@require_tier("pro")`.
8. Commodity macro matrix endpoint.
9. Global spillover endpoint.
10. Policy fast window — clamp `?hours` server-side to `policy_fast_window`.

**Defer:** Realtime SSE cadence differentiation (low perceived value, complex). Per-feature trial logic (keep binary free/pro).

---

## 3. Razorpay Payment Integration

**Use Razorpay, NOT Stripe.** Razorpay has native UPI/AutoPay/Mandate for recurring INR, GST invoicing, INR settlement. Stripe India is restricted for SaaS at retail price points.

**New module:** `backend/payments_razorpay.py` (sibling to [api_ext.py](backend/api_ext.py)).

**Endpoints:**
- `POST /api/tier/upgrade` (replaces 501 stub at line 1650): create Razorpay subscription, return `subscription_id` + checkout key.
- `POST /api/razorpay/webhook`: verify HMAC signature, flip `users.tier`, write `subscriptions` row.

**New table** in [database_schema.py](scraper/database_schema.py):
```
subscriptions(user_id, provider, provider_sub_id, plan, status, current_period_end, created_at, canceled_at)
```

**Subscription state model:** `status ∈ {trialing, active, past_due, canceled, expired}`. `active|trialing` → Pro. `past_due` → 7-day grace. `canceled` → access until `current_period_end`. `expired` → free.

**SEBI guard:** add disclaimer to [pricing.html](app/pricing.html) — "Data tool, not investment advice. We do not provide buy/sell recommendations."

---

## 4. Stream 1 — Upstox AP Affiliate (ship FIRST, before billing)

**Why first:** Zero infra debt. Payouts hit before billing is built. Pure attribution work.

**Why Upstox specifically:** Post-Aug-2024 SEBI rule capped Zerodha referral rewards at 300 points; Upstox AP partnership still pays ₹400-700/funded account because they onboard partners as APs.

### Implementation
1. **New table** `broker_referrals(user_id, click_ts, broker, ref_code, utm_source, kyc_status, funded_status, payout_inr)` in [database_schema.py](scraper/database_schema.py).
2. **New blueprint** `backend/api_broker.py` registered in [api.py](backend/api.py) near line 2124:
   - `GET /api/broker/upstox/click` (optional auth) — log row, 302 to `https://upstox.com/open-account/?f=<AP_CODE>&utm=tickwave_<placement>`.
   - `POST /api/broker/reconcile` (admin token) — paste weekly Upstox AP CSV, mark accounts funded.
3. **UI placements** (priority order):
   - Primary: [premover.html](app/premover.html) — single CTA card "Don't have a broker? Open free Upstox account" below score table. Highest action-intent cohort.
   - Secondary: [onboarding.html](app/onboarding.html) — optional "Connect broker" step with Upstox primary + skip.
   - Tertiary: [portfolio.html](app/portfolio.html) empty-state — "Import holdings — start with Upstox".
   - **Avoid:** stock detail (looks like a buy nudge), alerts (looks like signals).
4. **Compliance copy** (mandatory on every CTA): "Open a demat account. Tickwave does not provide trading advice. Brokerage and charges as per Upstox." Footer disclosure on policy page: "Tickwave earns a referral fee from Upstox for accounts opened via our links."

### Year-1 revenue (10K MAU)
1.5-3% click-to-funded × ₹500 avg payout = **₹7L-15L/year**. Floor ₹4L if onboarding placement underperforms.

### Risk
SEBI Feb-2026 rule blocks registered entities (Upstox) from associating with platforms making "advice/return claims." Audit `app/` for "X% returns", "buy zone", "target" copy before launching. Generic brand sponsorship is the carve-out.

### Dependencies
None. Ships standalone before subscriptions.

---

## 5. Stream 2 — B2B API / White-label (ship AFTER Pro launches)

**Highest standalone B2B value (in order):**
1. `GET /api/premover` ([api_v3.py:2026](backend/api_v3.py#L2026)) — premover scores are unique IP; quants/PMS desks pay for ranked candidates.
2. `GET /api/fo/unusual` ([api_v3.py:121](backend/api_v3.py#L121)) — algo shops want raw F&O unusual activity.
3. `GET /api/forensics/manipulation` + `/api/forensics/auditor-changes` — forensics bundle for fund compliance teams.

**Skip alpha signals from B2B** — naming alone reads as "advice" and creates SEBI exposure for the buyer.

### Implementation
1. **API-key auth layer** — new `backend/api_keys.py`:
   - Table: `api_keys(key_hash, org_id, tier, monthly_quota, calls_this_month, rate_per_min, created_at, revoked_at)`. Store SHA-256 hash, never raw.
   - Decorator `require_api_key(scope='premover')` parallel to existing `require_auth` in [api.py:125](backend/api.py#L125). Reads `X-API-Key` header, falls through to JWT if absent (UI keeps working).
2. **Tiered rate limiter** — extend `_RL_HITS` machinery at [api.py:69-72](backend/api.py#L69) to key on `org_id` when API key present, with per-tier `RL_MAX_HITS`.
3. **Pricing** — flat MRR tiers (Indian B2B finance buyers hate metered billing):
   - Starter: ₹25K/mo, 10K calls/day, 1 endpoint
   - Growth: ₹75K/mo, 100K calls/day, all 3 endpoints
   - Enterprise: ₹2.5L/mo, unlimited + SLA + bulk daily CSV drop
4. **Docs** — single `app/api-docs.html` with Swagger UI + hand-written OpenAPI YAML. Email-issued keys for first 10 customers; defer self-serve portal.

### Year-1 revenue
3-6 paying logos × ₹50K avg MRR = **₹18L-36L/year**. Realistic if you have any quant/PMS network; ₹0 if cold-outbound only.

### Risk
B2B compliance teams may insist on a "factual market data, not research" sign-off. Keep response schemas strictly numeric/factual — no `verdict` or `recommendation` fields.

### Dependencies
Reuses `subscriptions` table from Razorpay work. Ship 4-8 weeks after retail subscriptions go live.

---

## 6. Stream 3 — Educational Content (ship LAST)

### Compliant content formats
- "How to read F&O OI buildup"
- "Reading bulk-deal disclosures"
- "What auditor changes signal"
- "Sector rotation framework"
- "F&O margin mechanics"

**Forbidden:** anything stock-specific in titles, backtested return claims, anything titled "How I made X%".

### Implementation
1. New [academy.html](app/academy.html) + markdown content rendered server-side.
2. New blueprint `backend/api_academy.py`:
   - `GET /api/academy/lessons`
   - `GET /api/academy/lesson/<slug>`
   - `POST /api/academy/progress` (require_auth)
3. **Distribution:** YouTube (free, top-of-funnel for Stream 1 affiliate clicks) + in-app academy (Pro-tier gated for advanced modules).
4. **Monetization:** **bundle into Pro**, NOT a separate paid course. Standalone paid courses with a fintech brand attract SEBI Research Analyst regulator scrutiny on the boundary.

### Year-1 revenue
**₹0-2L direct.** Real value is conversion lift on Stream 1 affiliate (educated users open broker accounts at 2-3× rate) and Pro retention.

### Risk
Single lesson titled "When to buy X" or "How to pick winners" tips you into Investment Advisor territory. Editorial checklist: no tickers in titles, no forward-looking claims, no performance numbers.

### Dependencies
Ships after Pro tier exists (needs gating).

---

## 7. Sequencing

| Phase | Weeks | Work | Revenue impact |
|---|---|---|---|
| 1 | 1-3 | Stream 1 (Upstox affiliate) — independent | ₹50K-1L/mo by week 12 |
| 2 | 4-8 | Subscription tier billing (Razorpay + Pro gates) | First Pro signups |
| 3 | 9-14 | Stream 2 (B2B API + tiered rate limiter) | First B2B logo |
| 4 | 15+ | Stream 3 (academy) | Conversion lift |
| 5 | When Pro >100 paid | Team tier | New segment |

**Combined Year-1 realistic range at 10K MAU: ₹25L-50L total revenue.**

---

## 8. Critical Files

**Subscription tier (Phase 2):**
- [backend/api_v3.py](backend/api_v3.py) — `TIER_LIMITS` (1555-1582), `_user_tier` (1599-1614), `require_tier` decorator (1617-1635, **fix hierarchy**), `/api/tier/upgrade` stub (1650-1658), premover endpoint (2026), endpoints to gate
- [backend/smart_alerts.py](backend/smart_alerts.py) — enforce `alerts_active` in `create_alert`
- [backend/payments_razorpay.py](backend/payments_razorpay.py) — **new** module
- [scraper/database_schema.py](scraper/database_schema.py) — add `subscriptions` table
- [app/pricing.html](app/pricing.html) — Razorpay checkout button + SEBI disclaimer
- [app/stock.html](app/stock.html) — lock overlay on gated tabs

**Broker affiliate (Phase 1):**
- [backend/api_broker.py](backend/api_broker.py) — **new** blueprint
- [backend/api.py](backend/api.py) — register blueprint near line 2124
- [scraper/database_schema.py](scraper/database_schema.py) — add `broker_referrals` table
- [app/premover.html](app/premover.html) — primary CTA placement
- [app/onboarding.html](app/onboarding.html) — secondary CTA

**B2B API (Phase 3):**
- [backend/api_keys.py](backend/api_keys.py) — **new** module (key hashing, `require_api_key`)
- [backend/api.py](backend/api.py) — extend rate limiter (line 69-72) to key on `org_id`
- [app/api-docs.html](app/api-docs.html) — **new** Swagger UI page

**Academy (Phase 4):**
- [backend/api_academy.py](backend/api_academy.py) — **new** blueprint
- [app/academy.html](app/academy.html) — **new** page

---

## 9. Verification

**Phase 1 (Upstox affiliate):**
- Click `/api/broker/upstox/click?utm=premover` → confirm 302 to Upstox with AP code, row inserted in `broker_referrals`.
- Reconcile a sample CSV → verify `funded_status` flips, payout calculated.
- Audit all `app/*.html` for forbidden copy (`return`, `target`, `buy zone`).

**Phase 2 (Subscriptions):**
- Free user hits `/api/stock/RELIANCE/financials` → 402 with `upgrade_url`.
- Free user hits `/api/premover` → response has `tier_gated: true`, data array length = 5.
- Pro user same call → full 100-row response, `tier_gated: false`.
- Razorpay test mode: complete checkout → webhook fires → `users.tier='pro'`, `subscriptions` row written.
- Cancel subscription → `current_period_end` retained, downgrade fires on cron after expiry.
- Apply tier hierarchy fix and confirm a `team` user passes `@require_tier("pro")`.

**Phase 3 (B2B API):**
- Issue test API key → curl `/api/premover` with `X-API-Key` → 200, rate limit applies per `org_id`.
- Exceed `monthly_quota` → 429 with quota-reset header.
- Web UI still works without `X-API-Key` (JWT fallback intact).

**Phase 4 (Academy):**
- Editorial checklist: title scan for tickers/return claims/forward-looking verbs.
- Free vs Pro: lesson list returns differently based on `_user_tier()`.

---

## 10. Open Questions (deferred decisions)

These don't block the roadmap but should be revisited at each phase boundary:
- **Trial length:** 7 days CC-required (~31% trial→paid) vs 14 days no-CC (~9% but bigger top-of-funnel). Pick at Phase 2 launch.
- **Annual upsell timing:** show annual after 30 days on monthly, or at signup? Indian retail prefers annual when discounted ≥30%.
- **Failed-payment grace period:** 7 vs 14 days. Default 7; revisit if churn data shows otherwise.
- **B2B trial:** offer 30-day free API key trial or paid-only from Day 1? Defer to Phase 3.
