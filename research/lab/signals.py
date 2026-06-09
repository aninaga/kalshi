"""research.lab.signals — composable signal primitives over a Panel.

Each function is PURE: it takes a :class:`research.lab.types.Panel` (plus small
scalar args) and returns a length-``n`` ``np.ndarray`` aligned to the panel's
per-minute index. No I/O, no sibling lab imports, no hidden state — so signals
compose freely and stay trivially testable on synthetic fixtures.

The core mechanism (see ``research/scripts/totals_alpha.py`` /
``spread_alpha.py`` / ``totals_realistic.py``): live scoring *pace* projects a
final level, the market's implied level (``panel.mid``) may lag it, and the gap
between the two is the anchoring signal. Staleness measures how long the implied
line has sat unchanged — a sparse historical ladder makes a stale line vs. live
play a measurement artifact, not a tradeable edge, so strategies guard on it.
"""
from __future__ import annotations

import numpy as np

from research.lab.types import Panel

# Regulation game length in seconds; pace projection scales the live level to it.
_GAME_SEC = 2880.0
# Below this much elapsed time a pace projection is too noisy to trust (matches
# the ``e > 120`` guard in the source alpha scripts): emit NaN instead.
_MIN_ELAPSED_SEC = 120.0
_EPS = 1e-9


def pace_projection(panel: Panel) -> np.ndarray:
    """Pace-projected final total: ``total * 2880 / elapsed`` (TOTAL market).

    The live scoring rate extrapolated to a full regulation game. Bars with
    elapsed time at or below :data:`_MIN_ELAPSED_SEC` are NaN (projection is too
    noisy that early), mirroring the ``e > 120`` guard in the source scripts.
    """
    elapsed = np.asarray(panel.elapsed_sec, dtype=float)
    total = np.asarray(panel.total, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        proj = total * _GAME_SEC / elapsed
    return np.where(elapsed > _MIN_ELAPSED_SEC, proj, np.nan)


def anchoring_gap(panel: Panel) -> np.ndarray:
    """Pace projection minus the implied level: ``pace_projection - panel.mid``.

    Positive => pace says the final total will exceed the market line (line too
    low); negative => the opposite. This is the raw pace-vs-line alpha signal.
    """
    return pace_projection(panel) - np.asarray(panel.mid, dtype=float)


def implied_level(panel: Panel) -> np.ndarray:
    """The market's implied 0.5-level (``panel.mid``), as a fresh array.

    Convenience alias so signal pipelines can name the implied level explicitly;
    returns a copy so callers can mutate the result without touching the panel.
    """
    return np.asarray(panel.mid, dtype=float).copy()


def calibration_gap(panel: Panel, realized) -> np.ndarray:
    """Realized outcome minus the implied level: ``realized - panel.mid``.

    ``realized`` is the eventual settled level (e.g. final total / final margin),
    a scalar broadcast across all bars or a length-``n`` array. Positive means the
    market under-priced the level relative to what actually happened.
    """
    mid = np.asarray(panel.mid, dtype=float)
    realized_arr = np.asarray(realized, dtype=float)
    return np.broadcast_to(realized_arr, mid.shape).astype(float) - mid


def staleness_min(panel: Panel) -> np.ndarray:
    """Minutes since ``panel.mid`` last changed.

    Zero at the first bar and at every bar where the implied level moves;
    increments by one for each consecutive bar it stays flat. Bars are per-minute
    in a Panel, so the count is minutes. This is the freshness guard from the
    source scripts (a long-stale line vs. live play is a measurement artifact).
    """
    mid = np.asarray(panel.mid, dtype=float)
    n = mid.shape[0]
    if n == 0:
        return np.zeros(0, dtype=float)
    idx = np.arange(n)
    changed = np.r_[True, np.abs(np.diff(mid)) > _EPS]
    last_change = np.maximum.accumulate(np.where(changed, idx, -1))
    return (idx - last_change).astype(float)


def rolling(panel: Panel, key: str, window: int) -> np.ndarray:
    """Trailing rolling mean of a panel array, NaN-aware.

    ``key`` selects a length-``n`` series: a :class:`Panel` attribute
    (``"mid"``, ``"total"``, ``"margin"``, ``"elapsed_sec"``) or a
    ``panel.features`` entry. The window is trailing and inclusive; bars before a
    full window use the partial trailing window. Bars whose entire trailing window
    is NaN stay NaN.
    """
    x = _series(panel, key)
    return _rolling_mean(x, window)


def zscore(x: np.ndarray, window: int) -> np.ndarray:
    """Trailing rolling z-score of ``x`` over ``window``, NaN-aware.

    ``(x - rolling_mean) / rolling_std`` using the trailing inclusive window.
    Bars with fewer than two finite observations in the window, or zero rolling
    standard deviation, are NaN (z-score undefined).
    """
    x = np.asarray(x, dtype=float)
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    n = x.shape[0]
    out = np.full(n, np.nan, dtype=float)
    for i in range(n):
        lo = max(0, i - window + 1)
        w = x[lo : i + 1]
        finite = w[np.isfinite(w)]
        if finite.size < 2:
            continue
        sd = finite.std()
        if sd <= _EPS:
            continue
        out[i] = (x[i] - finite.mean()) / sd
    return out


def _series(panel: Panel, key: str) -> np.ndarray:
    """Resolve ``key`` to a length-n float array (panel attr or feature)."""
    if key in panel.features:
        return np.asarray(panel.features[key], dtype=float)
    if hasattr(panel, key):
        return np.asarray(getattr(panel, key), dtype=float)
    raise KeyError(
        f"unknown signal key {key!r}: not a Panel attribute or feature "
        f"(features: {sorted(panel.features)})"
    )


def _rolling_mean(x: np.ndarray, window: int) -> np.ndarray:
    """Trailing inclusive rolling mean; NaN where the window is all-NaN."""
    x = np.asarray(x, dtype=float)
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    n = x.shape[0]
    out = np.full(n, np.nan, dtype=float)
    for i in range(n):
        lo = max(0, i - window + 1)
        w = x[lo : i + 1]
        finite = w[np.isfinite(w)]
        if finite.size:
            out[i] = finite.mean()
    return out
