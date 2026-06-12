# Kalshi repo — arbitrage machine + research fund

This repository holds **two independent products** that share market-data
plumbing but are otherwise separate:

1. **Cross-venue arbitrage machine** (`kalshi_arbitrage/` + root
   `arbitrage_analyzer.py`) — finds genuine, fee-clearing Kalshi↔Polymarket
   arbitrage, holds basis-risk lookalikes for review, and can paper-trade (then
   live-trade, under hard gates) the survivors.
2. **Automated research fund** (`research/` + `ops/`) — an NBA quantitative
   research / strategy-backtesting effort plus an always-on controller that
   conducts cloud-side analyst lanes.

> **Separation rule:** `kalshi_arbitrage/` must never import from `research/`.

The operator runbooks are in `docs/`: **[`docs/MACHINE.md`](docs/MACHINE.md)**
(the arb machine, incl. the critical legality / basis-risk / capital-lockup
caveats — read this before risking capital), **[`docs/EXECUTION.md`](docs/EXECUTION.md)**
(the gated-execution safety model), **[`docs/NEXT_GEN.md`](docs/NEXT_GEN.md)**
(roadmap).

---

## The arbitrage machine

### It is not analysis-only — it has a full, gated execution stack
Out of the box (`EXECUTION_ENABLED=True`, `EXECUTION_MODE="paper"`) a run does
**everything except place a real order**: detect → match-verify → price →
risk-check → build orders → **simulated fills** → PnL capture. A real order
reaches a venue only when all three independent conditions hold — flipping a
config flag is **not** enough:

1. `Config.EXECUTION_ENABLED` is `True`   (default `True`)
2. `Config.EXECUTION_MODE == "live"`      (default `"paper"`)
3. the **live-trading lock is ARMED**     (`execution/live_lock.py`)

In paper mode a `SimulatedGateway` fills orders locally with the real fee model
but never calls a venue API; in live mode the real gateways refuse to POST
(`live_trading_locked`) until the lock is armed. Live caps are tiny by default
(`LIVE_MAX_NOTIONAL_USD=5`/leg, `LIVE_MAX_CONCURRENT_POSITIONS=1`).

### Install (local)

```bash
git clone <repo> && cd kalshi
# main is the working branch (research + arb lines merged 2026-06-12)

python -m venv .venv && source .venv/bin/activate   # recommended
pip install -e .                                  # installs the `kalshi-arb` command
```

`pip install -e .` is all you need to run a paper scan. Optional extras:
`".[dev]"` (tests), `".[viz]"` (plots), `".[research]"` (parquet/duckdb).

### Quick start — everything runs through `kalshi-arb`

```bash
kalshi-arb doctor            # confirm both venues are reachable (the PM UA/403 gotcha)
kalshi-arb scan              # full paper pipeline: detect → match → verify → price →
                             #   simulated fills → PnL   (NO real orders)
kalshi-arb find-arb          # discover → bucket genuine / held-for-review / rejected → allowlist
kalshi-arb monitor           # continuous fee-aware capture; records gap episodes to a ledger
kalshi-arb analyze-ledger    # summarize a paper session (net, episode/duration mix, per-market)
kalshi-arb analyze-paper     # summarize a captured paper run (drift, hedges)
kalshi-arb diagnose matches  # inspect why matches are/aren't found
kalshi-arb backtest          # matching precision/recall gate (needs labels)
kalshi-arb review            # walk held-for-review pairs → allow/deny decisions
kalshi-arb readiness         # live-pilot go/no-go checklist
kalshi-arb live status       # show / arm / disarm the live-trading lock
```

> **Safe by default.** `kalshi-arb scan` runs the *entire* pipeline including the
> execution engine, but fills are **simulated** — no real order is ever placed.

The original `python arbitrage_analyzer.py ...` entry point still works; the CLI
wraps it plus the validation tools.

```bash
python arbitrage_analyzer.py --help
  --mode {single,continuous}                 (default: continuous)
  --interval SECONDS                         (default: 30)
  --threshold DECIMAL                        min profit (default: 0.02)
  --similarity DECIMAL                       lexical match floor (default: 0.78)
  --completeness {FAST,BALANCED,LOSSLESS}
  --realtime                                 enable WebSocket streaming
```

### Going live (deliberate, gated — only after validation)

```bash
kalshi-arb readiness                 # must print PASS
kalshi-arb live arm                  # writes the arm token (deliberate, explicit)
EXECUTION_MODE=live kalshi-arb scan  # real orders now permitted, hard-capped
kalshi-arb live disarm               # lock it back down
```

Arming writes `market_data/LIVE_TRADING_ARMED` containing the phrase
`I_HAVE_VALIDATED_AND_ACCEPT_REAL_MONEY_RISK`. See
[`docs/EXECUTION.md`](docs/EXECUTION.md) for the full safety model and the
operator controls (halt / resume / flatten-all).

### Matching is required before any money moves
Lexical similarity (`SIMILARITY_THRESHOLD = 0.78`) is necessary but not
sufficient. `kalshi_arbitrage/matching/` adds verification: outcome polarity
(Kalshi-YES vs the PM YES-token-or-complement), resolution criteria (close dates
/ thresholds / rules), and an operator allowlist (the live gate). The matching
gate requires a bootstrapped precision-CI lower bound ≥ 0.99 **and** polarity
accuracy = 1.0 before the matcher is trusted.

### Fees — `venue_fees.py` is canonical (golden-tested)
The root `venue_fees.py` is the single source of truth (guarded by
`tests/test_venue_fees_golden.py`), used by both products. Never hardcode a fee.
- **Kalshi taker:** `ceil_cents(0.07·C·P·(1−P))` per contract; maker = a quarter
  of that, only on `quadratic_with_maker_fees` series (e.g. NBA).
- **Polymarket taker:** per-category parabolic `C·(bps/10000)·P·(1−P)`
  (sports 300bps, crypto 700bps, geopolitics 0; makers $0 + rebate).
- There is **no** flat "Polymarket ~2%" fee — it never existed in the official
  schedule and over-charges the extreme-priced legs where durable arb lives.

### Honest expectation
Real cross-venue arb on identical contracts is small, illiquid, capital-locked,
and latency-insensitive. It is dominated by **basis risk** (Kalshi and Polymarket
use differently-governed oracles and demonstrably resolve the same event
differently) and **capital lockup** (profit is locked from entry to resolution).
For US persons there is also a **legality constraint** on the liquid global
Polymarket book. Read [`docs/MACHINE.md`](docs/MACHINE.md) before funding
anything — its caveats override the gross-profitability story.

### Authentication
Copy the template and (optionally) add Kalshi credentials for the live WebSocket
feed:

```bash
cp .env.example .env
# KALSHI_API_KEY=your_access_key_id
# KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi_private_key.pem  (RSA-PSS)
# POLYMARKET_PRIVATE_KEY=...   (only needed for the live pilot)
```

Without these the client runs **REST-only**: public REST market data still works,
but there is no live Kalshi WebSocket feed. Secrets live in a local `.env`, never
committed.

---

## The research fund (`research/` + `ops/`)

`research/` is the NBA quantitative-research / strategy-backtesting project. It is
self-contained and never imported by the arb bot.

| Path | Purpose |
|------|---------|
| `research/nba_odds_study/` | NBA data ingestion + analysis package |
| `research/harness/` | strategy backtester — replay, realistic fills, cost profiles (uses `venue_fees`) |
| `research/scorer/`, `research/promotion/` | statistical gates (bootstrap CIs) + promotion pipeline |
| `research/registry/`, `research/lake/` | result registry (SQLite) + data lake (parquet/DuckDB) |
| `research/agents/` | research worker pool (codex workers, Claude reviewer) |
| `research/scripts/` | study CLIs — run as modules from the repo root |

```bash
python -m research.scripts.analyze_nba_game
python -m research.scripts.study_players
```

`ops/` is the **fund controller** — `ops/controller.py` is the single always-on
process of the desk (a localhost dashboard + one RUN/PAUSE state machine that
owns every fund process). Models are cloud-side; this box only conducts. See
`ops/APEX_PROTOCOL.md`.

---

## Project structure

```
kalshi/
├── arbitrage_analyzer.py          # ARB — original scan entry point
├── analyze_price_discrepancies.py # ARB — helper scripts
├── simple_price_check.py
├── venue_fees.py                  # CANONICAL fee schedule (shared, golden-tested)
├── kalshi_arbitrage/              # ARB — core package
│   ├── api_clients.py             #   Kalshi + Polymarket REST clients
│   ├── market_analyzer.py         #   detection / matching engine
│   ├── live_probe.py              #   tested economics core (< $1 complementary arb)
│   ├── matching/                  #   cross-venue match verification (polarity, criteria, allowlist)
│   ├── execution/                 #   general-purpose order stack (gateways, engine, kill switch, live_lock)
│   ├── arbitrage_executor.py      #   arb orchestration (2-leg + confirmed-unwind hedge)
│   ├── validation/                #   arb validation tooling
│   │   ├── matching/              #     matcher precision/recall backtest + gate
│   │   ├── paper/                 #     paper-run analysis
│   │   └── pilot/                 #     live-pilot readiness checklist
│   ├── risk_engine.py · circuit_breaker.py · reconcile.py · confirmed_pnl.py
│   ├── ws_books.py · websocket_client.py · config.py · utils.py
│   └── ...
├── research/                      # FUND — NBA quant research + backtest harness (independent)
│   ├── nba_odds_study/  harness/  scorer/  promotion/  registry/  lake/  agents/  scripts/
├── ops/                           # FUND — always-on controller, governor, usage meter
├── tests/                         # ARB test suite (pytest; async via pytest-asyncio)
├── tools/                         # misc analysis/arb CLIs (find_live_arb, monitor_arb, …)
├── deploy/                        # Docker + systemd shadow-deploy
├── docs/                          # MACHINE.md · EXECUTION.md · NEXT_GEN.md
└── market_data/                   # local data storage (logs, captures, allowlists, ledgers)
```

## How the arb pipeline works

1. **Market capture** — fetch active markets from both venues (paginated; Gamma
   top-up keeps extreme-priced PM markets discoverable).
2. **Match + verify** — lexical similarity, then polarity / resolution-criteria /
   allowlist verifiers strip out false matches.
3. **Price + economics** — `live_probe` checks each candidate clears the Kalshi
   taker fee curve at real book depth.
4. **Bucket** — genuine (auto-allowlistable) / held-for-review (uncertain
   resolution) / rejected (>15% edge ⇒ false match or stale leg).
5. **Execute** — simulated fills by default; real orders only under the three
   gates above, with a partial-fill-aware confirmed-unwind hedge.
6. **Persist** — episodes, captures, and PnL to local ledgers under `market_data/`.

## Development & testing

```bash
~/code/kvenv/bin/python3 -m pytest tests/ -q     # arb test suite (async via pytest-asyncio)
```

- Use the repo venv `~/code/kvenv/bin/python3` (a Desktop `.venv` hits an iCloud
  dataless-`.pyc` import hang).
- All fee math goes through `venue_fees.py`.
- Do not weaken the live lock, the kill switch, or the live caps.
