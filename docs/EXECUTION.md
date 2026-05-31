# Auto-Execution Platform

This document describes the execution stack that takes the system from
*detection-only* to a **safe, phased auto-execution** platform, and how to
operate each phase. It is intentionally gated: matching must be proven before
any money moves, and live trading is hard-capped and allowlist-only.

> **Default state is safe.** `EXECUTION_ENABLED=False` and
> `EXECUTION_MODE="paper"`. Nothing places a real order until both are flipped
> *and* the readiness checklist passes.

## Architecture

```
MarketAnalyzer ──detects──▶ opportunity ──▶ ArbitrageExecutor (thin arb orchestrator)
                                              │  builds 2 OrderRequests, fires both,
                                              │  partial-fill-aware confirmed-unwind hedge
                                              ▼
                              execution/  (general-purpose, arb-agnostic)
                              ├─ ExecutionEngine   kill switch + circuit breaker + idempotent retries
                              ├─ VenueGateway      uniform Kalshi/Polymarket: place/cancel/poll/fees/balance
                              ├─ OrderRequest/Outcome   venue-agnostic, deterministic client_order_id
                              ├─ KillSwitch        durable global halt (flag + sentinel file)
                              ├─ ExecutionCapture  estimate-vs-realized JSONL log
                              └─ OperatorControls  halt / resume / flatten_all / position_health
```

The `execution/` package knows nothing about arbitrage — any strategy can drive
it with `OrderRequest`s.

## Match verification (Phase A)

Lexical similarity is necessary but not sufficient. `kalshi_arbitrage/matching/`
adds a verification layer that runs after the similarity threshold:

- **OutcomePolarityVerifier** — resolves whether Kalshi-YES equals the
  Polymarket YES token or its complement (negation + threshold direction). The
  4-strategy selector swaps the PM YES/NO books when a pair is `inverted`.
- **ResolutionCriteriaVerifier** — rejects divergent close dates, incompatible
  thresholds, or divergent rules text.
- **AllowlistVerifier** — operator allow/deny override (the live gate).

### Backtesting the matcher

```bash
# 1. capture candidate pairs during a paper scan, label a sample true/false,
#    save to market_data/matching/labeled_pairs.jsonl  (see research/matching/dataset.py)
# 2. measure + gate:
python - <<'PY'
from kalshi_arbitrage.matching import CompositeVerifier, load_labeled_pairs
from research.matching.gate import MatchingGate
pairs = load_labeled_pairs("market_data/matching/labeled_pairs.jsonl")
print(MatchingGate().evaluate(CompositeVerifier(), pairs).summary())
PY
```

The gate requires the **lower bound** of a bootstrapped precision CI to clear
`min_precision` (default 0.99) and **polarity accuracy = 1.0**.

## Execution safety (Phase B)

- Partial-fill-aware hedge: computes the imbalance, unwinds exactly that size,
  **confirms the unwind filled**, and trips the kill switch + records residual
  exposure if it can't.
- Real Polymarket fees everywhere (no more hardcoded 0).
- RiskEngine pre-trade gate; live-only balance gate.
- Idempotent retries (stable `client_order_id`) and a per-venue circuit breaker.

## Paper validation (Phase C)

```bash
# run with EXECUTION_ENABLED=True, EXECUTION_MODE="paper" for N days, then:
python -m research.paper.analyze_paper_run market_data/executions/executions.jsonl
```

Reports estimated-vs-realized drift (with CI), hedge rate, unwind failures, and
**polarity-error count (must be 0)**.

## Live pilot (Phase D)

1. Provide live credentials (see `.env.example` — Polymarket needs
   `POLYMARKET_PRIVATE_KEY`).
2. Populate the allowlist: copy `market_data/matching/match_allowlist.example.json`
   to `match_allowlist.json` and add verified pairs.
3. Run the readiness gate:
   ```bash
   python -m research.pilot.live_readiness_checklist
   ```
4. Only if it prints `PASS`, set `EXECUTION_MODE="live"`. Caps:
   `LIVE_MAX_NOTIONAL_USD` (default $5/leg), `LIVE_MAX_CONCURRENT_POSITIONS=1`.

### Operator controls

```python
from kalshi_arbitrage.execution.operator import OperatorControls
op = OperatorControls()
op.halt("manual")             # trips durable kill switch
await op.flatten_all()        # cancel orders + market-unwind all positions
await op.position_health()    # report/alert on open exposure
op.resume()
```

A halt also persists as `market_data/EXECUTION_HALT`, so it survives a restart
until an operator resumes.
