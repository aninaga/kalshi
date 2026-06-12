# Plan: make the NBA research codebase natively agentic (`research/lab/` substrate)

## What I found

The research half (scope: **NBA research only**; the arb bot is untouched) has three
anti-agentic properties the user called out:

1. **Clone-a-script evaluators.** ~13 near-duplicate scripts — `totals_alpha.py`,
   `totals_walkforward.py`, `totals_realistic.py`, `spread_alpha.py`,
   `spread_walkforward.py`, `spread_realistic.py`, `calib_alpha.py`,
   `fairvalue_alpha.py`, `sub_alpha.py`, `term_structure_alpha.py`,
   `totals_extremes_alpha.py`, `clv_alpha.py`, … — each re-implements the SAME
   plumbing (`_load`/panel build, `build_trades` entry/exit/side, freshness guard,
   `_pm_taker_fee`, `evaluate_trial` wrapping, walk-forward). To test an idea an
   agent must clone one and hand-edit it. Several clones got execution WRONG (the
   0.50-fill artifact that falsely "certified" totals).
2. **Hard-coded idea discovery.** `agents/usage/edge_hunt_loop.py::DIRECTIONS` and
   `agents/orchestrator/run.py::DIRECTION_ROTATION` enumerate a fixed
   markets×mechanisms menu. Agents pick from the menu; they don't originate ideas.
3. **Prescriptive `.md`-prompt scaffolding + a frozen DSL.** `gpt55_edge_hunter_prompt.md`,
   `diagnostic_prompt.md`, `codex_worker_prompt.md`, `example_direction.md` + the
   AST-locked `StrategySpec`. An agent fills a template; it has no composable
   toolset to express an arbitrary hypothesis like a hedge-fund analyst would.

**The fix:** a `research/lab/` substrate — a composable, self-documenting toolkit
(data → signals → strategy → realistic execution → gate, plus a dynamic hypothesis
registry) that any deployed agent imports and freely composes to generate AND test
ideas, with honest execution + the gate built in by default. Replace the hard-coded
menus with a runtime hypothesis registry, and the prescriptive prompts with a
toolkit the agent is simply handed.

## Foundation (coordinator lays this on the base branch BEFORE spawning workers)

`research/lab/types.py` + `research/lab/__init__.py` + `research/lab/CONTRACT.md`:
the shared dataclasses every unit builds against, so the 15 worktrees develop in
parallel without depending on each other's branches:

- `Panel` — one game's per-minute frame for a market: `game_id, date, market,
  home_team, away_team, minute_ts[], elapsed_sec[], margin[], total[],
  mid[ (the 0.5-crossing implied level) ], ladder{strike->prob[]}, features{},
  home_won, split`. Plus `synthetic_panel(**knobs)` factory for fixtures.
- `Trade` / `Trades` — `game_id, date, primary_team, home_team, side, entry_ts,
  exit_ts, entry_price, exit_payoff, pnl` (+ a DataFrame view).
- `FillResult` — realistic fill output: `strike, fill_mid, half_spread, fee, net_cost`.
- `GateResult` — `passed, cents_per_contract, ci_lo, ci_hi, n, n_games,
  reasons[], walkforward{month->...}, adversarial{check->result}`.
- `Hypothesis` — `id, market, mechanism, signal_desc, direction, status
  (open/running/done), verdict, results{}, created, updated`.
- `CONTRACT.md` — the exact public signature of every `lab/*` module so workers
  agree on interfaces.

## Work units (15; each = new files under `research/lab/` + synthetic-fixture tests, additive)

1. **lab/data.py — Panel loaders.** Load cached pkls (winner/total/spread) into
   `Panel`s for a split; expose the strike ladder + features; reuse `nba_odds_study`
   + `splits.json`. Includes the `synthetic_panel` fixtures. Real-cache load is a
   guarded path (skipped when cache absent).
2. **lab/execution.py — realistic fills.** Port `totals_realistic` logic into a
   primitive: snap to the nearest LISTED strike, fill at its REAL quoted mid (never
   0.50), cross a half-spread, apply PM/Kalshi fees (mirror `realistic_fills` /
   `mock_execution.FeeModel`); `FillModel(half_spread, fee).cost(...)` + a sweep.
3. **lab/signals.py — composable signal primitives.** Pure functions over a `Panel`:
   pace projection, anchoring gap (proj−implied level), implied-level crossing,
   calibration bias, freshness/staleness age, rolling stats, lead/run features.
4. **lab/strategy.py — composable Strategy.** `Strategy(entry_fn, exit_fn, side_fn,
   sizing)` over `Panel`s → `Trades`, with honest i+1 latency + freshness guard
   built in. The open replacement for the frozen `StrategySpec` DSL.
5. **lab/evaluate.py — one-call evaluation.** `evaluate(trades, fill_model) ->
   GateResult`: wraps `scorer.promotion_gate` + cost sweep + monthly walk-forward +
   the 4 adversarial checks (staleness, estimator-bias-vs-baseline, concentration,
   OOS). Realistic `FillModel` is the default.
6. **lab/hypothesis.py — dynamic hypothesis registry.** Append-only JSONL store:
   `register/claim/update/query` `Hypothesis` rows; dedupe by mechanism hash. The
   runtime replacement for the hard-coded direction menus.
7. **lab/session.py — the Lab façade.** A `Lab` object tying it together ergonomically:
   `lab.load(market, split)`, `lab.strategy(...)`, `lab.evaluate(...)`,
   `lab.register(...)`, `lab.open_hypotheses()` — the single import an analyst uses.
8. **lab/cli.py — agent-facing entrypoint.** `python -m research.lab run <module>`,
   `... hypotheses [list|claim|new]`, `... eval <module>` — natively callable so an
   agent (or codex exec) drives the lab without bespoke scripts.
9. **lab/analyst.py — autonomous analyst runner.** Hands an agent the toolkit + the
   OPEN hypotheses from the registry + autonomy; logs trials; budget-aware via
   `agents/usage/limits`. Replaces `edge_hunt_loop`'s hard-coded loop with a
   registry-driven one.
10. **agents/workers/analyst_prompt.md (new) — the analyst brief.** Self-documenting,
    points at `lab/CONTRACT.md` + `lab/README.md`, grants real autonomy (originate a
    mechanism, compose signals, test, register findings) instead of a recipe;
    deprecation note on the old prescriptive prompts.
11. **Rewire discovery to the registry.** Modify `agents/usage/edge_hunt_loop.py` and
    `agents/orchestrator/run.py` to source directions from `lab.hypothesis` instead
    of `DIRECTIONS`/`DIRECTION_ROTATION` (the only unit that edits existing files;
    isolated to these two).
12. **lab/strategies/anchoring.py — anchoring family via substrate.** Re-express the
    totals + spreads anchoring strategies as `Strategy`s on the lab primitives with
    realistic execution by default (consolidates totals_alpha/walkforward/realistic +
    spread_*). Reproduces the known realistic numbers (coordinator-validated post-merge).
13. **lab/strategies/calibration.py — level-bias family via substrate.** calibration /
    favorite-longshot / totals-extremes as lab `Strategy`s (dead family) — regression demos.
14. **lab/strategies/reactions.py — reaction family via substrate.** sub-latency /
    fair-value-gap / term-structure / CLV as lab `Strategy`s (dead family) — regression demos.
15. **lab/README.md — the research-platform guide.** "How to be an analyst here": the
    toolkit API, composing a strategy, execution+gate built-in, registering a
    hypothesis. Supersedes the prescriptive worker `.md` prompts.

## E2E test recipe (per unit)

The data cache is gitignored and ABSENT in worktrees, so e2e is **synthetic-fixture
based** and runs anywhere:

1. `pytest <your unit's test files> -q` — must pass using ONLY `research.lab.types`
   + your own module + `synthetic_panel` fixtures. Mock any sibling `lab` module you
   call (siblings may not be merged in your worktree).
2. Compose-and-evaluate smoke: build a `synthetic_panel`, run a trivial `Strategy`
   (or your module's surface), and assert the call returns the right shape
   (`Trades`/`GateResult`) — no real data needed.
3. Run any pre-existing repo tests for files you touched (`pytest research/... -q`).

**Real-data validation is explicitly a COORDINATOR post-merge step** (reproduce
totals≈−1.0¢ / spreads≈+1.9¢ realistic on the real cache) — not a worker e2e,
because no worker has the cache.

## Worker instruction template (verbatim, adapted: coordinator-merge, no gh)

```
After you finish implementing the change:
1. Code review — Invoke the Skill tool with skill: "code-review"; fix any findings.
2. Unit tests — run `pytest <your test files> -q` (and any repo tests for files you
   touched). Fix failures. Tests must pass using only research.lab.types + your own
   module + synthetic fixtures (mock not-yet-merged sibling lab modules).
3. E2E — follow the coordinator's synthetic-fixture recipe above. (No real-data e2e:
   the data cache is absent in the worktree.)
4. Commit and push YOUR branch (do NOT open a PR; gh is unavailable and the
   coordinator merges branches into the feature branch). Use a clear message.
5. Report — end with exactly: `BRANCH: <your-branch-name>` (or `BRANCH: none — <reason>`).
```

Workers use `subagent_type: "general-purpose"`, `isolation: "worktree"`,
`run_in_background: true`, `model: "opus"`.

## Conventions workers must follow
- Scope = NBA research only; never touch the arb bot (`kalshi_arbitrage/`, root `*.py`).
- Import shared dataclasses from `research.lab.types` (already on the base branch).
- Realistic execution is the DEFAULT everywhere (no 0.50-fill assumptions — that
  artifact is the cautionary tale; see `research/TOTALS_REFINE_FINDINGS.md`).
- Match existing code style; keep modules small and pure; never weaken the gate;
  never read `--split test` / the test ids in `market_data/splits.json`.
