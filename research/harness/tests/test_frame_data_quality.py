"""Unit tests for the WS1 frame/feature data-quality fixes.

These call ``_build_bars`` / ``_build_context`` directly with synthesised
in-memory ``game`` dicts (no lake on disk) so the suite is hermetic and pins
the per-bar frame contract introduced by the data-quality remediation:

  C1  kalshi_home_winprob is produced and oriented to the HOME side.
  C2  lead_changes_cum is a TRUE running total (not the per-minute count).
  C3  lineup_sig is a stable, real on-court signature that moves on a sub.
  C6  unrealized_pnl_bps is None (not a fabricated 0) when no quote exists.
"""
from __future__ import annotations

import unittest

import pandas as pd

from research.features import computers
from research.harness import replay

HOME, AWAY = "LAL", "GSW"


def _pbp(mt, el, lead_changes):
    n = len(mt)
    return pd.DataFrame(
        {
            "game_id": ["G"] * n,
            "date": ["2025-10-21"] * n,
            "home_tri": [HOME] * n,
            "away_tri": [AWAY] * n,
            "minute_ts": mt,
            "elapsed_game_sec": el,
            "home_score": [float(i) for i in range(n)],
            "away_score": [float(i) for i in range(n)],
            "margin": [0.0] * n,
            "pts_home": [0.0] * n,
            "pts_away": [0.0] * n,
            "total": [float(2 * i) for i in range(n)],
            "n_subs": [0] * n,
            "n_star_subs": [0] * n,
            "lead_changes": lead_changes,
            "home_stars_on": [0] * n,
            "away_stars_on": [0] * n,
        }
    )


def _pm(mt, mids=None):
    n = len(mt)
    mids = [0.5] * n if mids is None else mids
    return pd.DataFrame(
        {"game_id": ["G"] * n, "minute_ts": mt, "token_id": ["t"] * n,
         "team": ["home"] * n, "mid": mids}
    )


def _empty_kalshi():
    return pd.DataFrame(
        columns=["game_id", "minute_ts", "series", "ticker", "team",
                 "yes_bid", "yes_ask", "mid", "last"]
    )


def _empty_subs():
    return pd.DataFrame(
        columns=["game_id", "date", "ts", "team", "player_in", "player_out", "is_star"]
    )


def _meta(starters_home="A;B;C;D;E", starters_away="V;W;X;Y;Z",
          stars_home="A;B", stars_away="V;W"):
    return pd.Series(
        {"game_id": "G", "home_tri": HOME, "away_tri": AWAY,
         "starters_home": starters_home, "starters_away": starters_away,
         "stars_home": stars_home, "stars_away": stars_away, "home_won": True}
    )


def _game(pbp, pm, kalshi=None, subs=None, meta=None):
    return {
        "pbp": pbp,
        "polymarket": pm,
        "kalshi": _empty_kalshi() if kalshi is None else kalshi,
        "subs": _empty_subs() if subs is None else subs,
        "meta": _meta() if meta is None else meta,
    }


class TestLeadChangesCum(unittest.TestCase):
    """C2: the contractually-cumulative feature must report a running total."""

    def test_cumulative_and_feature(self):
        mt = [1000 + 60 * i for i in range(5)]
        el = [float(60 * i) for i in range(5)]
        bars = replay._build_bars(_game(_pbp(mt, el, [0, 1, 0, 2, 1]), _pm(mt)), "polymarket")
        cum = [b["lead_changes_cum"] for b in bars]
        self.assertEqual(cum, [0.0, 1.0, 1.0, 3.0, 4.0])
        self.assertTrue(all(cum[i] <= cum[i + 1] for i in range(len(cum) - 1)))
        # The feature reads the cumulative column, not the per-minute count.
        self.assertEqual(computers.compute_lead_changes_cum(bars[3], 0.0), 3.0)
        self.assertEqual(bars[3]["lead_changes"], 2)  # per-minute still raw


class TestKalshiHomeWinprob(unittest.TestCase):
    """C1: kalshi_home_winprob is produced and oriented to the home side."""

    def _run(self, team, mid):
        mt = [1000 + 60 * i for i in range(4)]
        el = [float(60 * i) for i in range(4)]
        k = pd.DataFrame(
            {"game_id": ["G"] * 4, "minute_ts": mt, "series": ["KXNBAGAME"] * 4,
             "ticker": [f"KXNBAGAME-X-{team}"] * 4, "team": [team] * 4,
             "yes_bid": [mid - 0.01] * 4, "yes_ask": [mid + 0.01] * 4,
             "mid": [mid] * 4, "last": [mid] * 4}
        )
        return replay._build_bars(_game(_pbp(mt, el, [0] * 4), _pm(mt), kalshi=k), "polymarket")

    def test_home_team_market(self):
        bars = self._run(HOME, 0.70)
        self.assertAlmostEqual(bars[-1]["kalshi_home_winprob"], 0.70, places=6)
        self.assertAlmostEqual(computers.compute_kalshi_implied_wp(bars[-1], 0.0), 0.70, places=6)

    def test_away_team_market_inverted(self):
        bars = self._run(AWAY, 0.70)
        self.assertAlmostEqual(bars[-1]["kalshi_home_winprob"], 0.30, places=6)

    def test_none_when_kalshi_absent(self):
        mt = [1000 + 60 * i for i in range(3)]
        el = [float(60 * i) for i in range(3)]
        bars = replay._build_bars(_game(_pbp(mt, el, [0] * 3), _pm(mt)), "polymarket")
        self.assertIsNone(bars[0]["kalshi_home_winprob"])
        self.assertIsNone(computers.compute_kalshi_implied_wp(bars[0], 0.0))


class TestLineupSig(unittest.TestCase):
    """C3: real on-court signature, moves on a sub, stable across runs."""

    def test_changes_on_sub_and_deterministic(self):
        mt = [1000 + 60 * i for i in range(6)]
        el = [float(60 * i) for i in range(6)]
        subs = pd.DataFrame(
            {"game_id": ["G"], "date": ["2025-10-21"], "ts": [mt[3] + 5],
             "team": [HOME], "player_in": ["NewGuy"], "player_out": ["A"], "is_star": [True]}
        )
        game = _game(_pbp(mt, el, [0] * 6), _pm(mt), subs=subs)
        bars = replay._build_bars(game, "polymarket")
        sig_before, sig_after = bars[0]["lineup_sig"], bars[5]["lineup_sig"]
        self.assertIsNotNone(sig_before)
        self.assertIsNotNone(sig_after)
        self.assertNotEqual(sig_before, sig_after)
        # Stable across a second identical build (no salted hash()).
        bars2 = replay._build_bars(game, "polymarket")
        self.assertEqual([b["lineup_sig"] for b in bars], [b["lineup_sig"] for b in bars2])
        self.assertEqual(computers.compute_lineup_hash(bars[0], 0.0), sig_before)

    def test_none_when_starters_missing(self):
        mt = [1000 + 60 * i for i in range(3)]
        el = [float(60 * i) for i in range(3)]
        game = _game(_pbp(mt, el, [0] * 3), _pm(mt),
                     meta=_meta(starters_home=None, starters_away=None))
        bars = replay._build_bars(game, "polymarket")
        self.assertIsNone(bars[0]["lineup_sig"])
        self.assertIsNone(computers.compute_lineup_hash(bars[0], 0.0))


class TestUnrealizedPnlNone(unittest.TestCase):
    """C6: no fabricated 0 PnL on a data-gap bar."""

    def test_none_on_missing_quote(self):
        bar = {"elapsed_game_sec": 600.0, "margin": -5.0, "total": 100.0,
               "home_score": 50.0, "away_score": 55.0,
               "pm_home_mid": None, "pm_away_mid": None}
        pos = {"side": "long_home", "entry_price": 0.5, "entry_game_sec": 300.0, "size": 5.0}
        ctx = replay._build_context(bar, pos, {}, venue="polymarket")
        self.assertIsNone(ctx["unrealized_pnl_bps"])

    def test_computed_when_quote_present(self):
        bar = {"elapsed_game_sec": 600.0, "margin": -5.0, "total": 100.0,
               "home_score": 50.0, "away_score": 55.0,
               "pm_home_mid": 0.6, "pm_away_mid": 0.4}
        pos = {"side": "long_home", "entry_price": 0.5, "entry_game_sec": 300.0, "size": 5.0}
        ctx = replay._build_context(bar, pos, {}, venue="polymarket")
        self.assertAlmostEqual(ctx["unrealized_pnl_bps"], 2000.0, places=3)


if __name__ == "__main__":
    unittest.main()
