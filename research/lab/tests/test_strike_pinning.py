"""Tests for strike pinning — the snap-flip fix (bake-off action item 1).

The hazard: between the signal bar and the i+1 fill, the mid can cross a
bucket midpoint, the FillModel re-snap picks a DIFFERENT strike, and a
band-conditioned side label executes the opposite exposure. The fix: a
strategy may pin the strike its signal evaluated; the fill model honors it
exactly (or raises on an unlisted strike — never fabricates a quote).
"""
from __future__ import annotations

import unittest

import numpy as np

from research.lab.execution import FillModel
from research.lab.strategy import Strategy
from research.lab.types import TOTAL, Panel


def _panel(n=10):
    """mid starts at 219, jumps to 226 at bar 5 — the snap flips 220 -> 225."""
    ts = 1_700_000_000.0 + np.arange(n) * 60.0
    mid = np.where(np.arange(n) < 5, 219.0, 226.0).astype(float)
    ladder = {
        220.0: np.full(n, 0.90),   # deep ITM "over 220"
        225.0: np.full(n, 0.30),   # the strike the re-snap flips to
    }
    return Panel(
        game_id="snap", date="2026-01-01", market=TOTAL,
        home_team="HOM", away_team="AWY",
        minute_ts=ts, elapsed_sec=np.linspace(0, 2820, n),
        margin=np.zeros(n), total=np.linspace(0, 220, n),
        mid=mid, ladder=ladder, features={},
        home_won=1.0, final_total=230.0, final_margin=1.0, split="train",
    )


class TestFillPinning(unittest.TestCase):
    def test_unpinned_resnaps_at_fill_time(self) -> None:
        p = _panel()
        fm = FillModel(half_spread=0.0, venue="kalshi")
        # Signal at bar 4 (mid 219 -> nearest 220), fill at bar 5 (mid 226).
        fill = fm.fill(p, float(p.minute_ts[5]), "over")
        self.assertEqual(fill.strike, 225.0)        # the flip, demonstrated
        self.assertAlmostEqual(fill.fill_mid, 0.30)

    def test_pinned_strike_is_honored(self) -> None:
        p = _panel()
        fm = FillModel(half_spread=0.0, venue="kalshi")
        fill = fm.fill(p, float(p.minute_ts[5]), "over", strike=220.0)
        self.assertEqual(fill.strike, 220.0)
        self.assertAlmostEqual(fill.fill_mid, 0.90)  # the REAL 220 quote

    def test_pinning_unlisted_strike_raises(self) -> None:
        p = _panel()
        fm = FillModel(half_spread=0.0, venue="kalshi")
        with self.assertRaises(ValueError):
            fm.fill(p, float(p.minute_ts[5]), "over", strike=222.5)

    def test_pin_none_falls_back_to_snap(self) -> None:
        p = _panel()
        fm = FillModel(half_spread=0.0, venue="kalshi")
        fill = fm.fill(p, float(p.minute_ts[5]), "over", strike=None)
        self.assertEqual(fill.strike, 225.0)


class TestStrategyPinning(unittest.TestCase):
    def _strategy(self, pick=None):
        def entry(panel):
            mask = np.zeros(panel.n, dtype=bool)
            mask[4] = True                          # signal just before the jump
            return mask

        return Strategy(name="t", entry=entry, side=lambda p, i: "over",
                        min_elapsed=0.0, max_elapsed=1e9, max_stale_min=1e9,
                        pick_strike=pick)

    def test_strategy_pins_signal_bar_strike(self) -> None:
        p = _panel()
        fm = FillModel(half_spread=0.0, venue="kalshi")

        def pick(panel, bar):
            # The strike the signal evaluated: nearest to mid at the SIGNAL bar.
            strikes = sorted(panel.ladder)
            return min(strikes, key=lambda s: abs(s - panel.mid[bar]))

        trades = self._strategy(pick).run([p], fill_model=fm)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades.rows[0].entry_strike, 220.0)   # pinned
        # Settlement: final 230 > 220 -> payoff 1; entry at the real 0.90.
        self.assertAlmostEqual(trades.rows[0].entry_price, 0.90)

    def test_default_strategy_behavior_unchanged(self) -> None:
        p = _panel()
        fm = FillModel(half_spread=0.0, venue="kalshi")
        trades = self._strategy(None).run([p], fill_model=fm)
        self.assertEqual(trades.rows[0].entry_strike, 225.0)   # legacy re-snap

    def test_ducktyped_fill_model_without_kwarg_still_works(self) -> None:
        # Older fakes accept (panel, ts, side) only; unpinned strategies must
        # never pass the kwarg.
        class Fake:
            def fill(self, panel, ts, side):
                from research.lab.types import FillResult
                return FillResult(strike=220.0, fill_mid=0.5, half_spread=0.0, fee=0.0)

        trades = self._strategy(None).run([_panel()], fill_model=Fake())
        self.assertEqual(len(trades), 1)


if __name__ == "__main__":
    unittest.main()
