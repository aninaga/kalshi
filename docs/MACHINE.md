# Running the cross-venue arbitrage machine

This is the operator runbook. The machine finds genuine, risk-free, fee-clearing
Kalshi↔Polymarket arbitrage, holds the basis-risk lookalikes for review, and can
paper-trade (then live-trade) the survivors under existing safety gates.

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

## What latency does and doesn't buy

The genuine edges persist minutes-to-hours, so a $100/mo always-on instance with
WebSocket feeds captures essentially all of them — sub-second/co-located hardware
adds ~nothing. Infra's real value is **24/7 coverage** (catch transient sports
gaps the moment they open) and **capital recycling**, not speed.
