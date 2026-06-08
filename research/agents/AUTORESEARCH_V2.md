# Autoresearch V2 — GPT-5.5 (codex exec) edge-hunting on your ChatGPT-Pro 20x

A design for automating edge-discovery with `codex exec`, budget-governed by
your `agent-limits` tool, and tuned to GPT-5.5's actual behavior. Everything
below is built and (where possible without `codex`) tested in this repo. The
arb bot is out of scope.

## 0. The one thing that makes this different from the prior attempt

The prior autoresearch loop was mechanically excellent and **found nothing** —
because it ground the **efficient moneyline DSL** with threshold rules. The
hunt I just ran proved two things that V2 is built around:

1. **Edge exists, but in a different market.** Moneyline is picked clean; the
   *less-watched totals book* carried a gate-certified anchoring edge
   (+6.21¢/contract net at 2¢, full-season gate PASS). **Efficiency varies by
   market, not sport.**
2. **The binding constraint was data, not cleverness.** The same edge failed the
   gate at n=199 (val) and passed at n=1,297 (full season). So V2's job is to
   point cheap GPT-5.5 labor at the **right search space** and let the gate +
   data volume do the certifying.

So V2 = (right search space) × (a prompt that encodes the methodology that
worked) × (cheap parallel GPT-5.5 workers, budget-governed) × (the honest gate
as the only judge).

## 1. Components (all in this repo)

| piece | file | status |
|---|---|---|
| Usage poller + budget governor | `research/agents/usage/limits.py` | ✅ tested (8) |
| GPT-5.5 characterization probe | `research/agents/usage/probe_gpt55.py` | ✅ tested (5), mock-verified |
| Edge-hunt loop driver | `research/agents/usage/edge_hunt_loop.py` | ✅ dry-run verified |
| Research-grade worker prompt | `research/agents/workers/gpt55_edge_hunter_prompt.md` | ✅ |
| The judge (reused) | `research/scorer/promotion_gate.py` | ✅ existing |
| Evaluator templates (reused) | `research/scripts/{totals_alpha,totals_walkforward,sub_alpha,...}.py` | ✅ |

The existing `foreman.py` / `orchestrator/run.py` / `codex_dispatcher.py` remain
valid; V2's `limits.py` is the cleaned-up, tested extraction of foreman's
limit-reading, and `edge_hunt_loop.py` is a smaller driver you can run directly.

## 2. Polling your ChatGPT-Pro usage (your `agent-limits` tool)

`limits.py` is the single hardened place that talks to your tool:

- **Locates** it: `$AGENT_LIMITS_BIN` → PATH → the known install path
  (`~/Documents/Codex/.../bin/agent-limits`). Set `$AGENT_LIMITS_BIN` if it moves.
- **Polls** `agent-limits status --json` and parses the nested codex windows
  (`primary`=5h, `secondary`=7d, `used_percent`, `resets_at_epoch`).
- **Governs**: `decide_workers(snap, base)` →
  - usage UNKNOWN/stale ⇒ **optimistic** (run base; the model's own 429s are the
    real backstop — never assume tapped on a stale read);
  - 7d ≥ 90% or 5h ≥ 88% ⇒ **pause until reset**;
  - 7d ≥ 70% ⇒ **halve** workers; else run base.

Check it any time: `python -m research.agents.usage.limits --plan 4`.

## 3. The nature of GPT-5.5, and how to prompt it (the crux of your ask)

I could not run `codex` in the build sandbox, so I did two things: (a) grounded
the prompt in **first-person evidence from the hunt I just ran** (what actually
made *a* research agent productive), and (b) built `probe_gpt55.py` to measure
GPT-5.5's real behavior on your machine and auto-tune from there. The hypotheses
the prompt encodes — each a probe the harness will confirm or refute:

1. **It's a code-first agentic model — make it measure, not muse.** GPT-5.5
   (codex) is strongest when told "write the evaluator, run it, read the CI,
   iterate," weakest when asked to reason in prose. The prompt forbids
   theorizing; every number must come from a script it ran. *(The single biggest
   accelerator in my hunt was a tight evaluator loop — `evaluate()` → instant
   0¢/2¢/CI/gate. Hand it the same loop.)*
2. **It optimizes whatever metric you name — so never name PnL.** Any capable
   agent will p-hack a positive backtest if "positive backtest" is the goal. The
   prompt makes the **gate verdict on a holdout** the success metric and makes
   **self-reported PnL explicitly worthless**. *(My calibration edge looked great
   in-sample, +1.35¢, and inverted to −3.83¢ OOS — the gate caught it; a PnL
   target would have shipped it.)*
3. **It will skip skepticism unless skepticism is mandatory.** Probe
   `artifact_detection` plants a +16.7¢ stale-line result and checks whether the
   model hunts the bug or endorses it. The prompt makes the
   "+16.7¢ → staleness → freshness guard" teardown a **required step** for any
   gross edge > ~3¢. *(This exact teardown is what separated the real totals edge
   from the artifact.)*
4. **It follows explicit contracts well.** So the prompt gives a rigid
   `result.md` JSON schema (`probe contract_adherence` verifies adherence), which
   `edge_hunt_loop.py` parses to a ledger.
5. **Hard rules need repetition + a preflight.** The test-split ban is stated at
   top and bottom; `probe test_split_discipline` checks it's respected.
6. **Reasoning effort is a real knob.** `probe_gpt55 --effort {low,medium,high}`
   sweeps `model_reasoning_effort` and scores correctness-per-token, so you pick
   the setting that maximizes gate-passes per unit quota rather than guessing.

Run the characterization first, let it tune the prompt, *then* hunt:

```bash
python -m research.agents.usage.probe_gpt55 --model gpt-5.5 --reps 3 --effort high
# weak traits print concrete prompt fixes; apply them to gpt55_edge_hunter_prompt.md
```

The probe is also how you **understand the model over time** — re-run it when
ChatGPT ships a new GPT-5.x; the scorers are deterministic so deltas are real.

## 4. The search space (markets × mechanisms)

`edge_hunt_loop.py` rotates a matrix ordered by my prior of where edge survives —
**less-watched books + level/anchoring mechanisms** (honest i+1 latency kills all
timing/momentum plays, so those are excluded):

1. `totals_extend` — extend the proven pace-anchoring totals edge (quarter
   conditioning, team pace priors, spreads-implied-total cross-check).
2. `spread_anchor` — spread line under-reacts to the live margin trajectory.
3. `altline_drift` — alternate lines stale vs the main line they track.
4. `props_thin` — thin player-prop books slow to mark foul trouble / benching.
5. `totals_endgame` — fouling/clock makes the final-minutes total predictable vs
   a frozen line.

Each worker forms its *own* mechanism within the cell, pre-registers direction,
clones the closest evaluator template, and reports via the gate.

## 5. End-to-end loop

```
poll agent-limits ─► governor: how many GPT-5.5 workers? (0 ⇒ sleep to reset)
      │
      ▼
pick next direction (round-robin matrix, persisted)
      │
      ▼
spawn N codex exec workers (workspace-write sandbox, edge_hunter prompt + direction.md)
      │  each: pre-register mechanism → clone evaluator → iterate at 0/2¢ →
      │        run promotion_gate on val → adversarial teardown if too-good →
      │        write result.md {verdict, gate_passed, real_edge, ...}
      ▼
harvest result.md ─► JSONL ledger ─► surface PROMOTE / NEEDS_DATA candidates
      │
      ▼
[HUMAN] review PROMOTE candidate; if it needs the holdout, burn ONE test unlock
        (research.promotion.review_cli --burn-unlock). Workers NEVER touch test.
```

Run it:
```bash
python -m research.agents.usage.edge_hunt_loop --waves 8 --workers 3 --effort high
python -m research.agents.usage.edge_hunt_loop --dry-run     # plan only, no codex
```

Cost model: GPT-5.5 workers run on your ChatGPT-Pro quota (governed by §2);
Claude/you stay the coordinator + final reviewer. The gate and any test-set burn
are human-gated — the cheap labor proposes, the expensive judge disposes.

## 6. Honest limitations / next steps
- I built and unit-tested everything that doesn't need `codex`; the probe's
  **live** characterization and the loop's **live** runs must happen on your
  machine. Run `probe_gpt55` first — its findings may change the prompt.
- The evaluator templates are NBA-specific. New markets (spreads/props) need the
  matching `prefetch_games --kinds ...` data and a cloned evaluator; the worker
  prompt tells GPT-5.5 to do exactly that, but confirm the data exists first
  (e.g., player-prop history may be absent, like Kalshi pre-2025-26).
- Keep the test-set sacred: 5 lifetime unlocks, human-only. The loop is designed
  so a worker physically cannot spend one.
