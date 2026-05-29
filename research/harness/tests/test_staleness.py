"""Tests for the WS4 staleness bound + quote-age transparency.

A quote older than STALENESS_BOUND_SEC must be dropped to NaN (not presented as
the current price), and every bar must expose the forward-fill age so audits
and strategies can see freshness.
"""
from __future__ import annotations

import unittest

import pandas as pd

from research.harness import replay
from research.harness.replay import STALENESS_BOUND_SEC


def _pbp(mt):
    n = len(mt)
    return pd.DataFrame({
        "game_id": ["G"] * n, "date": ["2025-10-21"] * n,
        "home_tri": ["LAL"] * n, "away_tri": ["GSW"] * n,
        "minute_ts": mt, "elapsed_game_sec": [float(60 * i) for i in range(n)],
        "home_score": [float(i) for i in range(n)], "away_score": [float(i) for i in range(n)],
        "margin": [0.0] * n, "pts_home": [0.0] * n, "pts_away": [0.0] * n, "total": [0.0] * n,
        "n_subs": [0] * n, "n_star_subs": [0] * n, "lead_changes": [0] * n,
        "home_stars_on": [0] * n, "away_stars_on": [0] * n,
    })


def _empty(cols):
    return pd.DataFrame(columns=cols)


class TestStalenessBound(unittest.TestCase):
    def test_stale_pm_quote_dropped_fresh_kept(self):
        self.assertEqual(STALENESS_BOUND_SEC, 300)
        mt = [1000 + 60 * i for i in range(10)]  # 1000 .. 1540
        # PM ticks only at 1000 and 1480 -> a long mid-game gap.
        pm = pd.DataFrame({
            "game_id": ["G", "G"], "minute_ts": [1000, 1480],
            "token_id": ["t", "t"], "team": ["home", "home"], "mid": [0.50, 0.60],
        })
        meta = pd.Series({"home_tri": "LAL", "away_tri": "GSW",
                          "starters_home": None, "starters_away": None})
        game = {
            "pbp": _pbp(mt), "polymarket": pm,
            "kalshi": _empty(["game_id", "minute_ts", "series", "ticker", "team",
                              "yes_bid", "yes_ask", "mid", "last"]),
            "subs": _empty(["game_id", "date", "ts", "team", "player_in", "player_out", "is_star"]),
            "meta": meta,
        }
        by_mt = {int(b["minute_ts"]): b for b in replay._build_bars(game, "polymarket")}
        # Fresh at the tick minute.
        self.assertEqual(by_mt[1000]["pm_quote_age_sec"], 0)
        self.assertAlmostEqual(by_mt[1000]["pm_home_mid"], 0.50, places=6)
        # 240s old (<= bound): kept, age reported.
        self.assertEqual(by_mt[1240]["pm_quote_age_sec"], 240)
        self.assertAlmostEqual(by_mt[1240]["pm_home_mid"], 0.50, places=6)
        # 420s old (> bound): dropped to None, age None — not masquerading.
        self.assertIsNone(by_mt[1420]["pm_home_mid"])
        self.assertIsNone(by_mt[1420]["pm_quote_age_sec"])
        # Fresh again once a new tick arrives.
        self.assertEqual(by_mt[1480]["pm_quote_age_sec"], 0)
        self.assertAlmostEqual(by_mt[1480]["pm_home_mid"], 0.60, places=6)


if __name__ == "__main__":
    unittest.main()
