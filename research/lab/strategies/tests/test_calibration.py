"""Standalone tests for research.lab.strategies.calibration.

These run WITHOUT the sibling lab modules (``lab.strategy`` / ``lab.signals``):
we install a minimal stub ``research.lab.strategy`` into ``sys.modules`` exposing
a ``Strategy`` dataclass that matches the CONTRACT.md signature, so the factory
functions can construct their objects. ``lab.signals`` is simply absent, which
exercises the inline fallback in ``calibration._price_level``.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pytest


# --------------------------------------------------------------------------- #
# Stub the sibling lab.strategy module BEFORE importing calibration's factories.
# --------------------------------------------------------------------------- #
@dataclass
class _StubStrategy:
    """Minimal Strategy matching research/lab/CONTRACT.md (Unit 4)."""

    name: str
    entry: Callable
    side: Callable
    exit: object = "settlement"
    entry_latency_min: float = 1.0
    max_stale_min: float = 2.0
    min_elapsed: float = 600.0
    max_elapsed: float = 2520.0


@pytest.fixture(autouse=True)
def _stub_strategy_module(monkeypatch):
    mod = types.ModuleType("research.lab.strategy")
    mod.Strategy = _StubStrategy
    monkeypatch.setitem(sys.modules, "research.lab.strategy", mod)
    yield


from research.lab.strategies import calibration  # noqa: E402
from research.lab.types import Panel, WINNER, TOTAL, synthetic_panel  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures: panels with a KNOWN price level so assertions are deterministic.
# --------------------------------------------------------------------------- #
def _winner_panel(mid: np.ndarray) -> Panel:
    n = len(mid)
    elapsed = np.linspace(0.0, 2820.0, n)
    zeros = np.zeros(n)
    return Panel(
        game_id="t", date="2025-11-01", market=WINNER,
        home_team="HOM", away_team="AWY",
        minute_ts=1_700_000_000.0 + elapsed,
        elapsed_sec=elapsed, margin=zeros, total=zeros,
        mid=np.asarray(mid, dtype=float), home_won=1.0,
    )


def test_favorite_longshot_constructs():
    strat = calibration.favorite_longshot(p_lo=0.1, p_hi=0.9)
    assert isinstance(strat, _StubStrategy)
    assert "favorite_longshot" in strat.name
    assert strat.exit == "settlement"
    assert callable(strat.entry) and callable(strat.side)


def test_favorite_longshot_fires_at_near_lock_and_backs():
    # price climbs into the near-lock tail (>0.9) at the END (within elapsed window).
    mid = np.linspace(0.5, 0.97, 48)
    panel = _winner_panel(mid)
    strat = calibration.favorite_longshot(market=WINNER, p_lo=0.1, p_hi=0.9)

    mask = strat.entry(panel)
    assert mask.sum() == 1, "exactly one entry (first qualifying bar)"
    i = int(np.flatnonzero(mask)[0])
    assert mid[i] > 0.9, "fires only at the extreme price"
    # near-lock presumed underpriced -> BACK home.
    assert strat.side(panel, i) == "long_home"


def test_favorite_longshot_fires_at_longshot_and_fades():
    # price drops into the longshot tail (<0.1).
    mid = np.linspace(0.5, 0.03, 48)
    panel = _winner_panel(mid)
    strat = calibration.favorite_longshot(market=WINNER, p_lo=0.1, p_hi=0.9)

    mask = strat.entry(panel)
    assert mask.sum() == 1
    i = int(np.flatnonzero(mask)[0])
    assert mid[i] < 0.1
    # longshot presumed overpriced -> FADE home (back away).
    assert strat.side(panel, i) == "long_away"


def test_favorite_longshot_no_fire_when_calibrated():
    # price stays in the calibrated middle -> never extreme -> no entry.
    panel = _winner_panel(np.full(48, 0.5))
    strat = calibration.favorite_longshot(p_lo=0.1, p_hi=0.9)
    assert strat.entry(panel).sum() == 0


def test_favorite_longshot_respects_elapsed_window():
    # extreme only in the first ~minute, before min_elapsed -> filtered out.
    mid = np.full(48, 0.5)
    mid[0] = 0.02
    panel = _winner_panel(mid)
    strat = calibration.favorite_longshot(p_lo=0.1, p_hi=0.9, min_elapsed=600.0)
    assert strat.entry(panel).sum() == 0


def test_favorite_longshot_totals_uses_ladder_and_over_under_labels():
    # TOTAL market: price level read off the ladder; labels are over/under.
    panel = synthetic_panel(market=TOTAL, seed=3)
    strat = calibration.favorite_longshot(market=TOTAL, p_lo=0.1, p_hi=0.9)
    # side labels for non-winner markets must be over/under.
    price = calibration._price_level(panel)
    assert price.shape == (panel.n,)
    assert np.all((price >= 0.0) & (price <= 1.0))
    for i in range(panel.n):
        assert strat.side(panel, i) in ("over", "under")


def test_favorite_longshot_bad_thresholds():
    with pytest.raises(ValueError):
        calibration.favorite_longshot(p_lo=0.9, p_hi=0.1)


# --------------------------------------------------------------------------- #
# calibration_bucket
# --------------------------------------------------------------------------- #
def test_calibration_bucket_constructs_with_default_prior():
    strat = calibration.calibration_bucket(edge_thresh=0.03)
    assert isinstance(strat, _StubStrategy)
    assert "calibration_bucket" in strat.name
    assert callable(strat.entry) and callable(strat.side)


def test_calibration_bucket_fires_in_flagged_bucket_and_sides_by_bias():
    # bins -> 4 buckets [0,.25,.5,.75,1]; flag bucket 0 over (bias<0), bucket 3 under (>0).
    bins = (0.0, 0.25, 0.5, 0.75, 1.0)
    bias = (-0.05, 0.0, 0.0, 0.05)
    strat = calibration.calibration_bucket(
        market=WINNER, bias=bias, price_bins=bins, edge_thresh=0.03
    )

    # price sits in the UNDERpriced top bucket -> back home.
    panel_hi = _winner_panel(np.full(48, 0.85))
    mask_hi = strat.entry(panel_hi)
    assert mask_hi.sum() == 1
    i_hi = int(np.flatnonzero(mask_hi)[0])
    assert strat.side(panel_hi, i_hi) == "long_home"

    # price sits in the OVERpriced bottom bucket -> fade home.
    panel_lo = _winner_panel(np.full(48, 0.10))
    mask_lo = strat.entry(panel_lo)
    assert mask_lo.sum() == 1
    i_lo = int(np.flatnonzero(mask_lo)[0])
    assert strat.side(panel_lo, i_lo) == "long_away"


def test_calibration_bucket_no_fire_in_unflagged_bucket():
    bins = (0.0, 0.25, 0.5, 0.75, 1.0)
    bias = (-0.05, 0.0, 0.0, 0.05)
    strat = calibration.calibration_bucket(
        market=WINNER, bias=bias, price_bins=bins, edge_thresh=0.03
    )
    # price in the calibrated middle bucket (bias 0) -> never fires.
    panel = _winner_panel(np.full(48, 0.6))
    assert strat.entry(panel).sum() == 0


def test_calibration_bucket_default_prior_is_favorite_longshot():
    # No bias supplied: extremes flagged, middle calibrated.
    strat = calibration.calibration_bucket(edge_thresh=0.03)
    # bottom bucket -> overpriced -> fade.
    panel_lo = _winner_panel(np.full(48, 0.02))
    m = strat.entry(panel_lo)
    assert m.sum() == 1
    assert strat.side(panel_lo, int(np.flatnonzero(m)[0])) == "long_away"
    # top bucket -> underpriced -> back.
    panel_hi = _winner_panel(np.full(48, 0.98))
    m2 = strat.entry(panel_hi)
    assert m2.sum() == 1
    assert strat.side(panel_hi, int(np.flatnonzero(m2)[0])) == "long_home"


def test_calibration_bucket_validates_bias_length():
    with pytest.raises(ValueError):
        calibration.calibration_bucket(bias=(0.1, 0.2), price_bins=(0.0, 0.5, 1.0, 1.5))


def test_calibration_bucket_validates_bins_monotonic():
    with pytest.raises(ValueError):
        calibration.calibration_bucket(price_bins=(0.0, 0.5, 0.5, 1.0))


# --------------------------------------------------------------------------- #
# Smoke: both strategies on a synthetic_panel, exercising entry + side end-to-end.
# --------------------------------------------------------------------------- #
def test_smoke_both_on_synthetic_panel():
    panel = synthetic_panel(market=WINNER, seed=1)
    for strat in (
        calibration.favorite_longshot(market=WINNER, p_lo=0.2, p_hi=0.8),
        calibration.calibration_bucket(market=WINNER, edge_thresh=0.03),
    ):
        mask = strat.entry(panel)
        assert mask.dtype == bool and mask.shape == (panel.n,)
        assert mask.sum() <= 1  # at most one trade per game
        for i in np.flatnonzero(mask):
            assert strat.side(panel, int(i)) in ("long_home", "long_away")
