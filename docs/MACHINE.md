# Running the cross-venue arbitrage machine

> ## ⚠️ CRITICAL — read before risking any capital (mid-2026, web-verified)
>
> An adversarial multi-source review surfaced three issues that override the
> profitability story. None are code bugs; all are real-world constraints.
>
> **1. Legality / geo-access (can invalidate the whole premise for a US person).**
> The deep-liquidity *global* Polymarket book this bot trades (`clob.polymarket.com`,
> self-custodied USDC on Polygon) **remains geoblocked to US persons.** Reaching it
> via VPN/non-US wallet violates Polymarket's ToS and risks **frozen funds and
> enforcement action** (a US soldier was DOJ-charged after exactly this access
> pattern). Since ~Dec 2025 there IS a legal US path — **Polymarket US / QCX LLC**,
> a CFTC-regulated DCM — but it is a *separate, walled* product (USD via FCMs, full
> KYC, no self-custodied wallet) whose contracts/liquidity do **not** mirror the
> global book. **A single US-resident identity cannot legitimately run both legs**
> (Kalshi US-KYC on one side + the liquid *global* Polymarket on the other). Confirm
> your own legal standing before funding anything.
>
> **2. Basis risk is the binding constraint, not a footnote.** Kalshi (centralized,
> CFTC source-agency rules) and Polymarket (UMA token-vote oracle) **demonstrably
> resolve the same event differently** — e.g. the Feb-2026 Super Bowl halftime
> market settled YES on Polymarket ($1) but ~$0.26 on Kalshi; the Zelensky-suit and
> Ukraine-minerals markets saw multi-day UMA disputes/manipulation; **1,150+
> disputed Polymarket markets in 2026 alone.** This divergence is *worst* in the
> long-dated, subjective markets the bot favors (arrests, "Musk trillionaire",
> elections). A "<$1 → $1" pair across two differently-governed oracles is really a
> **bet that they agree** — and they regularly don't. Treat the `uncertain`
> (held-for-review) flag as a hard gate, and confirm BOTH venues' resolution rules
> match word-for-word before allowlisting anything.
>
> **3. Capital lockup crushes the return.** Profit on a months-out market is locked
> from entry to resolution. A "2% guaranteed" edge held ~4–7 months is only
> **~3.5–6% annualized** before fees — and one divergent resolution (issue 2) can
> turn the whole position negative. The honest expected value, after basis risk and
> lockup, is far thinner than the gross capturable, and can be negative.
>
> **4. Fee model (minor, flagged not fixed).** Polymarket's official current
> taker fee is per-category *parabolic* (`contracts × feeRate × P(1−P)`, feeRate
> 0–0.072), not the flat-2%-of-notional the code uses. The code's model is the
> wrong shape — it modestly *under-charges* the low-priced legs where edges live
> (so it slightly overstates capturable, ~a few %) and over-charges high-priced
> legs. Left as-is (it's tested + conservative-in-aggregate) pending a coordinated
> change to `FeeModel.polymarket_taker_fee` + the research harness + `test_arb_ws5`.
>
> **Bottom line:** the *code* is sound and the gross capturable is real, but the
> *strategy* is gated by legality (issue 1) and dominated by basis risk (issue 2).
> The shadow run measures gross capturable at real depth; it does NOT price in
> resolution divergence or your legal access. Do not treat its numbers as net,
> risk-free, or realizable until issues 1–2 are resolved for your situation.

This is the operator runbook. The machine finds genuine, fee-clearing
Kalshi↔Polymarket arbitrage candidates, holds the basis-risk lookalikes for
review, and can paper-trade (then live-trade) the survivors under existing safety
gates — subject to the critical caveats above.

**Honest expectation first.** Real cross-venue arb on identical contracts is
small, illiquid, capital-locked, and latency-insensitive. The realistic envelope
is **~$15–40k/year on ~$20–50k of working capital** (see the analysis in the
session history) — dominated by a handful of extreme-priced election/threshold
markets plus transient sports-market gaps during marquee events. The dashboard's
six-figure "edge" is false matches + basis risk; the machine's job is to strip
that out and surface only what's real.

## The one core idea

Every surface shares one tested economics core (`kalshi_arbitrage/live_probe.py`):
a complementary arb (own outcome-A + outcome-B across venues for **< $1**) is only
real once it clears the **Kalshi taker fee curve `0.07·P·(1−P)`** — which is
maximal mid-price and eats most gaps, so durable edge lives at extreme prices.

## Commands (all via `kalshi-arb`)

| Command | What it does |
|---|---|
| `kalshi-arb doctor` | Confirm both venues are reachable (the UA/403 gotcha). |
| `kalshi-arb find-arb --output allow.json` | Discover → bucket **genuine / held-for-review / rejected** → write allowlist. |
| `kalshi-arb backtest-arb --days 7` | What was capturable over the past N days (upper bound), with PM-fee sensitivity. |
| `kalshi-arb monitor --ledger paper.jsonl --duration 86400` | Continuous fee-aware capture; records each gap **episode** to a JSONL ledger. |
| `kalshi-arb monitor --execute ...` | Same, but routes each opening gap through the executor (paper unless live+armed). |
| `kalshi-arb analyze-ledger --path paper.jsonl --session-hours 24` | Summarize a paper session: net, episode/duration mix, per-market, run-rate. |
| `kalshi-arb machine --monitor --duration 86400 --ledger paper.jsonl` | One command: discover → allowlist → paper-harvest. |
| `kalshi-arb live status\|arm\|disarm` | The live-trading lock (only after validation). |

## Three buckets, and why

- **Genuine (auto-allowlistable):** same event, deadlines congruent, no
  definitional carve-out, fee-clearing, plausible (<15%) edge. These are written
  to the allowlist.
- **Held for review:** plausible edge but *uncertain resolution* — a different
  "arrested" definition (Polymarket excludes "indictment without arrest", counts
  house arrest), or a time-anchor the rules can't pin. **Confirm both venues
  resolve identically before allowlisting.** (Enable the LLM tiebreaker —
  `ANTHROPIC_API_KEY` — to auto-adjudicate these.)
- **Rejected:** edge > 15% ⇒ a false match (different proposition/threshold) or a
  stale leg. Never arbitrage.

## Going live (deliberate, gated)

1. **Validate in paper.** Run `monitor --ledger` (or `machine --monitor`) for a
   real session; `analyze-ledger` it. Confirm captures are real and the
   estimated capture is sane.
2. **Fund both venues** (~$20–50k; Kalshi API + RSA key, Polymarket
   `POLYMARKET_PRIVATE_KEY`). Secrets go in a local `.env`, never committed.
3. **Review the allowlist** by hand — especially every held-for-review market.
4. `kalshi-arb readiness` → only on PASS, `kalshi-arb live arm`, set
   `EXECUTION_MODE=live`, caps tiny (`LIVE_MAX_NOTIONAL_USD=5`,
   `LIVE_MAX_CONCURRENT_POSITIONS=1`). Reconcile fills, then scale slowly.

## Catalog coverage gotcha (fixed, but know it)

Polymarket's CLOB `/sampling-markets` only lists markets in the liquidity-rewards
sampling set and **sheds near-resolved extreme-priced markets** — exactly where
durable arb lives. Observed live (2026-06-09): the Musk-trillionaire pair — the
largest standing clean edge — vanished from the catalog when PM hit 97.75¢ while
the cross-venue gap was still ~8% gross at depth. Two defenses are now built in:

1. `fetch_polymarket()` tops the catalog up from the Gamma API (top-volume active
   markets), so extreme-priced markets stay discoverable.
2. `monitor --watchlist-cache <file>` persists every verified pair and keeps
   watching it until its **Kalshi market actually closes**, even if both catalogs
   forget it (survives restarts; allowlist removals still win).

The shadow deploy (`deploy/run_shadow.sh`) passes the cache by default
(`SHADOW_WATCHLIST_CACHE`).

## What latency does and doesn't buy

The genuine edges persist minutes-to-hours, so a $100/mo always-on instance with
WebSocket feeds captures essentially all of them — sub-second/co-located hardware
adds ~nothing. Infra's real value is **24/7 coverage** (catch transient sports
gaps the moment they open) and **capital recycling**, not speed.
