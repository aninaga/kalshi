# research/lab/ — integration validation (coordinator, post-merge)

The 15 substrate units were built in parallel isolated worktrees (synthetic
fixtures only — the data cache is gitignored and absent there). After merging
onto the feature branch, the coordinator validated the ASSEMBLED substrate.

## Test suite (assembled)
- `pytest research/lab research/agents/usage/tests research/agents/orchestrator/tests
  research/agents/workers/tests` → **238 passed** (fast) + **13 passed**
  (`test_evaluate.py`, ~5 min bootstrap) = **251 lab+agent tests green**.

## Real-cache end-to-end (the check worktrees couldn't do)
Loaded real cached panels via `lab.data`, ran the `lab.strategies.anchoring`
Strategies through `Strategy.run` (realistic `FillModel` default) and scored with
`lab.evaluate` (the gate). Reproduces the known conclusions through the NEW stack:

| market  | panels | trades | net ¢/ct | gate | known conclusion |
|---------|-------:|-------:|---------:|------|------------------|
| total   | 1112   | 1107   | −1.78    | FAIL | execution-killed (0.50-fill artifact) ✓ |
| spread  | 1112   | 1106   | +3.32    | FAIL | real residual, uncertified ✓ |

Sign + gate verdict match the originals (`TOTALS_REFINE_FINDINGS.md` /
`SPREADS_FINDINGS.md`); magnitudes differ slightly (nontest split + lab default
params vs the originals' full-population runs). The substrate is validated:
data → signals → Strategy → realistic execution → gate works on real data.

## Coordinator integration fixes (recorded for provenance)
- `conftest.py` purges `research.lab.*` between tests (worktree stub leakage).
- `anchoring._anchoring_gap`: SPREAD projects from signed margin (signals.anchoring_gap
  is TOTAL-only per contract) — a real cross-unit bug the assembly exposed.
- strategy-test stub guards + duck-typed assertions (real Strategy post-merge).
