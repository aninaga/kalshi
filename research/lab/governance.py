"""research.lab.governance — N-aware Deflated-Sharpe governance.

The promotion gate's Deflated Sharpe Ratio (DSR) correction needs two numbers
that describe the *whole research program*, not just the trial being judged:

  * ``N`` — how many distinct trials the org has ever EVALUATED (the
    multiple-testing count, a.k.a. "how many darts were thrown at the board"),
    and
  * ``V[SR]`` — the cross-trial variance of the per-trial Sharpe ratios.

These feed ``research.scorer.dsr.expected_max_sharpe(N, V[SR])``, the
"best-of-N" hurdle a candidate must clear. Historically the gate was fed
``n_trials=1`` (a placeholder), which switches the multiple-testing correction
OFF — every candidate is judged as if it were the only experiment ever run.
That is exactly the selection-bias trap López de Prado warns about: when an
analyst keeps trying strategies and only reports the winners, the maximum
observed Sharpe is inflated, and a naive significance test will promote noise.

This module reconstructs ``(N, V[SR])`` HONESTLY from the persisted research
record — the per-trial ledger plus the hypothesis registry — so that as more
darts are thrown the hurdle rises and the gate tightens automatically.

Everything here is a **pure function**: it reads JSONL files defensively (no
network, no mutation, malformed lines skipped) and degrades gracefully to the
documented cold-start placeholders ``N=1`` / ``V[SR]=1.0`` when there is not
enough history to estimate anything. The placeholders match the gate defaults
(``research.scorer.promotion_gate.evaluate_trial`` defaults
``sharpe_variance_in_registry=1.0`` and clamps ``n_trials`` to ``>= 1``).

Crucially, ``governance_params`` also reports WHERE each number came from
(real-ledger vs cold-start placeholder) so the orchestrator's audit log — and a
human reviewer — can see whether a promotion was deflated against real history
or against a placeholder.

Reference
---------
López de Prado, M. (2014). "The Deflated Sharpe Ratio: Correcting for Selection
Bias, Backtest Overfitting, and Non-Normality." Journal of Portfolio
Management, 40(5), 94–107. (Selection-bias / meta-strategy rationale: the more
configurations you try, the higher the expected maximum Sharpe under the null,
so the significance hurdle must rise with the trial count.)
"""
from __future__ import annotations

import json
import math
import os

from research.lab import hypothesis

# Documented cold-start placeholders. These match the promotion gate defaults so
# that a cold (empty) research record reproduces the historical behaviour:
#   - N=1            -> expected_max_sharpe collapses to 0 (no MTC; PSR vs 0).
#   - V[SR]=1.0      -> matches evaluate_trial's sharpe_variance default.
_PLACEHOLDER_N_TRIALS = 1
_PLACEHOLDER_SHARPE_VARIANCE = 1.0

# Bootstrap CI is a ~95% two-sided interval, so its half-width ≈ 1.96 * SE of the
# mean. Dividing the half-width by this z-value recovers an approximate standard
# error, which we use as a documented stand-in for per-trial return dispersion
# when no explicit Sharpe is recorded. (Stand-in only; see _row_sharpe.)
_CI95_Z = 1.959963984540054


# ---------------------------------------------------------------------------
# Defensive JSONL reading
# ---------------------------------------------------------------------------


def _read_jsonl(path: str) -> list[dict]:
    """Read every JSONL object in ``path``; skip blank / malformed lines.

    Mirrors ``research.lab.hypothesis._read_rows`` semantics: a missing file is
    an empty list, and a torn line from a concurrent partial write is silently
    skipped rather than raised.
    """
    if not path or not os.path.exists(path):
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ---------------------------------------------------------------------------
# Per-trial Sharpe extraction
# ---------------------------------------------------------------------------


def _row_sharpe(results: dict) -> float | None:
    """Best-effort per-trial Sharpe from one ledger row's ``results`` dict.

    Preference order:
      1. An explicitly recorded ``sharpe`` (used as-is when finite).
      2. A PROXY derived from the reported net edge and its bootstrap CI:
         the CI half-width is a ~95% interval on the MEAN, so
             SE_of_mean ≈ half_width / 1.96
             per-trade_std ≈ SE_of_mean * sqrt(n_trades)
             sharpe_proxy ≈ mean / per-trade_std = mean * 1.96 / half_width
         i.e. the n_trades factors cancel and the proxy is simply the t-like
         ratio ``mean * z / half_width``. This is a STAND-IN for a true
         per-period Sharpe (we do not have the per-trade PnL vector here); it is
         monotone in the trial's statistical strength, which is all V[SR] needs.

    Returns ``None`` when no usable number can be formed (the row is then
    ignored by the variance estimator).

    Units note: ``net_cents_0c`` / ``ci_lo`` are in CENTS in the demo ledger;
    the proxy is scale-consistent within a row (cents/cents), so cross-trial
    comparison is consistent as long as the ledger is self-consistent.
    """
    if not isinstance(results, dict):
        return None

    # 1. Explicit Sharpe.
    sharpe = results.get("sharpe")
    if _is_finite_number(sharpe):
        return float(sharpe)

    # 2. Proxy from mean edge + bootstrap CI half-width.
    mean = None
    for key in ("net_cents_0c", "net_cents", "mean_cents", "cents", "mean"):
        if _is_finite_number(results.get(key)):
            mean = float(results[key])
            break
    if mean is None:
        return None

    # CI half-width: prefer an explicit (ci_lo, ci_hi) pair; else use the
    # zero-cost CI lower bound vs the mean as a one-sided half-width stand-in.
    ci_lo = results.get("ci_lo_0c")
    if not _is_finite_number(ci_lo):
        ci_lo = results.get("ci_lo")
    ci_hi = results.get("ci_hi_0c")
    if not _is_finite_number(ci_hi):
        ci_hi = results.get("ci_hi")

    half_width: float | None = None
    if _is_finite_number(ci_lo) and _is_finite_number(ci_hi):
        half_width = (float(ci_hi) - float(ci_lo)) / 2.0
    elif _is_finite_number(ci_lo):
        # Only a lower bound available: distance from mean to ci_lo approximates
        # the (one-sided) half-width.
        half_width = abs(mean - float(ci_lo))

    if half_width is None or not math.isfinite(half_width) or half_width <= 0:
        return None

    sharpe_proxy = mean * _CI95_Z / half_width
    if not math.isfinite(sharpe_proxy):
        return None
    return float(sharpe_proxy)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def trial_count(
    ledger_path: str,
    registry_path: str = hypothesis.DEFAULT_PATH,
) -> int:
    """Number of DISTINCT trials the org has ever EVALUATED — the DSR's ``N``.

    What counts as a "trial" (López de Prado meta-strategy / "how many darts
    were thrown"):

      * **Every ledger row is one trial.** The ledger records each evaluation
        the org ran; each row is one dart thrown at the board, whether it
        passed the gate or not. Selection bias is driven by the COUNT of things
        attempted, not just the winners, so a failed evaluation still counts.
      * **Plus every registry hypothesis that has a ``verdict`` set.** A
        hypothesis that reached a verdict (PROMOTE / PROMISING / NEEDS_DATA /
        DEAD) was evaluated even if — in a cold or partially-migrated record —
        it never made it into the ledger. Hypotheses still ``open`` / unjudged
        are NOT counted: no dart has been thrown yet.

    De-duplication: a trial that was RE-RUN counts ONCE. Ledger rows are deduped
    by ``hypothesis_id`` (re-running the same hypothesis is the same dart, re-
    thrown), and a registry hypothesis already represented by a ledger row of
    the same id is not double-counted.

    Returns at least ``1`` (the candidate being judged is itself a trial; with
    no history the multiple-testing correction is meaningless and DSR collapses
    to a plain PSR vs zero — see ``research.scorer.dsr.expected_max_sharpe``).
    """
    ids: set[str] = set()
    anon_rows = 0  # ledger rows with no usable id still count as trials.

    for row in _read_jsonl(ledger_path):
        hid = row.get("hypothesis_id") or row.get("id")
        if isinstance(hid, str) and hid:
            ids.add(hid)
        else:
            anon_rows += 1

    # Registry hypotheses that reached a verdict (deduped against ledger ids).
    try:
        hyps = hypothesis.query(path=registry_path)
    except Exception:
        hyps = []
    for h in hyps:
        verdict = getattr(h, "verdict", None)
        hid = getattr(h, "id", None)
        if verdict and isinstance(hid, str) and hid:
            ids.add(hid)

    return max(1, len(ids) + anon_rows)


def _sharpe_variance_estimate(
    ledger_path: str,
) -> tuple[float, bool]:
    """Core V[SR] computation. Returns ``(variance, is_real_estimate)``.

    ``is_real_estimate`` is True iff at least 2 trials carried usable Sharpe
    numbers and the resulting variance was finite and non-negative (i.e. the
    value is a genuine estimate rather than the cold-start placeholder). This
    lets :func:`governance_params` label the source WITHOUT a brittle float
    comparison against ``1.0`` (a real estimate could legitimately equal 1.0).
    """
    by_id: dict[str, float] = {}
    anon_values: list[float] = []

    for row in _read_jsonl(ledger_path):
        s = _row_sharpe(row.get("results", {}))
        if s is None or not math.isfinite(s):
            continue
        hid = row.get("hypothesis_id") or row.get("id")
        if isinstance(hid, str) and hid:
            # Last-write-wins per id (a re-run replaces its prior Sharpe).
            by_id[hid] = s
        else:
            anon_values.append(s)

    values = list(by_id.values()) + anon_values
    if len(values) < 2:
        return _PLACEHOLDER_SHARPE_VARIANCE, False

    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)  # sample variance
    if not math.isfinite(var) or var < 0:
        return _PLACEHOLDER_SHARPE_VARIANCE, False
    return float(var), True


def sharpe_variance(
    ledger_path: str,
    registry_path: str = hypothesis.DEFAULT_PATH,
) -> float:
    """Cross-trial variance of per-trial Sharpe across the ledger — DSR's V[SR].

    Each ledger row contributes one per-trial Sharpe via :func:`_row_sharpe`
    (an explicit ``sharpe`` if present, else a documented CI-based proxy). We
    take the SAMPLE variance (``ddof=1``) across distinct trials, which is the
    dispersion of "how good did the darts look" — the quantity that scales the
    best-of-N hurdle in ``expected_max_sharpe``.

    NaN-safe: rows without a usable number are dropped. If FEWER THAN 2 trials
    carry usable numbers, there is nothing to estimate dispersion from, so we
    return the documented placeholder ``1.0`` (matching the gate default). The
    result is always finite and ``>= 0``.

    Note: only the ledger is mined for Sharpe values — the registry is used by
    :func:`trial_count` for ``N`` but does not carry comparable per-trial PnL,
    so it does not contribute to V[SR]. (``registry_path`` is accepted for
    signature symmetry and future use.)
    """
    var, _is_real = _sharpe_variance_estimate(ledger_path)
    return var


def governance_params(
    ledger_path: str,
    registry_path: str = hypothesis.DEFAULT_PATH,
) -> dict:
    """Compute ``(N, V[SR])`` AND report where each number came from.

    Returns a dict with four keys::

        {
          "n_trials":               int,    # DSR multiple-testing N (>= 1)
          "sharpe_variance":        float,  # cross-trial V[SR] (>= 0)
          "n_trials_source":        str,    # "real-ledger" | "cold-start placeholder"
          "sharpe_variance_source": str,    # "real-ledger" | "cold-start placeholder"
        }

    The ``*_source`` labels make the governance layer INSPECTABLE: a caller (or
    audit log) can tell whether a promotion was deflated against the real
    research history or against the cold-start placeholders. A number is labelled
    ``"real-ledger"`` only when it was actually derived from the record:

      * ``n_trials_source`` is "real-ledger" when more than one distinct trial
        was found (i.e. the candidate is genuinely competing against history);
        with ``n_trials == 1`` it is the cold-start placeholder.
      * ``sharpe_variance_source`` is "real-ledger" when at least 2 trials
        carried usable Sharpe numbers (so the variance is an estimate, not the
        ``1.0`` fallback).

    Pure / side-effect free / no network. Safe on missing or empty paths
    (degrades to ``N=1``, ``V[SR]=1.0``, both sources "cold-start placeholder").
    """
    n = trial_count(ledger_path, registry_path=registry_path)
    v, v_is_real = _sharpe_variance_estimate(ledger_path)

    n_source = "real-ledger" if n > _PLACEHOLDER_N_TRIALS else "cold-start placeholder"
    v_source = "real-ledger" if v_is_real else "cold-start placeholder"

    return {
        "n_trials": int(n),
        "sharpe_variance": float(v),
        "n_trials_source": n_source,
        "sharpe_variance_source": v_source,
    }
