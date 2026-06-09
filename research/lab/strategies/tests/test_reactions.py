"""Standalone tests for research.lab.strategies.reactions.

These run with ONLY ``research.lab.types`` + synthetic fixtures + this module:
the sibling ``research.lab.strategy`` (and ``research.lab.signals``) may not be
merged in this worktree, so we STUB ``research.lab.strategy`` in ``sys.modules``
with a minimal ``Strategy`` matching ``research/lab/CONTRACT.md`` before importing
the module under test. Each test constructs a Strategy and exercises its
entry/side over a synthetic Panel built so a known trigger fires.
"""
from __future__ import annotations

import sys
import types as _pytypes
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pytest

from research.lab.types import Panel, WINNER, synthetic_panel


# --------------------------------------------------------------------------- #
# Stub the sibling lab.strategy module (not merged in this worktree).
# --------------------------------------------------------------------------- #

@dataclass
class _StubStrategy:
    """Minimal Strategy mirroring research/lab/CONTRACT.md §strategy."""
    name: str
    entry: Callable[[Panel], np.ndarray]
    side: Callable[[Panel, int], str]
    exit: object = "settlement"
    entry_latency_min: float = 1.0
    max_stale_min: float = 2.0
    min_elapsed: float = 600.0
    max_elapsed: float = 2520.0


def _install_strategy_stub() -> None:
    if "research.lab.strategy" in sys.modules:
        return
    mod = _pytypes.ModuleType("research.lab.strategy")
    mod.Strategy = _StubStrategy  # type: ignore[attr-defined]
    sys.modules["research.lab.strategy"] = mod


_install_strategy_stub()

from research.lab.strategies import reactions  # noqa: E402  (after stub install)


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #

def _winner_panel(mid: np.ndarray, *, margin=None, n=None, features=None,
                  home_won=1.0) -> Panel:
    """A WINNER Panel with a hand-set mid series for deterministic triggers."""
    mid = np.asarray(mid, float)
    n = len(mid) if n is None else n
    elapsed = np.linspace(0.0, 2820.0, n)
    minute_ts = 1_700_000_000.0 + (elapsed / 60.0).astype(int) * 60.0
    margin = np.zeros(n) if margin is None else np.asarray(margin, float)
    return Panel(
        game_id="syn", date="2025-11-01", market=WINNER,
        home_team="HOM", away_team="AWY",
        minute_ts=minute_ts, elapsed_sec=elapsed, margin=margin,
        total=np.zeros(n), mid=mid, ladder={}, features=features or {},
        home_won=home_won, final_margin=1.0 if home_won else -1.0,
    )


def _first_true(mask: np.ndarray) -> int:
    idx = np.flatnonzero(np.asarray(mask, bool))
    assert idx.size > 0, "entry mask never fires on the constructed trigger"
    return int(idx[0])


# --------------------------------------------------------------------------- #
# fair_value_gap.
# --------------------------------------------------------------------------- #

def test_fair_value_gap_fires_short_when_market_rich() -> None:
    # Tied game => fair WP near 0.5; force market mid well ABOVE fair so the gap is
    # large and positive (market too high on home) -> reversion -> short home.
    n = 48
    margin = np.zeros(n)                 # tied -> fair_wp ~ 0.5
    mid = np.full(n, 0.90)              # market far above fair
    p = _winner_panel(mid, margin=margin, n=n)
    strat = reactions.fair_value_gap(gap_thresh=0.06)
    mask = strat.entry(p)
    i = _first_true(mask)
    assert strat.side(p, i) == "short_home"
    assert strat.name == "reactions.fair_value_gap"


def test_fair_value_gap_fires_long_when_market_cheap() -> None:
    n = 48
    margin = np.full(n, 25.0)            # deep home lead -> fair_wp == 0.98
    mid = np.full(n, 0.50)              # market far BELOW fair -> long home
    p = _winner_panel(mid, margin=margin, n=n)
    strat = reactions.fair_value_gap(gap_thresh=0.06)
    i = _first_true(strat.entry(p))
    assert strat.side(p, i) == "long_home"


def test_fair_value_gap_no_fire_when_aligned() -> None:
    n = 48
    margin = np.zeros(n)                 # mid margin bucket -> fair_wp ~ 0.5
    mid = np.full(n, 0.50)
    p = _winner_panel(mid, margin=margin, n=n)
    strat = reactions.fair_value_gap(gap_thresh=0.06)
    assert not strat.entry(p).any()


# --------------------------------------------------------------------------- #
# term_structure_rv.
# --------------------------------------------------------------------------- #

def test_term_structure_rv_fires_long_when_model_above_market() -> None:
    # Positive margin with low remaining pace => model WP high; depress market mid
    # so gap = mwp - market > tau -> model says leader underpriced -> long home.
    n = 48
    margin = np.full(n, 8.0)
    rem = np.full(n, 10.0)               # small R -> tight dispersion -> mwp ~ 1
    mid = np.full(n, 0.50)
    p = _winner_panel(mid, margin=margin, n=n, features={"rem_pts": rem})
    strat = reactions.term_structure_rv(tau=0.05, k=1.30)
    i = _first_true(strat.entry(p))
    assert strat.side(p, i) == "long_home"


def test_term_structure_rv_fires_short_when_model_below_market() -> None:
    n = 48
    margin = np.full(n, -8.0)            # home behind -> mwp low
    rem = np.full(n, 10.0)
    mid = np.full(n, 0.50)              # market still at 0.5 -> gap < -tau -> short
    p = _winner_panel(mid, margin=margin, n=n, features={"rem_pts": rem})
    strat = reactions.term_structure_rv(tau=0.05, k=1.30)
    i = _first_true(strat.entry(p))
    assert strat.side(p, i) == "short_home"


def test_term_structure_rv_uses_default_rem_when_feature_absent() -> None:
    # No rem_pts feature -> flat fallback; still fires on a big margin/market gap.
    n = 48
    margin = np.full(n, 12.0)
    mid = np.full(n, 0.40)
    p = _winner_panel(mid, margin=margin, n=n)        # no rem_pts feature
    strat = reactions.term_structure_rv(tau=0.05)
    assert strat.entry(p).any()


# --------------------------------------------------------------------------- #
# closing_line_value.
# --------------------------------------------------------------------------- #

def test_clv_continuation_long_when_home_rising() -> None:
    # Pregame mid drifts UP from the open by more than thresh -> continuation buys.
    mid = np.array([0.50, 0.51, 0.55, 0.58])   # drift at idx>=2 exceeds 0.02
    p = _winner_panel(mid)
    strat = reactions.closing_line_value(thresh=0.02, side_mode="continuation")
    i = _first_true(strat.entry(p))
    assert i == 2
    assert strat.side(p, i) == "long_home"


def test_clv_reversion_flips_side() -> None:
    mid = np.array([0.50, 0.51, 0.55, 0.58])   # rising
    p = _winner_panel(mid)
    strat = reactions.closing_line_value(thresh=0.02, side_mode="reversion")
    i = _first_true(strat.entry(p))
    assert strat.side(p, i) == "short_home"   # reversion fades a rising line


def test_clv_continuation_short_when_home_falling() -> None:
    mid = np.array([0.60, 0.59, 0.55, 0.52])   # falling => buy away
    p = _winner_panel(mid)
    strat = reactions.closing_line_value(thresh=0.02, side_mode="continuation")
    i = _first_true(strat.entry(p))
    assert strat.side(p, i) == "short_home"


def test_clv_rejects_bad_side_mode() -> None:
    with pytest.raises(ValueError):
        reactions.closing_line_value(side_mode="nonsense")


# --------------------------------------------------------------------------- #
# substitution_latency.
# --------------------------------------------------------------------------- #

def test_substitution_latency_follow_star_out() -> None:
    # Star OUT marker (negative) at one bar; follow bets the fair direction (down).
    n = 48
    sub = np.zeros(n)
    sub[20] = -1.0
    p = _winner_panel(np.full(n, 0.5), n=n, features={"sub_signed": sub})
    strat = reactions.substitution_latency(side_mode="follow")
    i = _first_true(strat.entry(p))
    assert i == 20
    assert strat.side(p, i) == "short_home"     # out -> down -> short home


def test_substitution_latency_follow_star_in() -> None:
    n = 48
    sub = np.zeros(n)
    sub[15] = 1.0
    p = _winner_panel(np.full(n, 0.5), n=n, features={"sub_signed": sub})
    strat = reactions.substitution_latency(side_mode="follow")
    i = _first_true(strat.entry(p))
    assert strat.side(p, i) == "long_home"      # in -> up -> long home


def test_substitution_latency_fade_inverts() -> None:
    n = 48
    sub = np.zeros(n)
    sub[15] = 1.0                                 # star in
    p = _winner_panel(np.full(n, 0.5), n=n, features={"sub_signed": sub})
    strat = reactions.substitution_latency(side_mode="fade")
    i = _first_true(strat.entry(p))
    assert strat.side(p, i) == "short_home"      # fade the up reaction


def test_substitution_latency_no_fire_without_event() -> None:
    n = 48
    p = _winner_panel(np.full(n, 0.5), n=n, features={"sub_signed": np.zeros(n)})
    strat = reactions.substitution_latency()
    assert not strat.entry(p).any()


def test_substitution_latency_rejects_bad_side_mode() -> None:
    with pytest.raises(ValueError):
        reactions.substitution_latency(side_mode="nope")


# --------------------------------------------------------------------------- #
# Cross-cutting: registry + staleness helper + smoke over synthetic_panel.
# --------------------------------------------------------------------------- #

def test_registry_constructs_every_strategy() -> None:
    for fac in reactions.REACTIONS.values():
        strat = fac()
        assert isinstance(strat, _StubStrategy)
        assert callable(strat.entry) and callable(strat.side)


def test_smoke_entry_side_over_synthetic_winner_panel() -> None:
    # Exercise every strategy's entry/side over a stock synthetic_panel (WINNER):
    # entry returns a bool mask of the right length; side returns a valid label.
    p = synthetic_panel(market=WINNER, n=48, seed=3)
    for fac in reactions.REACTIONS.values():
        strat = fac()
        mask = strat.entry(p)
        assert mask.dtype == bool and len(mask) == p.n
        idx = np.flatnonzero(mask)
        if idx.size:
            assert strat.side(p, int(idx[0])) in {"long_home", "short_home"}
