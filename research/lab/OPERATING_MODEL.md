# Operating model: a model-agnostic substrate, driven from chat

The research stack is deliberately split in two:

1. **The substrate** (`research/lab/`) — a clean, deterministic, **model-agnostic**
   toolkit. It originates nothing and calls no model. It just lets *whoever holds
   it* go from data to a gated verdict:
   `Panel → signals → Strategy → realistic execution → evaluate → GateResult`,
   with a hypothesis registry, an N-aware Deflated-Sharpe governance layer, a
   leakage auditor, and a shared trial ledger.

2. **The orchestrator** — that's the **operator chat**. There is no Python
   agent-spawning apparatus anymore. The operator (a Claude session) spins up
   **Claude Opus 4.8 subagents** with the `Agent` tool, points them at the
   substrate, and keeps the stack alive with the `Monitor` tool.

This replaced a large `codex exec` / dual-dispatcher / GPT-5.5-budget-governor
apparatus (`agents/orchestrator/`, `agents/usage/`, `agents/foreman.py`,
`lab/executors.py`) that spawned agents *from Python*. Under "drive from chat"
that whole layer was redundant, so it was deleted. What remains is genuinely
model-agnostic: the only place a model enters is an **injected seam**.

## The seams (where a model plugs in)

Every agentic boundary is a callable with a model-blind contract and a
**deterministic default** (no model baked in):

| Seam | Default (model-agnostic) | Injected agent does |
|------|--------------------------|---------------------|
| `scout.propose(proposer=…)` | `default_proposer` → `[]` (originates nothing) | reads EDA + `scout_prompt.md`, returns `{market, mechanism, signal_desc, direction}` dicts |
| `director.select(ranker=…)` | `default_ranker` → stable novelty order | ranks the open pool by EV × novelty × evidence using `director_prompt.md` + the ledger |
| `analyst.run_analyst(executor=…)` | `_noop_executor` (prepares the assignment, runs nothing) | works one hypothesis to a verdict via the lab toolkit, honoring `analyst_prompt.md` |
| `data_agent.fulfill(provider=…)` | `default_provider` (derive from existing operators only) | sources/derives a requested feature; external asks → `NEEDS_DATA`, never fabricated |

Inject a callable and it works. With nothing injected the loop is a deterministic
no-op — it never invents ideas or fabricates verdicts.

## Event-class families (beyond NBA)

The data layer is a fifth seam: `research/lab/providers/` is a registry of
**MarketDataProvider** implementations, one per event-class *family*
(`nba`, `weather`, …). A provider owns its markets (plain strings), its cache,
and its own locked `splits.json` (TEST never surfaced unless asked). The
public `data.load_panels/load_panel/available` API is unchanged — the market
string resolves the family. `Panel.duration_sec` carries each family's event
clock (`None` = untimed → pace-style signals are NaN and never fire; the
generic families — calibration, staleness/reactions, term-structure,
cross-market — apply everywhere).

The **family is also the governance partition**: `lab.governance` computes the
Deflated-Sharpe `N` / `V[SR]` per family (pass `family=` to
`evaluate`/`governance_params`), so darts thrown at one event class do not
raise the hurdle for another. Records whose family is undeterminable count
toward every family — partitioning can exclude only what provably belongs
elsewhere, so it never weakens any hurdle. The analyst stamps
`family` + `market` on every ledger row.

Onboarding a new family = one provider module (enumerate events → build
Panels with a REAL quote ladder) + a locked splits file. See
`providers/weather.py` (Kalshi daily-high-temperature, the first non-NBA
vertical: bucket book → cumulative boundary ladder with measured two-sided
spreads) and `providers/_kalshi_fetch.py` (the generic series-agnostic
Kalshi historical fetcher).

## How the operator runs it from chat

1. **Originate.** Spawn an Opus subagent with `scout_prompt.md` + the EDA report;
   register what it returns. (Or call `scout.propose(proposer=<agent-call>)`.)
2. **Prioritize.** Spawn (or be) the director: rank the open pool against the
   ledger via `director.select(ranker=…)`.
3. **Work.** For each chosen hypothesis, spawn an Opus analyst subagent that
   imports `research.lab`, composes a strategy, runs the gate with realistic
   execution, and reports a verdict. `run_analyst(executor=<spawn>)` is the
   harness around that; it appends each settled trial to the shared ledger so
   `lab.governance` keeps the Deflated-Sharpe `N` honest.
4. **Keep it alive.** Use the `Monitor` tool to watch the stack and re-spawn /
   re-kick as needed. `lab/heartbeat.py` is an optional pure-Python cadence for
   an always-on box (it too takes an injected `executor`).

## Non-negotiables (unchanged)

Realistic execution by default (never a 0.50 fill); honest i+1 latency; the gate
is the judge and is never weakened; never read the test split. The factory's job
is to **falsify** — every survivor surfaces as a `PROMOTE` candidate for human
review. It places no orders: capital and the test-split burn stay human-gated.
