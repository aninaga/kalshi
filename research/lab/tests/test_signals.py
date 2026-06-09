"""Unit tests for research.lab.signals — pure signal primitives over a Panel.

All tests run on synthetic fixtures only (no real-data cache required).
"""
from __future__ import annotations

import numpy as np
import pytest

from research.lab.types import TOTAL, SPREAD, WINNER, Panel, synthetic_panel
from research.lab import signals


# --------------------------------------------------------------------------- #
# shapes: every signal returns a length-n ndarray
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("market", [TOTAL, SPREAD, WINNER])
def test_shapes_are_length_n(market):
    p = synthetic_panel(market=market, n=48, seed=1)
    assert pace_shape(signals.pace_projection(p), p)
    assert pace_shape(signals.anchoring_gap(p), p)
    assert pace_shape(signals.implied_level(p), p)
    assert pace_shape(signals.calibration_gap(p, 100.0), p)
    assert pace_shape(signals.staleness_min(p), p)
    assert pace_shape(signals.rolling(p, "mid", 5), p)
    assert pace_shape(signals.zscore(np.asarray(p.mid, float), 5), p)


def pace_shape(arr, panel) -> bool:
    return isinstance(arr, np.ndarray) and arr.shape == (panel.n,)


# --------------------------------------------------------------------------- #
# pace_projection
# --------------------------------------------------------------------------- #

def test_pace_projection_formula():
    p = synthetic_panel(market=TOTAL, n=48, seed=2)
    proj = signals.pace_projection(p)
    elapsed = np.asarray(p.elapsed_sec, float)
    mask = elapsed > 120.0
    expected = np.asarray(p.total, float)[mask] * 2880.0 / elapsed[mask]
    assert np.allclose(proj[mask], expected)


def test_pace_projection_nan_when_too_early():
    p = synthetic_panel(market=TOTAL, n=48, seed=3)
    # force an early bar with tiny elapsed time
    p.elapsed_sec = p.elapsed_sec.copy()
    p.elapsed_sec[0] = 30.0
    proj = signals.pace_projection(p)
    assert np.isnan(proj[0])
    assert np.isfinite(proj[-1])


# --------------------------------------------------------------------------- #
# anchoring_gap == pace_projection - mid
# --------------------------------------------------------------------------- #

def test_anchoring_gap_is_projection_minus_mid():
    p = synthetic_panel(market=TOTAL, n=48, seed=4)
    gap = signals.anchoring_gap(p)
    expected = signals.pace_projection(p) - np.asarray(p.mid, float)
    # NaN-aware equality (early bars are NaN in both)
    np.testing.assert_array_equal(np.isnan(gap), np.isnan(expected))
    fin = np.isfinite(gap)
    assert np.allclose(gap[fin], expected[fin])


# --------------------------------------------------------------------------- #
# implied_level == mid, and is a copy (does not alias)
# --------------------------------------------------------------------------- #

def test_implied_level_equals_mid_and_is_copy():
    p = synthetic_panel(market=TOTAL, n=24, seed=5)
    lvl = signals.implied_level(p)
    assert np.allclose(lvl, np.asarray(p.mid, float))
    lvl[0] += 999.0
    assert not np.allclose(lvl, np.asarray(p.mid, float))  # mutation isolated


# --------------------------------------------------------------------------- #
# calibration_gap == realized - mid (scalar broadcast and array)
# --------------------------------------------------------------------------- #

def test_calibration_gap_scalar():
    p = synthetic_panel(market=TOTAL, n=24, seed=6)
    realized = float(p.final_total)
    gap = signals.calibration_gap(p, realized)
    assert np.allclose(gap, realized - np.asarray(p.mid, float))


def test_calibration_gap_array():
    p = synthetic_panel(market=TOTAL, n=24, seed=7)
    realized = np.asarray(p.total, float)
    gap = signals.calibration_gap(p, realized)
    assert np.allclose(gap, realized - np.asarray(p.mid, float))


# --------------------------------------------------------------------------- #
# staleness_min: 0 where mid changes, increments where flat
# --------------------------------------------------------------------------- #

def test_staleness_zero_when_mid_always_changes():
    p = synthetic_panel(market=TOTAL, n=10, seed=8)
    p.mid = np.arange(10, dtype=float)  # strictly changing every bar
    stale = signals.staleness_min(p)
    assert np.all(stale == 0.0)


def test_staleness_increments_when_flat():
    p = synthetic_panel(market=TOTAL, n=8, seed=9)
    # mid: change, flat, flat, change, flat, change, flat, flat
    p.mid = np.array([10.0, 10.0, 10.0, 12.0, 12.0, 15.0, 15.0, 15.0])
    stale = signals.staleness_min(p)
    expected = np.array([0.0, 1.0, 2.0, 0.0, 1.0, 0.0, 1.0, 2.0])
    np.testing.assert_array_equal(stale, expected)


def test_staleness_first_bar_is_zero():
    p = synthetic_panel(market=TOTAL, n=5, seed=10)
    p.mid = np.array([7.0, 7.0, 7.0, 7.0, 7.0])  # never changes
    stale = signals.staleness_min(p)
    assert stale[0] == 0.0
    assert np.array_equal(stale, np.arange(5, dtype=float))


# --------------------------------------------------------------------------- #
# rolling: window mean, NaN-aware
# --------------------------------------------------------------------------- #

def test_rolling_mean_basic():
    p = synthetic_panel(market=TOTAL, n=6, seed=11)
    p.mid = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    r = signals.rolling(p, "mid", 3)
    expected = np.array([1.0, 1.5, 2.0, 3.0, 4.0, 5.0])  # trailing partial windows
    assert np.allclose(r, expected)


def test_rolling_window_one_is_identity():
    p = synthetic_panel(market=TOTAL, n=6, seed=12)
    r = signals.rolling(p, "total", 1)
    assert np.allclose(r, np.asarray(p.total, float))


def test_rolling_nan_aware():
    p = synthetic_panel(market=TOTAL, n=5, seed=13)
    p.mid = np.array([1.0, np.nan, 3.0, np.nan, np.nan])
    r = signals.rolling(p, "mid", 2)
    # i0: [1]->1; i1: [1,nan]->1; i2: [nan,3]->3; i3: [3,nan]->3; i4: [nan,nan]->nan
    assert np.allclose(r[:4], np.array([1.0, 1.0, 3.0, 3.0]))
    assert np.isnan(r[4])


def test_rolling_feature_key():
    p = synthetic_panel(market=TOTAL, n=12, seed=14)
    r = signals.rolling(p, "pace_ppm", 4)  # a feature, not an attribute
    assert r.shape == (p.n,)
    assert np.all(np.isfinite(r))


def test_rolling_unknown_key_raises():
    p = synthetic_panel(market=TOTAL, n=6, seed=15)
    with pytest.raises(KeyError):
        signals.rolling(p, "not_a_real_key", 3)


def test_rolling_bad_window_raises():
    p = synthetic_panel(market=TOTAL, n=6, seed=16)
    with pytest.raises(ValueError):
        signals.rolling(p, "mid", 0)


# --------------------------------------------------------------------------- #
# zscore: window normalization, NaN-aware
# --------------------------------------------------------------------------- #

def test_zscore_constant_is_nan():
    x = np.full(10, 5.0)
    z = signals.zscore(x, 4)
    assert np.all(np.isnan(z))  # zero std => undefined


def test_zscore_matches_manual_full_window():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    z = signals.zscore(x, 5)
    w = x  # full trailing window at last bar
    expected_last = (x[-1] - w.mean()) / w.std()
    assert np.isclose(z[-1], expected_last)


def test_zscore_first_bar_nan_insufficient_data():
    x = np.array([3.0, 1.0, 4.0, 1.0, 5.0])
    z = signals.zscore(x, 3)
    assert np.isnan(z[0])  # only 1 obs => can't compute std
    assert np.isfinite(z[2])


def test_zscore_nan_aware():
    x = np.array([1.0, np.nan, 2.0, 3.0, 4.0])
    z = signals.zscore(x, 3)
    # window at i4 = [2,3,4] all finite -> finite z
    assert np.isfinite(z[4])
    # i1 window [1, nan] -> only 1 finite obs -> nan
    assert np.isnan(z[1])


def test_zscore_bad_window_raises():
    with pytest.raises(ValueError):
        signals.zscore(np.arange(5.0), 0)


# --------------------------------------------------------------------------- #
# E2E smoke: compute every signal on a synthetic panel; assert finite + len-n
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("market", [TOTAL, SPREAD, WINNER])
def test_smoke_all_signals_finite_and_length_n(market):
    p = synthetic_panel(market=market, n=48, seed=42, signal=0.1)
    n = p.n

    results = {
        "pace_projection": signals.pace_projection(p),
        "anchoring_gap": signals.anchoring_gap(p),
        "implied_level": signals.implied_level(p),
        "calibration_gap": signals.calibration_gap(p, 100.0),
        "staleness_min": signals.staleness_min(p),
        "rolling_mid": signals.rolling(p, "mid", 5),
        "zscore_mid": signals.zscore(np.asarray(p.mid, float), 5),
    }
    for name, arr in results.items():
        assert isinstance(arr, np.ndarray), name
        assert arr.shape == (n,), name

    # Levels / gaps that should be fully finite (no early-bar NaN):
    for name in ("implied_level", "calibration_gap", "staleness_min", "rolling_mid"):
        assert np.all(np.isfinite(results[name])), name

    # Pace-based signals: finite for all but the very-early bars (elapsed <= 120s).
    early = np.asarray(p.elapsed_sec, float) <= 120.0
    for name in ("pace_projection", "anchoring_gap"):
        assert np.all(np.isfinite(results[name][~early])), name


def test_smoke_multiple_seeds_no_exceptions():
    for seed in range(5):
        p = synthetic_panel(market=TOTAL, n=30, seed=seed)
        signals.pace_projection(p)
        signals.anchoring_gap(p)
        signals.implied_level(p)
        signals.calibration_gap(p, p.final_total)
        signals.staleness_min(p)
        signals.rolling(p, "total", 3)
        signals.zscore(np.asarray(p.total, float), 4)
