# Research direction

Worker ID: a3f1b2c4
Direction index: 0042
Direction theme: halftime small-deficit bounce

## Background

Phase −1's P4 finding showed that buying the trailing team at halftime when
the deficit is 1-3 points has a +3.8c mean gross PnL at zero cost, but the
CI lower bound is -2.3c. The Phase −1 evaluation used a flat 2.5c cost; the
harness's `realistic_fills.py` now applies Kalshi/Polymarket fee formulas
giving roughly 3c round-trip. The hypothesis is fragile — it cleared the gate
on the cheap-cost cell but not the expensive one.

## Hypothesis to explore

Would a tighter deficit band (e.g., 2-3 points only, dropping 1-point games
which may be noise) recover the gross signal under the realistic cost model?
Or would a longer holding window (10 minutes vs 4 minutes) let the bounce
play out further? Either variant is in scope; pick ONE.

## Suggested constraints

- Suggested feature set: `margin`, `pm_implied_wp`.
- Suggested side: `long_trailing`.
- Suggested venue: `polymarket`.
- Suggested sizing: `fixed_contracts`, value 5, max 100.

## Decision constraints

- You may explore variants of P4. **Do not repeat the exact spec** that lives
  at `research/experiments/manual/p4_halftime_1to3.py` — the registry will
  dedupe by spec_hash and your trial will be marked redundant.
- `entry_condition` must reference `elapsed_game_sec` to lock the entry
  window. Otherwise you'll fire on every bar where the margin condition
  holds, which is not the hypothesis.

## Your identity

- worker_id: `a3f1b2c4`
- agent_id (use this exact string for `--agent-id`): `codex:a3f1b2c4`

When you call `run_backtest`, the `--agent-id` argument is
`codex:a3f1b2c4`. The orchestrator uses this to attribute the trial to you
in the registry.
