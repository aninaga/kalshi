"""Tests for the WS3 calibrated fill-depth fix (C7).

The synthesized book must use the active profile's depth (calibrated from real
executed trades) rather than a fabricated constant, so the price-impact model
bites and a large order partially fills against a realistically thin book.
"""
from __future__ import annotations

import unittest

from research.harness import replay
from research.harness.cost_profile import CALIBRATED_PM, PESSIMISTIC, use_profile
from research.harness.realistic_fills import simulate_fill


class TestCalibratedDepth(unittest.TestCase):
    def test_profile_depth_is_calibrated_and_realistic(self):
        # Calibrated depth is far below the legacy fabricated 1000.
        self.assertGreater(CALIBRATED_PM.depth_per_level, 0.0)
        self.assertLess(CALIBRATED_PM.depth_per_level, 1000.0)
        self.assertEqual(PESSIMISTIC.depth_per_level, 1000.0)

    def test_synth_book_reads_profile_depth(self):
        with use_profile(CALIBRATED_PM):
            ob = replay.synthesize_orderbook_from_mid("polymarket", "yes", 0.5, 0, 100.0)
        self.assertAlmostEqual(ob.asks[0][1], CALIBRATED_PM.depth_per_level, places=3)
        with use_profile(PESSIMISTIC):
            ob2 = replay.synthesize_orderbook_from_mid("polymarket", "yes", 0.5, 0, 100.0)
        self.assertEqual(ob2.asks[0][1], 1000.0)

    def test_impact_bites_and_large_order_partially_fills(self):
        size = 200.0  # >> calibrated 3-level depth, << fabricated 3000
        with use_profile(PESSIMISTIC):
            ob_fab = replay.synthesize_orderbook_from_mid("polymarket", "yes", 0.5, 0, 100.0)
            f_fab = simulate_fill(ob_fab, "buy", size, stale_threshold_sec=float("inf"), now_wall=0.0)
        with use_profile(CALIBRATED_PM):
            ob_real = replay.synthesize_orderbook_from_mid("polymarket", "yes", 0.5, 0, 100.0)
            f_real = simulate_fill(ob_real, "buy", size, stale_threshold_sec=float("inf"), now_wall=0.0)
        # Fabricated deep book swallows the whole order with tiny slippage;
        # the real thin book only partially fills it and slips much more.
        self.assertEqual(f_fab.filled_size, size)
        self.assertLess(f_real.filled_size, size)
        self.assertGreater(f_real.slippage_bps, f_fab.slippage_bps)


if __name__ == "__main__":
    unittest.main()
