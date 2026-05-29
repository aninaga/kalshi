"""Hermetic no-forward-leak canary (WS6).

A feature value at bar i must reflect only data knowable at i — never a future
bar's value. This pins the property directly on the frame: with strictly
increasing per-minute mids, pm_implied_wp at bar i must equal mid[i] (the value
quotable at i) and never mid[i+1]. The full data-quality gate
(research/scripts/check_data_quality.py) runs the same canary on the real lake.
"""
from __future__ import annotations

import unittest

import pandas as pd

from research.features import computers
from research.harness import replay


class TestNoForwardLeak(unittest.TestCase):
    def test_pm_implied_wp_uses_current_not_future_mid(self):
        n = 8
        mt = [1000 + 60 * i for i in range(n)]
        mids = [0.40 + 0.03 * i for i in range(n)]  # strictly increasing, distinct
        pbp = pd.DataFrame({
            "game_id": ["C"] * n, "date": ["2025-10-21"] * n, "home_tri": ["LAL"] * n,
            "away_tri": ["GSW"] * n, "minute_ts": mt,
            "elapsed_game_sec": [float(60 * i) for i in range(n)],
            "home_score": [float(i) for i in range(n)], "away_score": [0.0] * n,
            "margin": [float(i) for i in range(n)], "pts_home": [0.0] * n,
            "pts_away": [0.0] * n, "total": [float(i) for i in range(n)],
            "n_subs": [0] * n, "n_star_subs": [0] * n, "lead_changes": [0] * n,
            "home_stars_on": [0] * n, "away_stars_on": [0] * n,
        })
        pm = pd.DataFrame({"game_id": ["C"] * n, "minute_ts": mt, "token_id": ["t"] * n,
                           "team": ["home"] * n, "mid": mids})
        game = {
            "pbp": pbp, "polymarket": pm,
            "kalshi": pd.DataFrame(columns=["game_id", "minute_ts", "series", "ticker",
                                            "team", "yes_bid", "yes_ask", "mid", "last"]),
            "subs": pd.DataFrame(columns=["game_id", "date", "ts", "team",
                                          "player_in", "player_out", "is_star"]),
            "meta": pd.Series({"home_tri": "LAL", "away_tri": "GSW",
                               "starters_home": None, "starters_away": None}),
        }
        bars = replay._build_bars(game, "polymarket")
        self.assertEqual(len(bars), n)
        for i, b in enumerate(bars):
            wp = computers.compute_pm_implied_wp(b, 0.0)
            self.assertIsNotNone(wp)
            self.assertAlmostEqual(wp, mids[i], places=9,
                                   msg=f"bar {i}: pm_implied_wp must be mid[i], no forward leak")
            if i + 1 < n:
                self.assertNotAlmostEqual(wp, mids[i + 1], places=6)


if __name__ == "__main__":
    unittest.main()
