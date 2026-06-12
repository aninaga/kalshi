# Kalshi repo — two product lines under one roof

This repository holds **two independent products** that share only market-data
plumbing. Keep contributions on the correct side of the line.

1. **Cross-venue arbitrage machine** — `kalshi_arbitrage/` package + the root
   `arbitrage_analyzer.py` entry point. Finds genuine, fee-clearing
   Kalshi↔Polymarket arbitrage, holds basis-risk lookalikes for review, and can
   paper-trade (then live-trade, under hard gates) the survivors.
2. **Automated research fund** — `research/` (NBA quant studies + a strategy
   backtest harness) plus `ops/` (the always-on fund controller that conducts
   cloud-side analyst lanes). Self-contained.

**Separation rule:** `kalshi_arbitrage/` must NEVER import from `research/`.
The research side may use shared data utilities but stands alone.

Canonical operator runbooks live in `docs/`: **`docs/MACHINE.md`** (the arb
machine, incl. the critical legality/basis-risk/lockup caveats), **`docs/EXECUTION.md`**
(the gated-execution safety model), **`docs/NEXT_GEN.md`** (roadmap). Read
`docs/MACHINE.md` before risking any capital.

---

## Product line 1 — the arbitrage machine (`kalshi_arbitrage/`)

### What it actually does
The system is **NOT analysis-only.** It ships a full, gated execution stack.
Out of the box it runs the entire pipeline — detect → match-verify → price →
risk-check → build orders → **simulated fills** → PnL — but **no real order is
ever placed** by default. Real orders require three independent conditions to
all hold (see *Gated execution* below). Flipping a config flag is not enough.

### Core components
- `arbitrage_analyzer.py` (root) — original continuous/single scan entry point.
- `kalshi_arbitrage/cli.py` — the `kalshi-arb` command (installed by
  `pip install -e .`); wraps the analyzer plus all validation/arb tooling.
- `api_clients.py` — Kalshi + Polymarket REST clients (retry logic; every
  session sends a browser User-Agent — Polymarket's Cloudflare 403s the default
  Python UA, which silently starves the bot; guarded by
  `tests/test_user_agent_headers.py`).
- `market_analyzer.py` — detection/matching engine (`FAST`/`BALANCED`/`LOSSLESS`
  completeness levels).
- `live_probe.py` — the tested economics core: a complementary arb (own
  outcome-A + outcome-B across venues for **< $1**) is only real once it clears
  the Kalshi taker fee curve (maximal at mid-price, so durable edge lives at
  extreme prices).
- `matching/` — cross-venue match **verification** beyond lexical similarity:
  `OutcomePolarityVerifier` (Kalshi-YES vs PM YES-token-or-complement),
  `ResolutionCriteriaVerifier` (close dates / thresholds / rules), `AllowlistVerifier`
  (operator override = the live gate), optional `llm_tiebreaker` (needs
  `ANTHROPIC_API_KEY`).
- `execution/` — general-purpose, arb-agnostic order execution: `ExecutionEngine`
  (kill switch + circuit breaker + idempotent retries), `VenueGateway`
  (uniform place/cancel/poll/fees/balance), `SimulatedGateway` (paper fills with
  the real fee model, never calls a venue), `KillSwitch`, `OperatorControls`,
  `live_lock`.
- `arbitrage_executor.py` — thin arb orchestrator: builds the 2 legs, fires both,
  partial-fill-aware **confirmed-unwind** hedge (trips the kill switch + records
  residual exposure if an unwind can't be confirmed).
- `risk_engine.py`, `circuit_breaker.py`, `reconcile.py`, `confirmed_pnl.py`,
  `snapshot_store.py`, `ws_books.py` (live order-book mirror), `websocket_client.py`.
- `validation/` — arb validation tooling (lives WITH the bot, not in `research/`):
  `matching/` (matcher precision/recall backtest + gate), `paper/` (paper-run
  analysis), `pilot/` (live-pilot readiness checklist).

### Matching is required before any money moves
Lexical similarity is necessary but not sufficient. A candidate must clear the
verifiers, and the matching gate requires the **lower bound** of a bootstrapped
precision CI to clear `min_precision` (default 0.99) **and** polarity accuracy
= 1.0 before the matcher is trusted live.

### Fees — `venue_fees.py` is canonical (golden-tested)
The root `venue_fees.py` module is the single source of truth for both products,
guarded by `tests/test_venue_fees_golden.py`. Use it; never hardcode a fee.
- **Kalshi taker:** `ceil_cents(0.07 · C · P · (1−P))` per contract; maker = a
  quarter of that, and **only** on `quadratic_with_maker_fees` series (e.g. NBA).
- **Polymarket taker:** per-category parabolic `C · (bps/10000) · P · (1−P)`
  (sports 300bps, crypto 700bps, geopolitics 0; makers $0 + rebate).
- **There is no flat "Polymarket ~2%" fee** — it never existed in the official
  schedule and over-charges the extreme-priced legs where durable arb lives.
  Do NOT reintroduce it.

### Gated execution — three conditions for a REAL order
A real order reaches a venue only when ALL hold:
1. `Config.EXECUTION_ENABLED` is `True`  (default `True` — executor runs)
2. `Config.EXECUTION_MODE == "live"`     (default `"paper"`)
3. the **live-trading lock is ARMED**    (`execution/live_lock.py`)

Until then, paper mode swaps in `SimulatedGateway`; even in live mode the real
gateways refuse to POST (`live_trading_locked`) until armed. Arming is
deliberately awkward — it writes `market_data/LIVE_TRADING_ARMED` containing the
phrase `I_HAVE_VALIDATED_AND_ACCEPT_REAL_MONEY_RISK`. Live caps are tiny by
default: `LIVE_MAX_NOTIONAL_USD = 5.0`/leg, `LIVE_MAX_CONCURRENT_POSITIONS = 1`.

```bash
python -m kalshi_arbitrage.execution.live_lock status|arm|disarm
# or via the CLI:  kalshi-arb live status|arm|disarm
```

### Config defaults that matter (`kalshi_arbitrage/config.py`)
| Key | Default | Note |
|---|---|---|
| `MIN_PROFIT_THRESHOLD` | `0.02` | min profit margin |
| `SIMILARITY_THRESHOLD` | `0.78` | lexical match floor (verifiers run after) |
| `SCAN_INTERVAL_SECONDS` | `30` | continuous-mode poll |
| `EXECUTION_ENABLED` | `True` | executor runs (paper unless live+armed) |
| `EXECUTION_MODE` | `"paper"` | `"paper"` simulate \| `"live"` real, lock-gated |
| `LIVE_MAX_NOTIONAL_USD` | `5.0` | hard per-leg clamp in live mode |
| `LIVE_MAX_CONCURRENT_POSITIONS` | `1` | live concurrency clamp |

### Honest expectation
Real cross-venue arb on identical contracts is small, illiquid, capital-locked,
and latency-insensitive — dominated by basis risk (the two venues use
differently-governed oracles and demonstrably resolve the same event
differently) and capital lockup. See `docs/MACHINE.md` for the full caveats and
the legality constraint for US persons. Infra's value is 24/7 coverage and
capital recycling, not speed.

### `kalshi-arb` CLI (subcommands)
`doctor` · `scan` · `analyze-paper` · `diagnose` · `backtest` · `readiness` ·
`reconcile` · `live {status,arm,disarm}` · `find-arb` · `monitor` ·
`backtest-arb` · `analyze-ledger` · `machine` · `review`. See `docs/MACHINE.md`
for the per-command table.

---

## Product line 2 — the research fund (`research/` + `ops/`)

`research/` is the NBA quantitative-research and strategy-backtesting effort.

| Path | Purpose |
|---|---|
| `research/nba_odds_study/` | NBA data ingestion + analysis package |
| `research/harness/` | strategy backtester — replay, realistic fills, cost profiles (uses `venue_fees`) |
| `research/scorer/`, `research/promotion/` | statistical gates (bootstrap CIs) + promotion pipeline |
| `research/registry/`, `research/lake/` | result registry (SQLite) + historical data lake (parquet/DuckDB) |
| `research/agents/` | research worker pool (codex workers, Claude reviewer) |
| `research/scripts/` | study CLIs — run as modules from the repo root |

```bash
python -m research.scripts.analyze_nba_game
python -m research.scripts.study_players
```

`ops/` is the **fund controller** — `ops/controller.py` is the single always-on
process of the desk. It runs a localhost dashboard and owns every fund process
(analyst lanes, nightly data top-ups, the sleep-blocker) via one RUN/PAUSE state
machine. Models are cloud-side; this box only conducts. State lives outside
iCloud paths (`~/.kalshi_fund`); code lives in the repo. `ops/governor.py`
(staffing policy), `ops/usage_meter.py`/`usage_profile.py` (budget), and
`ops/APEX_PROTOCOL.md` (the orchestrator's self-optimization protocol) round it
out.

---

## Directory map (top level)

```
kalshi/
├── arbitrage_analyzer.py          # ARB — original scan entry point
├── analyze_price_discrepancies.py # ARB — helper scripts
├── simple_price_check.py
├── venue_fees.py                  # CANONICAL fee schedule (shared, golden-tested)
├── kalshi_arbitrage/              # ARB — core package (see product line 1)
│   ├── matching/  execution/  validation/   # verify · order stack · arb validation
│   └── ...
├── research/                      # FUND — NBA quant research + backtest harness
│   ├── nba_odds_study/  harness/  scorer/  promotion/  registry/  lake/  agents/  scripts/
├── ops/                           # FUND — always-on controller, governor, usage meter
├── tests/                         # ARB test suite (pytest; async via pytest-asyncio)
├── tools/                         # misc analysis/arb CLIs (find_live_arb, monitor_arb, …)
├── deploy/                        # Docker + systemd shadow-deploy
├── docs/                          # MACHINE.md · EXECUTION.md · NEXT_GEN.md
└── market_data/                   # local data storage (logs, captures, allowlists, ledgers)
```

---

## Working in this repo
- **Python:** use the repo venv at `~/code/kvenv/bin/python3`. Never a Desktop
  `.venv` (iCloud dataless-`.pyc` import hang).
- **Tests:** `pytest` from the repo root; async tests via `pytest-asyncio`.
- **Fees:** always go through `venue_fees.py`. Never reintroduce a flat PM fee.
- **Safety:** no order fires without the three live conditions above. Do not weaken
  the live lock, the kill switch, or the caps.
- **Separation:** keep `kalshi_arbitrage/` free of any `research/` import.
