"""research.lab.audit — point-in-time / data-contract leakage auditor.

The substrate's #1 silent failure mode is LOOKAHEAD: a live signal or entry rule
evaluated at bar ``i`` accidentally uses information only knowable at-or-after the
game ends (or after bar ``i``). A backtest contaminated this way certifies an edge
that cannot exist live.

The :class:`~research.lab.types.Panel` carries two disjoint classes of field:

  * POINT-IN-TIME time-series — known as the game unfolds, one value per minute
    bar: ``minute_ts``, ``elapsed_sec``, ``margin``, ``total``, ``mid``,
    ``ladder`` (per-strike prob arrays), and ``features`` entries. A live signal
    at bar ``i`` may read these only at indices ``<= i``.
  * OUTCOME / terminal — :data:`OUTCOME_FIELDS` = ``final_total``,
    ``final_margin``, ``home_won``. These are known ONLY after the final whistle.
    Settlement (``strategy._settlement_payoff``) is *allowed* to read them — that
    is the whole point of settlement. A live signal/entry/side callable is
    *forbidden* to touch them, directly or via any derived quantity.

As-of correctness: the value a signal emits for bar ``j`` must depend only on the
panel's state through bar ``j``. Equivalently, it must be invariant to anything
that happens after ``j``. This module enforces that two complementary ways:

  1. A STATIC source check (:func:`audit_callable_source`) greps a callable's
     source for references to outcome fields — cheap, catches the obvious cases,
     but blind to indirection.
  2. A DYNAMIC perturbation test (:func:`audit_signal_no_lookahead`) — the real
     teeth. It recomputes the signal on futures-truncated copies of the panel and
     asserts no past bar's value moved. If chopping off (and neutralizing) the
     future changes a past bar, the signal peeked. This catches lookahead through
     ANY mechanism (outcome fields, forward-rolling windows, reversed cumsum, …),
     not just literal field names.

Exemption — LABEL construction is allowed to use outcomes. ``eda._residual_label``
(and the calibration table, lens ranking, regime scan it feeds) legitimately reads
``final_total`` / ``final_margin`` / ``home_won``: they build the TRAINING TARGET,
computed offline to *rank lenses*, and are never evaluated as a live signal. The
distinction this module draws is "signal construction (live, must not peek)" vs
"label construction (offline, may use the realized outcome)". Only callables you
hand to the auditor as live signals are held to the no-lookahead contract;
``_residual_label`` is deliberately never audited as one.

Likewise ``signals.calibration_gap(panel, realized)`` takes the realized outcome
as an EXPLICIT argument: it is an offline/label-style API (realized-vs-implied),
not a live signal of one Panel. The auditor only inspects the panel-state a signal
derives on its own, so a label-style API that is *handed* the outcome is out of
scope and must not be flagged — only its callers, if they wire a panel outcome
field into ``realized`` inside a live context, would be caught by the source check.

No network, no sibling-lab side effects; pure stdlib + numpy.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import replace
from typing import Callable

import numpy as np

from research.lab.types import Panel

# Terminal fields knowable only after the final whistle. A live signal must never
# reference these; settlement (and offline label construction) may.
OUTCOME_FIELDS = frozenset({"final_total", "final_margin", "home_won"})

# Default cut points (as fractions of the panel length) for the perturbation test.
_DEFAULT_CUT_FRACS = (0.25, 0.5, 0.75, 0.9)
# Absolute tolerance for the as-of value-equality comparison.
_DEFAULT_ATOL = 1e-9


# --------------------------------------------------------------------------- #
# 1. static contract check
# --------------------------------------------------------------------------- #

def audit_callable_source(fn: Callable) -> list[str]:
    """Flag references to :data:`OUTCOME_FIELDS` in a live callable's source.

    Uses :func:`inspect.getsource` to scan the callable's text for any mention of
    an outcome field (``final_total`` / ``final_margin`` / ``home_won``), as a bare
    name or an attribute access. Returns a list of human-readable violation strings
    (empty == clean).

    Robust when source is unavailable (builtins, C extensions, dynamically built
    lambdas, REPL-defined fns): returns a single informational note rather than
    raising — a missing source is "could not verify", not "clean".
    """
    name = getattr(fn, "__name__", repr(fn))
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return [f"NOTE: source unavailable for {name!r}; "
                f"static outcome-field check skipped (use the dynamic test)."]

    violations: list[str] = []
    for field in sorted(OUTCOME_FIELDS):
        # Match the field as a standalone identifier or attribute (`.final_total`,
        # `panel.home_won`, `["final_margin"]`, etc.) — bounded by non-word chars.
        pattern = rf"(?<![\w]){re.escape(field)}(?![\w])"
        for lineno, line in enumerate(src.splitlines(), start=1):
            if re.search(pattern, line):
                violations.append(
                    f"{name}: references outcome field {field!r} at source line "
                    f"{lineno}: {line.strip()!r} — live signals must not read "
                    f"outcomes (allowed only in settlement / offline labels).")
    return violations


# --------------------------------------------------------------------------- #
# 2. dynamic perturbation (no-lookahead) test
# --------------------------------------------------------------------------- #

# Per-bar time-series arrays that are sliced when truncating a panel to bars 0..t.
_PER_BAR_ARRAYS = ("minute_ts", "elapsed_sec", "margin", "total", "mid")


def truncate_panel(panel: Panel, t: int) -> Panel:
    """Return a copy of ``panel`` containing only bars ``0..t`` (inclusive).

    Every per-bar array (the time-series fields and each ``ladder`` /
    ``features`` array) is sliced to ``[: t + 1]``, and ALL outcome fields are
    neutralized (set to NaN) so a truncated panel literally cannot leak a final
    score back into a past-bar computation — the future is gone, both as bars and
    as terminal aggregates. The original panel is never mutated.

    ``t`` is clamped to ``[0, n - 1]``. The returned panel's ``n`` is ``t + 1``.
    """
    n = panel.n
    if n == 0:
        return replace(panel)
    t = max(0, min(int(t), n - 1))
    cut = t + 1

    sliced = {name: np.asarray(getattr(panel, name))[:cut].copy()
              for name in _PER_BAR_ARRAYS}
    ladder = {float(k): np.asarray(v, dtype=float)[:cut].copy()
              for k, v in (panel.ladder or {}).items()}
    features = {k: np.asarray(v)[:cut].copy()
                for k, v in (panel.features or {}).items()}

    return replace(
        panel,
        ladder=ladder,
        features=features,
        # Neutralize terminal fields: the future does not exist in a truncated view.
        home_won=float("nan"),
        final_total=float("nan"),
        final_margin=float("nan"),
        **sliced,
    )


def _equal_nan(a: np.ndarray, b: np.ndarray, atol: float) -> np.ndarray:
    """Element-wise equality treating NaN==NaN as equal (and within ``atol``)."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    both_nan = np.isnan(a) & np.isnan(b)
    close = np.abs(a - b) <= atol
    return both_nan | close


def audit_signal_no_lookahead(
    signal_fn: Callable[[Panel], np.ndarray],
    panel: Panel,
    *,
    cut_fracs: tuple[float, ...] = _DEFAULT_CUT_FRACS,
    atol: float = _DEFAULT_ATOL,
) -> dict:
    """Test that ``signal_fn`` is invariant to FUTURE data (no lookahead).

    Strategy: compute the signal on the full panel; then for each cut point ``t``
    truncate the panel to bars ``0..t`` (futures removed AND outcome fields
    neutralized via :func:`truncate_panel`) and recompute. For an as-of-correct
    signal, every past bar ``j <= t`` must be unchanged vs the full-panel value. If
    it moved, the full-panel value at ``j`` had used information from bars ``> j``
    (or from a terminal outcome) — i.e. lookahead.

    Returns ``{"passed": bool, "violations": [str, ...], "cut_points_tested": int}``.
    Comparison is NaN-safe (NaN==NaN treated equal) to tolerance ``atol``.
    """
    n = panel.n
    violations: list[str] = []

    if n == 0:
        return {"passed": True, "violations": [], "cut_points_tested": 0}

    try:
        full = np.asarray(signal_fn(panel), dtype=float)
    except Exception as exc:  # noqa: BLE001 — a crashing signal is itself a finding
        return {"passed": False,
                "violations": [f"signal raised on full panel: {exc!r}"],
                "cut_points_tested": 0}

    if full.shape != (n,):
        violations.append(
            f"signal returned shape {full.shape}, expected ({n},); "
            f"cannot run as-of comparison")
        return {"passed": False, "violations": violations, "cut_points_tested": 0}

    # Build the cut points: dedupe, keep in (0, n-1) so there is both past and future.
    cuts = sorted({int(f * (n - 1)) for f in cut_fracs})
    cuts = [t for t in cuts if 1 <= t < n - 1] or [max(1, n // 2)]

    tested = 0
    for t in cuts:
        sub = truncate_panel(panel, t)
        try:
            partial = np.asarray(signal_fn(sub), dtype=float)
        except Exception as exc:  # noqa: BLE001
            violations.append(f"signal raised on panel truncated at t={t}: {exc!r}")
            continue
        tested += 1

        if partial.shape != (t + 1,):
            violations.append(
                f"t={t}: truncated signal shape {partial.shape}, expected "
                f"({t + 1},); signal may not respect panel length")
            continue

        match = _equal_nan(full[: t + 1], partial, atol)
        if not match.all():
            bad = np.flatnonzero(~match)
            j = int(bad[0])
            violations.append(
                f"t={t}: LOOKAHEAD — bar {j} (of {bad.size} changed) moved when "
                f"future bars {t + 1}..{n - 1} were removed: full={full[j]!r} "
                f"vs truncated={partial[j]!r}. A past bar must not depend on "
                f"future data.")

    return {
        "passed": not violations,
        "violations": violations,
        "cut_points_tested": tested,
    }


# --------------------------------------------------------------------------- #
# 3. report across a set of named signals
# --------------------------------------------------------------------------- #

def audit_report(panel: Panel, signal_fns: dict[str, Callable]) -> dict:
    """Run the dynamic no-lookahead test for each named signal and summarize.

    ``signal_fns`` maps a human name to a one-arg ``signal_fn(panel) -> ndarray``
    (live signals only — do NOT pass label constructors or label-style APIs that
    require a ``realized`` argument). Returns a per-signal detail dict plus a
    top-level summary.
    """
    per_signal: dict[str, dict] = {}
    for name, fn in signal_fns.items():
        result = audit_signal_no_lookahead(fn, panel)
        # Also fold in the cheap static check as supplementary evidence.
        result["source_flags"] = audit_callable_source(fn)
        per_signal[name] = result

    failed = sorted(name for name, r in per_signal.items() if not r["passed"])
    return {
        "n_signals": len(signal_fns),
        "n_passed": sum(1 for r in per_signal.values() if r["passed"]),
        "n_failed": len(failed),
        "all_passed": not failed,
        "failed_signals": failed,
        "signals": per_signal,
    }
