"""research.lab.strategies.anchoring — the pace/margin anchoring family.

This is the agentic-substrate re-expression of the two pace-vs-line anchoring
edges from ``research/scripts/totals_alpha.py`` and ``spread_alpha.py``
(plus their realistic-execution re-checks ``totals_realistic.py`` /
``spread_realistic.py``). The mechanism is the same in both markets:

    observed scoring/margin PACE projects the eventual final level
    (total points / signed home margin); the listed line may lag it. When the
    pace projection exceeds the implied level by a threshold, the line is "too
    low" and the `continuation` side (OVER / cover-the-home-side) is favored;
    when it falls short, take the opposite side.

        signal_t = pace_projection_t - implied_level_t      (lab.signals)
        proj > line  =>  bet OVER  (totals)  /  COVER HOME  (spread)
        proj < line  =>  bet UNDER (totals)  /  COVER AWAY  (spread)

Honest entry latency (i+1 fill) and the staleness/freshness guard are provided
by the ``Strategy`` itself (``entry_latency_min`` / ``max_stale_min``); the
realistic ``FillModel`` is the default at run time. We snap to a LISTED strike
at its REAL quoted price — never a fabricated 0.50 at-the-money fill.

REALISTIC-EXECUTION VERDICTS (``research/ALPHA_FINDINGS.md``, 2026-06-08):

* TOTALS — **DEAD**. The full-season gate PASS (+6.21c/ct @ 2c) was a
  0.50-at-the-money-fill ARTIFACT. The real listed over contract on the signal
  side already prices the pace move (~0.5555), so under realistic execution
  (real strike + ~1.5c half-spread + 2% Polymarket taker fee) the edge collapses
  to **net -1.00c/ct**, the gate fails at every spread, and the walk-forward
  drops 7/9 -> 3/8 months. RETRACTED. Kept here as a documented null / template.

* SPREADS — **REAL but UNCERTIFIED**. NOT a pure 0.50 artifact: the real fill
  mid is 0.5187 (not 0.50), so +5.22c of genuine residual gross survives;
  realistic taker net is **~+1.9c/ct** (+1.88c at a 1.5c half-spread + 2% fee;
  +3.43c at zero spread). It still FAILS the promotion gate (bootstrap CI lo < 0
  at any nonzero spread, season/parity stability fail, walk-forward 5/8), so it
  is PROMISING-BUT-UNCERTIFIED and marginal as a taker — it would need maker
  fills or more seasons to certify. This is the one real residual mispricing
  found in the data.

Treat any quoted edge that assumes a 0.50 / flat-cost fill as a STATISTICAL
signal only, never a tradeable edge. Realistic execution is the default here.

Sibling lab modules (``lab.signals``, ``lab.strategy``, ``lab.execution``) are
imported LAZILY inside the factory functions so this module imports cleanly in
an isolated worktree that only has ``research.lab.types``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from research.lab.types import TOTAL, Panel

if TYPE_CHECKING:  # only for type hints; never imported at runtime in isolation
    from research.lab.strategy import Strategy

# Pre-registered defaults shared by both markets (mirror the original scripts:
# thresh in point-units, the [min,max] elapsed window in seconds).
DEFAULT_THRESH = 6.0
DEFAULT_MIN_ELAPSED = 720.0
DEFAULT_MAX_ELAPSED = 2520.0
DEFAULT_ENTRY_LATENCY_MIN = 1.0
DEFAULT_MAX_STALE_MIN = 2.0

# Side labels (human-readable; the FillModel maps them to the strike side).
TOTALS_SIDES = ("over", "under")
SPREAD_SIDES = ("cover_home", "cover_away")


def _anchoring_gap(panel: Panel) -> np.ndarray:
    """proj - implied_level, per minute (lab.signals lazily, numpy fallback).

    Prefers ``lab.signals.anchoring_gap`` so the substrate stays the single
    source of truth for the signal; falls back to an inline numpy computation
    (pace projection minus ``panel.mid``) when the sibling module is absent
    (isolated worktree / standalone tests).
    """
    # lab.signals.anchoring_gap projects from total POINTS (TOTAL market only,
    # per the signals contract). For SPREAD the projection must come from the
    # signed MARGIN, so use the market-aware numpy computation directly.
    if panel.market != TOTAL:
        return _anchoring_gap_numpy(panel)
    try:
        from research.lab import signals  # lazy: sibling may be absent
    except Exception:  # noqa: BLE001 — isolated worktree has no sibling
        return _anchoring_gap_numpy(panel)
    gap_fn = getattr(signals, "anchoring_gap", None)
    if gap_fn is None:
        return _anchoring_gap_numpy(panel)
    return np.asarray(gap_fn(panel), dtype=float)


def _anchoring_gap_numpy(panel: Panel) -> np.ndarray:
    """Inline gap: pace projection of the relevant accumulator minus the line.

    Totals project final points from ``panel.total``; spreads project final
    signed home margin from ``panel.margin``. Projection = level * 2880 /
    elapsed (NBA regulation seconds), guarded for the early-game divide.
    """
    elapsed = np.asarray(panel.elapsed_sec, dtype=float)
    mid = np.asarray(panel.mid, dtype=float)
    accum = panel.total if panel.market == TOTAL else panel.margin
    accum = np.asarray(accum, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        proj = np.where(elapsed > 120.0, accum * 2880.0 / elapsed, np.nan)
    return proj - mid


def _staleness_min(panel: Panel) -> np.ndarray:
    """Minutes since ``panel.mid`` last actually changed (freshness guard).

    Prefers ``lab.signals.staleness_min``; falls back to the same accumulate
    trick the original scripts used so the guard works standalone.
    """
    try:
        from research.lab import signals  # lazy
    except Exception:  # noqa: BLE001
        return _staleness_min_numpy(panel)
    stale_fn = getattr(signals, "staleness_min", None)
    if stale_fn is None:
        return _staleness_min_numpy(panel)
    return np.asarray(stale_fn(panel), dtype=float)


def _staleness_min_numpy(panel: Panel) -> np.ndarray:
    mid = np.asarray(panel.mid, dtype=float)
    n = len(mid)
    if n == 0:
        return np.zeros(0, dtype=float)
    changed = np.r_[True, np.abs(np.diff(mid)) > 1e-9]
    idx = np.arange(n)
    last_change = np.maximum.accumulate(np.where(changed, idx, -1))
    return (idx - last_change).astype(float)


def _entry_mask(panel: Panel, *, thresh: float, min_elapsed: float,
                max_elapsed: float, max_stale_min: float) -> np.ndarray:
    """Bool mask over minutes: True where the anchoring signal qualifies.

    A bar qualifies when the absolute pace-vs-line gap clears ``thresh`` inside
    the [min_elapsed, max_elapsed] window AND the line is fresh (staleness guard).
    The ``Strategy`` fires on the FIRST True per game and applies i+1 latency.
    """
    gap = _anchoring_gap(panel)
    elapsed = np.asarray(panel.elapsed_sec, dtype=float)
    stale = _staleness_min(panel)
    in_window = (elapsed >= min_elapsed) & (elapsed <= max_elapsed)
    fresh = stale <= max_stale_min
    fires = np.isfinite(gap) & (np.abs(gap) >= thresh)
    return in_window & fresh & fires


def _make_side(*, continuation: bool, over_label: str, under_label: str):
    """Build a side(panel, i) -> label callable for one market.

    ``continuation`` bets toward the pace (gap > 0 -> over / cover-home); the
    contrarian variant flips it. The label maps to the FillModel's strike side.
    """
    def side(panel: Panel, i: int) -> str:
        gap = _anchoring_gap(panel)
        proj_high = bool(gap[i] > 0)               # pace says the level runs high
        bet_high = proj_high if continuation else (not proj_high)
        return over_label if bet_high else under_label

    return side


def _build_strategy(*, name: str, over_label: str, under_label: str,
                    thresh: float, min_elapsed: float, max_elapsed: float,
                    entry_latency_min: float, max_stale_min: float,
                    continuation: bool) -> "Strategy":
    """Assemble a configured ``lab.strategy.Strategy`` (imported lazily).

    The market is carried by each ``Panel`` (``panel.market``), so the entry/
    side closures need no separate market argument.
    """
    from research.lab.strategy import Strategy  # lazy: sibling may be absent

    def entry(panel: Panel) -> np.ndarray:
        return _entry_mask(panel, thresh=thresh, min_elapsed=min_elapsed,
                           max_elapsed=max_elapsed, max_stale_min=max_stale_min)

    side = _make_side(continuation=continuation, over_label=over_label,
                      under_label=under_label)

    return Strategy(
        name=name,
        entry=entry,
        side=side,
        exit="settlement",
        entry_latency_min=entry_latency_min,
        max_stale_min=max_stale_min,
        min_elapsed=min_elapsed,
        max_elapsed=max_elapsed,
    )


def totals_anchoring(*, thresh: float = DEFAULT_THRESH,
                     min_elapsed: float = DEFAULT_MIN_ELAPSED,
                     max_elapsed: float = DEFAULT_MAX_ELAPSED,
                     entry_latency_min: float = DEFAULT_ENTRY_LATENCY_MIN,
                     max_stale_min: float = DEFAULT_MAX_STALE_MIN,
                     continuation: bool = True) -> "Strategy":
    """Totals pace-anchoring as a ``Strategy`` (port of ``totals_alpha.py``).

    Continuation: when the pace projection of final points exceeds the implied
    total by ``thresh``, bet OVER (line too low); when it falls short, bet UNDER.

    VERDICT: DEAD under realistic execution — a 0.50-fill artifact (net
    -1.00c/ct; see the module docstring / ``ALPHA_FINDINGS.md``). Retained as a
    documented null and as the template the spread edge is measured against.
    """
    return _build_strategy(
        name="totals_anchoring" + ("" if continuation else "_reversion"),
        over_label=TOTALS_SIDES[0], under_label=TOTALS_SIDES[1],
        thresh=thresh, min_elapsed=min_elapsed, max_elapsed=max_elapsed,
        entry_latency_min=entry_latency_min, max_stale_min=max_stale_min,
        continuation=continuation,
    )


def spread_anchoring(*, thresh: float = DEFAULT_THRESH,
                     min_elapsed: float = DEFAULT_MIN_ELAPSED,
                     max_elapsed: float = DEFAULT_MAX_ELAPSED,
                     entry_latency_min: float = DEFAULT_ENTRY_LATENCY_MIN,
                     max_stale_min: float = DEFAULT_MAX_STALE_MIN,
                     continuation: bool = True) -> "Strategy":
    """Spread/handicap pace-anchoring as a ``Strategy`` (port of ``spread_alpha.py``).

    Continuation: when the pace projection of the final signed HOME margin
    exceeds the implied margin by ``thresh``, COVER HOME (bet final margin >
    strike); when it falls short, COVER AWAY.

    VERDICT: REAL but UNCERTIFIED — the one genuine residual mispricing found,
    ~+1.9c/ct net taker (NOT a 0.50 artifact), but it fails the promotion gate
    as a taker (CI lo < 0 at nonzero spread). See module docstring /
    ``ALPHA_FINDINGS.md``. Realistic ``FillModel`` is the default at run time.
    """
    return _build_strategy(
        name="spread_anchoring" + ("" if continuation else "_reversion"),
        over_label=SPREAD_SIDES[0], under_label=SPREAD_SIDES[1],
        thresh=thresh, min_elapsed=min_elapsed, max_elapsed=max_elapsed,
        entry_latency_min=entry_latency_min, max_stale_min=max_stale_min,
        continuation=continuation,
    )
