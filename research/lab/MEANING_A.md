# Meaning A: the research factory runs itself

"Getting the automated hedge fund up and running" splits into two very different
things. **Meaning A** is *run the falsification factory unattended*: originate →
prioritize → test → judge, on a cadence, cheaply, with no human babysitting the
loop. **Meaning B** is *deploy capital* — which stays blocked on a surviving
edge, a live in-game data feed, and execution infra, and gated behind the
human-only test-split burn. This document is Meaning A; it does **not** trade.

Before this slice the loop *worked when watched* but had three gaps. Each is now
closed:

## 1. The real executor (the brain is wired in)

`research.lab.analyst.run_analyst` is the deterministic harness; its `executor`
seam was a `_noop_executor` stub. `research/lab/executors.py` provides the real
one:

```python
from research.lab.executors import make_codex_executor
executor = make_codex_executor(model="gpt-5.5", effort="high")
```

It hands one assignment to a live agent via `run_codex_exec` (the same
`codex exec` subprocess the edge-hunt loop uses), harvests the agent's
`result.md` JSON verdict contract, and maps it onto the registry vocabulary.
**Honesty contract:** a failed/empty spawn or an out-of-vocabulary verdict
NEVER becomes a fabricated `DEAD`/`PROMOTE` — the idea is left `open` (re-tryable)
or `running`. The `runner` is injectable, so a headless `claude -p` or a mock
plugs in at the exact same boundary.

## 2. The ledger defaults ON (governance is N-aware in production)

`run_analyst(..., ledger_path=...)` now appends every settled trial to a shared
JSONL ledger (`DEFAULT_LEDGER = research/reports/alpha/ledger.jsonl`), and the
CLI defaults it on. That ledger is what `lab.director` ranks against and what
`lab.governance` dedupes by `hypothesis_id` to count the Deflated-Sharpe `N`. So
as the factory throws more darts, the multiple-testing hurdle rises
automatically — no more cold-start `N=1` placeholder in a live run. (Pass
`--ledger ''` to disable; tests leave it off so they never write to the repo.)

## 3. The cadence (`research/lab/heartbeat.py`)

The clock that fires passes:

```bash
# one pass, then exit — let cron / GitHub Actions own the cadence (CI default):
python -m research.lab.heartbeat --once

# resident: a pass every hour until idle/tapped or --max-passes:
python -m research.lab.heartbeat --interval 3600 --max-passes 24
```

It is budget-aware (the analyst polls `agents.usage.limits`), persists state
across `--once` invocations (`heartbeat_state.json`), and **stands down** rather
than spin when the gpt-5.5 windows are tapped or the open pool is exhausted
(`--stop-after-idle`). `sleep` and the `executor` are injectable, so the loop is
deterministically testable without waiting or spawning.

### Scheduling it

`.github/workflows/research-heartbeat.yml` runs `--once` on a 6-hour cron, but is
**opt-in**: the scheduled job only fires when the repo variable
`RESEARCH_HEARTBEAT_ENABLED == 'true'`, so merging it never silently starts
spawning agents. `workflow_dispatch` lets you fire a pass manually. Without a
live agent on the runner a pass no-ops (`processed=0`) and exits 0. For an
always-on box, run the resident `--interval` mode under your own
supervisor/systemd with `codex` on PATH instead.

## What it still does NOT do

It does not place a single order. Every pass that finds a candidate surfaces it
as a `PROMOTE` for **human** review; the test-split burn (5 lifetime) and any
capital decision remain human-gated. The factory's job is to keep honestly
trying to falsify market efficiency — and to tell you the moment something
survives.

## Tests

`tests/test_executors.py` (8) — verdict parsing/validation, no-fabrication on
spawn failure, injectable runner. `tests/test_heartbeat.py` (6) — `--once`,
`--max-passes`, idle stand-down, cross-pass state persistence. Plus the existing
`tests/test_analyst.py` contract is unchanged.
