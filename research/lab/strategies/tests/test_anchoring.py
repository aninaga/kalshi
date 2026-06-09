"""Standalone tests for research.lab.strategies.anchoring.

These pass with ONLY ``research.lab.types`` + this module present: the sibling
lab modules (``lab.strategy`` / ``lab.signals``) are STUBBED in via a fake
module so the factories' lazy imports resolve, and the entry/side logic is
exercised directly on synthetic / hand-built Panels. No real data, no
``Strategy.run``.
"""
from __future__ import annotations

import sys
import types as _pytypes
import unittest
from dataclasses import dataclass
from typing import Callable

import numpy as np

from research.lab import strategies  # package import smoke
from research.lab.strategies import anchoring
from research.lab.types import SPREAD, TOTAL, Panel, synthetic_panel


@dataclass
class _StubStrategy:
    """Minimal stand-in for lab.strategy.Strategy (siblings absent in worktree)."""
    name: str
    entry: Callable
    side: Callable
    exit: object = "settlement"
    entry_latency_min: float = 1.0
    max_stale_min: float = 2.0
    min_elapsed: float = 600.0
    max_elapsed: float = 2520.0


def _install_strategy_stub() -> None:
    """Register a fake ``research.lab.strategy`` exposing ``Strategy``."""
    mod = _pytypes.ModuleType("research.lab.strategy")
    mod.Strategy = _StubStrategy  # type: ignore[attr-defined]
    sys.modules["research.lab.strategy"] = mod


def _controlled_panel(market: str, *, gap: float, n: int = 48,
                      stale_mid: bool = False) -> Panel:
    """Hand-built Panel with a KNOWN pace-vs-line gap and a fresh line.

    Sets the accumulator (total/margin) so the pace projection minus ``mid`` is
    exactly ``gap`` across the qualifying window, with a non-stale mid. Positive
    ``gap`` => pace runs high => continuation bets over / cover-home.
    """
    elapsed = np.linspace(600.0, 2400.0, n)
    minute_ts = 1_700_000_000.0 + (elapsed / 60.0).astype(int) * 60.0
    mid_level = 200.0 if market == TOTAL else 5.0
    # The line ticks by a negligible amount every minute (like a real, live
    # quote) so the freshness guard sees it as fresh; the gap is effectively
    # ``gap`` throughout. ``stale_mid=True`` opts into a flat (never-updating)
    # line to exercise the staleness guard.
    if stale_mid:
        mid = np.full(n, mid_level, dtype=float)
    else:
        mid = mid_level + 1e-3 * np.arange(n, dtype=float)
    # proj = accum * 2880 / elapsed  ==>  accum = (mid + gap) * elapsed / 2880
    target_proj = mid + gap
    accum = target_proj * elapsed / 2880.0
    total = accum if market == TOTAL else np.zeros(n)
    margin = accum if market == SPREAD else np.zeros(n)
    return Panel(
        game_id="ctl", date="2025-11-01", market=market,
        home_team="HOM", away_team="AWY", minute_ts=minute_ts,
        elapsed_sec=elapsed, margin=margin, total=total, mid=mid,
        final_total=float(total[-1]) if market == TOTAL else None,
        final_margin=float(margin[-1]) if market == SPREAD else None,
        split="train",
    )


class TestEntryMask(unittest.TestCase):
    def test_fires_on_pace_above_line(self) -> None:
        """A constructed pace>line gap above threshold fires the entry mask."""
        for market in (TOTAL, SPREAD):
            panel = _controlled_panel(market, gap=12.0)
            mask = anchoring._entry_mask(
                panel, thresh=6.0, min_elapsed=720.0, max_elapsed=2520.0,
                max_stale_min=2.0)
            self.assertTrue(mask.any(), f"{market}: expected the mask to fire")

    def test_silent_below_threshold(self) -> None:
        """A sub-threshold gap never fires."""
        panel = _controlled_panel(TOTAL, gap=1.0)
        mask = anchoring._entry_mask(
            panel, thresh=6.0, min_elapsed=720.0, max_elapsed=2520.0,
            max_stale_min=2.0)
        self.assertFalse(mask.any())

    def test_respects_elapsed_window(self) -> None:
        """Bars outside [min_elapsed, max_elapsed] are excluded even with signal."""
        panel = _controlled_panel(TOTAL, gap=12.0)
        mask = anchoring._entry_mask(
            panel, thresh=6.0, min_elapsed=720.0, max_elapsed=2520.0,
            max_stale_min=2.0)
        early = panel.elapsed_sec < 720.0
        self.assertFalse(mask[early].any(), "no fire before min_elapsed")

    def test_staleness_guard_blocks_stale_line(self) -> None:
        """A line that has not changed for longer than max_stale_min is rejected."""
        # stale_mid: the line is flat for the whole game, so every in-window bar
        # is older than the guard and nothing fires despite a 12pt signal.
        stale = _controlled_panel(TOTAL, gap=12.0, stale_mid=True)
        mask = anchoring._entry_mask(
            stale, thresh=6.0, min_elapsed=720.0, max_elapsed=2520.0,
            max_stale_min=2.0)
        self.assertFalse(mask.any(), "stale line should be guarded out")
        # the same game with a live-ticking line DOES fire.
        fresh = _controlled_panel(TOTAL, gap=12.0)
        fresh_mask = anchoring._entry_mask(
            fresh, thresh=6.0, min_elapsed=720.0, max_elapsed=2520.0,
            max_stale_min=2.0)
        self.assertTrue(fresh_mask.any(), "fresh line should fire")


class TestSideLabels(unittest.TestCase):
    def test_totals_continuation_side(self) -> None:
        """gap>0 -> over; gap<0 -> under (continuation)."""
        side = anchoring._make_side(
            continuation=True, over_label="over", under_label="under")
        hi = _controlled_panel(TOTAL, gap=12.0)
        lo = _controlled_panel(TOTAL, gap=-12.0)
        self.assertEqual(side(hi, hi.n - 1), "over")
        self.assertEqual(side(lo, lo.n - 1), "under")

    def test_totals_reversion_flips(self) -> None:
        side = anchoring._make_side(
            continuation=False, over_label="over", under_label="under")
        hi = _controlled_panel(TOTAL, gap=12.0)
        self.assertEqual(side(hi, hi.n - 1), "under")

    def test_spread_continuation_side(self) -> None:
        """gap>0 -> cover_home; gap<0 -> cover_away (continuation)."""
        side = anchoring._make_side(
            continuation=True, over_label="cover_home", under_label="cover_away")
        hi = _controlled_panel(SPREAD, gap=12.0)
        lo = _controlled_panel(SPREAD, gap=-12.0)
        self.assertEqual(side(hi, hi.n - 1), "cover_home")
        self.assertEqual(side(lo, lo.n - 1), "cover_away")


class TestFactories(unittest.TestCase):
    def setUp(self) -> None:
        _install_strategy_stub()

    def tearDown(self) -> None:
        sys.modules.pop("research.lab.strategy", None)

    def test_totals_strategy_builds_and_fires(self) -> None:
        strat = anchoring.totals_anchoring(thresh=6.0)
        self.assertEqual(strat.name, "totals_anchoring")
        self.assertEqual(strat.exit, "settlement")
        self.assertEqual(strat.entry_latency_min, 1.0)
        self.assertEqual(strat.max_stale_min, 2.0)
        panel = _controlled_panel(TOTAL, gap=12.0)
        mask = strat.entry(panel)
        self.assertTrue(mask.any())
        i = int(np.argmax(mask))
        self.assertEqual(strat.side(panel, i), "over")

    def test_spread_strategy_builds_and_fires(self) -> None:
        strat = anchoring.spread_anchoring(thresh=6.0)
        self.assertEqual(strat.name, "spread_anchoring")
        panel = _controlled_panel(SPREAD, gap=12.0)
        mask = strat.entry(panel)
        self.assertTrue(mask.any())
        i = int(np.argmax(mask))
        self.assertEqual(strat.side(panel, i), "cover_home")

    def test_reversion_variant_names(self) -> None:
        self.assertEqual(
            anchoring.totals_anchoring(continuation=False).name,
            "totals_anchoring_reversion")
        self.assertEqual(
            anchoring.spread_anchoring(continuation=False).name,
            "spread_anchoring_reversion")


class TestSignalsLazyStub(unittest.TestCase):
    """The factory must use lab.signals lazily when present (stubbed here)."""

    def tearDown(self) -> None:
        sys.modules.pop("research.lab.signals", None)

    def test_uses_stubbed_signals_anchoring_gap(self) -> None:
        sentinel = np.full(48, 99.0)
        mod = _pytypes.ModuleType("research.lab.signals")
        mod.anchoring_gap = lambda panel: sentinel            # type: ignore[attr-defined]
        mod.staleness_min = lambda panel: np.zeros(48)        # type: ignore[attr-defined]
        sys.modules["research.lab.signals"] = mod
        panel = synthetic_panel(market=TOTAL, n=48, seed=1)
        gap = anchoring._anchoring_gap(panel)
        np.testing.assert_array_equal(gap, sentinel)

    def test_falls_back_to_numpy_without_signals(self) -> None:
        sys.modules.pop("research.lab.signals", None)
        panel = synthetic_panel(market=SPREAD, n=48, seed=2)
        gap = anchoring._anchoring_gap(panel)
        np.testing.assert_allclose(gap, anchoring._anchoring_gap_numpy(panel))


class TestPackage(unittest.TestCase):
    def test_package_exports(self) -> None:
        self.assertIn("anchoring", strategies.__all__)


if __name__ == "__main__":
    unittest.main()
