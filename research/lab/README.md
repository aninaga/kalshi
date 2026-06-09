# `research/lab/` — how to be an analyst here

This is the NBA research substrate: a small, composable toolkit you import to
**originate and test** trading ideas, with honest execution and the promotion
gate built in by default. It supersedes the old prescriptive `.md` worker
prompts and the clone-a-script evaluators — instead of editing a template, you
compose primitives and let the gate judge.

Scope is **NBA research only**. Never touch the arbitrage bot
(`kalshi_arbitrage/`, root `*.py`). The exact per-module API is in
[`CONTRACT.md`](CONTRACT.md); the shared dataclasses are in
[`types.py`](types.py); the lessons that shaped the defaults are in
[`../ALPHA_FINDINGS.md`](../ALPHA_FINDINGS.md) and
[`../TOTALS_REFINE_FINDINGS.md`](../TOTALS_REFINE_FINDINGS.md). Read those before
you risk any conclusion.

## Mental model

One idea flows through five stages. Each stage is a pure-ish primitive you can
swap or inspect:

```
Panel(s) ──signals──> features ──Strategy──> Trades ──evaluate──> GateResult
            (lab.signals)         (lab.strategy)      (lab.execution FillModel + lab.evaluate)
```

- **Panel** (`lab.data.load_panels`) — one game's per-minute frame for one
  market (`winner` / `total` / `spread`). It carries `elapsed_sec`, `margin`,
  `total`, the implied level `mid`, and — crucially — the real strike `ladder`
  (`strike -> P(over/cover)` per minute). Realistic execution reads that ladder.
- **signals** (`lab.signals`) — pure functions over a `Panel` returning a
  length-`n` `np.ndarray`: `pace_projection`, `anchoring_gap`, `implied_level`,
  `calibration_gap`, `staleness_min`, `rolling`, `zscore`. Compose them to
  describe your observable.
- **Strategy** (`lab.strategy.Strategy`) — your idea as code: an `entry` mask
  (first `True` per game fires), a `side` selector, an `exit`. Honest **i+1
  entry latency** and a **freshness guard** are built in; `.run(panels)` returns
  `Trades`.
- **realistic execution** (`lab.execution.FillModel`, default `REALISTIC`) —
  snaps to the nearest *listed* strike and fills at its **real quoted price**,
  crosses a half-spread, and pays the venue taker fee. This is the default
  everywhere; **never assume a 0.50 fill** (see Non-negotiables).
- **GateResult** (`lab.evaluate.evaluate`) — the verdict: block-bootstrap CI,
  a cost sweep, monthly walk-forward, and the four adversarial checks
  (staleness, estimator-bias-vs-unconditional, concentration, OOS). The gate is
  the judge — you do not get to argue with it.

**The hypothesis registry** (`lab.hypothesis`) is the dynamic backlog that
replaces the old hard-coded direction menus. It is an append-only JSONL store of
`Hypothesis` rows (`market`, `mechanism`, `signal_desc`, pre-registered
`direction`, `status`, `verdict`). You **pre-register your direction before you
score** — the registry is what keeps you honest about it. `register` dedupes by
mechanism hash; `claim` marks one `running`; `update` records the verdict.

## Minimal worked example

The `Lab` façade (`lab.session`) is the single import that ties it together:

```python
from research.lab import lab
from research.lab import signals as sg

L = lab()                                  # default session; REALISTIC fills

strat = L.strategy(
    name="totals_pace_anchor",
    entry=lambda p: sg.anchoring_gap(p) > 6,        # pace projection runs hot vs the line
    side=lambda p, i: "over",                        # pre-registered direction
)

# load + run + evaluate in one call, on the non-test population:
gate = L.backtest(strat, market="total", split="nontest")
print(gate.passed, gate.cents_per_contract, gate.ci_lo, gate.cost_sweep)
```

Or drive it from the CLI without writing a runner:

```bash
python -m research.lab.cli demo     # end-to-end backtest on synthetic fixtures
python -m research.lab.cli backtest mymod:STRATEGY --market total --split nontest
python -m research.lab.cli hypotheses list
```

The `demo` subcommand runs the whole pipeline on `synthetic_panel` fixtures, so
it works anywhere — no data cache required.

## Composing a NEW idea, end to end

1. **Register the hypothesis first** (pre-register the direction):

   ```python
   from research.lab import lab, Hypothesis
   L = lab()
   h = L.register(Hypothesis(
       market="spread",
       mechanism="Late-game spread overreacts to a scoring run; mean-reverts by settlement.",
       signal_desc="elapsed>2400 and a fresh anchoring_gap favoring the trailing side",
       direction="fade_the_run",
   ))
   ```

2. **Write `entry` / `side` from `lab.signals`** (pure functions over a `Panel`):

   ```python
   from research.lab import signals as sg

   def entry(p):
       return (p.elapsed_sec > 2400) & (sg.anchoring_gap(p) > 4) & (sg.staleness_min(p) <= 2)

   def side(p, i):
       return "long_home" if sg.anchoring_gap(p)[i] > 0 else "long_away"

   strat = L.strategy(name="late_run_fade", entry=entry, side=side)
   ```

3. **Run and evaluate, then record the verdict:**

   ```python
   trades = strat.run(L.load("spread", split="nontest"))   # REALISTIC fills by default
   gate = L.evaluate(trades)
   from research.lab import hypothesis as hyp
   hyp.update(h.id, results={"cents": gate.cents_per_contract, "ci_lo": gate.ci_lo},
              verdict="PROMOTE" if gate.passed else "DEAD", status="done")
   ```

That is the full loop: registry → signals → `Strategy` → realistic execution →
gate → verdict. No script cloning.

## Non-negotiables

These are not style preferences; each is a scar from a real failure.

- **Realistic execution is the default — never assume a 0.50 fill.** The
  "certified" totals edge was a pure execution artifact: the evaluator filled the
  at-the-money over/under at a fabricated **0.50**, but the real listed contract
  on the signal side already priced ~**0.556**. Under realistic fills the edge
  went from +6.21¢ to **−1.00¢/contract** and failed the gate everywhere
  (`../TOTALS_REFINE_FINDINGS.md`). Always fill at the real listed strike via a
  `FillModel`.
- **Honest latency.** Enter the minute *after* the signal (built-in i+1
  `entry_latency_min`), never on the bar that fired it. Optimistic latency
  manufactures phantom edges exactly like an optimistic fill does.
- **The gate is the judge.** A `GateResult` with `passed=False` is dead, no
  matter how pretty the headline cents look. Never weaken the gate, and pre-
  register your direction before you score it.
- **Never read the test split.** `load_panels(split=...)` defaults to leaving
  test out; use `"train"`, `"val"`, or `"nontest"`. Do not read `--split test`
  or the test ids in `market_data/splits.json` — that split is sacrosanct.
- **Hunt the bug on too-good results.** A result that looks great is a bug until
  proven otherwise. Decompose `payoff − 0.50` into `payoff − real_mid − fee −
  spread`; check the average fill mid against the realized win rate; run the
  cost sweep and the OOS / season-half check. The totals 0.50-fill artifact is
  the canonical cautionary tale — ~5.55¢ of a "+8¢ edge" was nothing but the gap
  between a fabricated 0.50 and the price you would actually pay.

## The decision layer — what gets pursued (and why nothing is hardcoded)

The substrate above is the *bench*. What an analyst-agent actually *pursues* is
decided at runtime by a three-part layer — and that machinery is the **only**
thing the codebase hardcodes about strategy selection. There is no seeded idea
list, no `DIRECTIONS`/`DIRECTION_ROTATION` menu, no `seed_defaults` (all removed).

1. **`lab.eda`** — idea-agnostic data scanners. Surface *where* a market deviates
   from calibration/efficiency (calibration bias by price bucket, anchoring-gap
   distribution, staleness). Raw material, never a recommendation.
2. **`lab.scout`** — ORIGINATION. Hands the EDA + the research record to an agent
   (`scout_prompt.md`) that *invents* new, distinct, mechanism-grounded
   hypotheses and registers them. Ideas come from the model, not a hand list. If
   no agent is available and the registry is empty, the pool stays empty (the
   loop says so) — nothing canned is substituted.
3. **`lab.director`** — PRIORITIZATION + FEEDBACK. `select()` hands the open pool
   + the verdict ledger to an agent (`director_prompt.md`) that *ranks* what to
   pursue next by expected value × novelty × evidence (down-ranking DEAD
   families, deepening PROMISING leads). `incorporate_results()` folds recorded
   verdicts back into the registry so the pool evolves. No round-robin.

Drivers (`agents/usage/edge_hunt_loop.py`, `lab/analyst.py`,
`agents/orchestrator/run.py`) all route through scout → director → feedback.
Cold start = scout (agent), selection = director (agent), learning = feedback
(results). The strategies are the agent's; the layer is ours.
