# Batch: governance, leakage audit, on-demand data agent (2026-06-09)

Three independent, parallel substrate units — the "build now" slice from the
hedge-fund-org deep-research report, after the adversarial review. The adversarial
analysis (Mythos / GraphWalks / METR long-horizon breakthroughs vs. the multi-agent
literature) concluded the 2026 capability gains touch the *autonomy / coordination /
long-context* layer, **not** the *data / edge / market-efficiency* layer that binds us
— so the right move is a thin, high-value slice on the existing substrate driven by a
single central orchestrator with surgical, **centralized** fan-out (never peer-to-peer,
which amplifies error 17×). These three units are exactly that: genuinely-parallel,
independent work, integrated centrally.

## Unit 1 — N-aware DSR governance (`lab/governance.py`)

The promotion gate's Deflated-Sharpe multiple-testing correction was fed the
cold-start placeholder `n_trials=1`. It now deflates against the **real** research
record:

- `trial_count(ledger, registry)` — the DSR "best-of-N": distinct trials ever
  *evaluated* (ledger rows deduped by `hypothesis_id`, plus verdicted registry
  hypotheses), min 1. This is "how many darts were thrown" (López de Prado 2014).
- `sharpe_variance(ledger)` — cross-trial Sharpe dispersion (uses `results.sharpe`
  when present, else a documented CI-half-width proxy); `<2` usable trials → `1.0`.
- `governance_params(...)` → `{n_trials, sharpe_variance, *_source}` — **inspectable
  provenance** so reviewers see real-ledger vs placeholder.

Wired into `lab.evaluate(..., ledger_path=...)`: when a ledger is supplied the gate
is deflated honestly; **when omitted, behavior is byte-identical to before**. DSR is
informational in the gate, so this tightens the *reported* hurdle without changing or
weakening any decisive (block-bootstrap / cluster-knockout) gate. Provenance is
surfaced on `GateResult.governance`.

## Unit 2 — point-in-time / leakage audit (`lab/audit.py`)

Catches lookahead — the substrate's #1 silent failure. `OUTCOME_FIELDS =
{final_total, final_margin, home_won}` must never enter a live signal.

- `audit_callable_source(fn)` — static: flags outcome-field references in a live
  callable's source (graceful NOTE when source is unavailable).
- `truncate_panel(panel, t)` + `audit_signal_no_lookahead(...)` — dynamic: a live
  signal at bar i must be invariant to *future* data; recompute on truncations and
  assert past bars are unchanged. NaN-safe.
- `audit_report(panel, signal_fns)` — aggregates across named signals.

**Real finding:** run against every shipped `lab.signals` function across all three
markets — **no shipped signal shows lookahead**. `calibration_gap(panel, realized)`
and `eda._residual_label` are correctly treated as offline label-construction
(exempt), not false-positived.

## Unit 3 — on-demand data agent (`lab/data_agent.py`)

The "analyst files a data request → a data agent fulfills it" flow, as an injectable
seam mirroring `scout.proposer` / `director.ranker` (model-agnostic).

- `DataRequest` + append-only JSONL log (`request`/`query_requests`/`update_request`).
- `fulfill(request, panel, *, provider=default_provider)` — `default_provider`
  synthesizes derivable fields from the existing operator vocabulary (reusing
  `signals.rolling`/`zscore`/`_series`); genuinely-external asks are marked
  `NEEDS_DATA` and **never fabricated**.
- `summon_for_hypothesis(h)` — files a request for a `NEEDS_DATA` hypothesis.

The `provider` arg is the agentic boundary: an LLM-backed data agent plugs in there
exactly like the scout's proposer; the no-fabrication contract holds for any provider.

## Tests

- `tests/test_governance.py` (17), `tests/test_audit.py` (19),
  `tests/test_data_agent.py` (6) — all green, plus the full `lab` suite passes with
  the `evaluate.py` integration.
