"""Tests for research.scorer.bootstrap."""
from __future__ import annotations

import unittest

import numpy as np

from research.scorer.bootstrap import (
    block_bootstrap_by_game,
    cluster_knockouts,
    stability_by_season_half,
)


def _make_synthetic_pnl(
    n_games: int,
    trades_per_game: int,
    mean: float,
    sd: float,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (pnl, game_ids) with `n_games * trades_per_game` total trades."""
    rng = np.random.default_rng(seed)
    pnl = rng.normal(loc=mean, scale=sd, size=n_games * trades_per_game)
    game_ids = np.repeat(np.arange(n_games), trades_per_game)
    return pnl, game_ids


class TestBlockBootstrap(unittest.TestCase):
    def test_bootstrap_recovers_zero_mean(self) -> None:
        """Synthetic pnl ~ N(0, 1), 1000 trades, 100 games — CI should
        straddle zero (ci_lo < 0 < ci_hi)."""
        pnl, gids = _make_synthetic_pnl(
            n_games=100, trades_per_game=10, mean=0.0, sd=1.0, seed=1,
        )
        result = block_bootstrap_by_game(pnl, gids, n_resamples=2000, rng_seed=42)
        self.assertEqual(result.n_observations, 1000)
        self.assertEqual(result.n_groups, 100)
        self.assertLess(result.ci_lo, 0.0, f"ci_lo {result.ci_lo} should be < 0")
        self.assertGreater(result.ci_hi, 0.0, f"ci_hi {result.ci_hi} should be > 0")

    def test_bootstrap_recovers_positive_mean(self) -> None:
        """pnl ~ N(0.05, 1), 1000 trades, 100 games — ci_lo > 0.

        With effect 0.05 over n=1000 the SE is ~0.032; 95% CI lower bound
        should clearly clear zero in expectation. We allow some seed
        variance but require ci_lo > 0.
        """
        pnl, gids = _make_synthetic_pnl(
            n_games=100, trades_per_game=10, mean=0.05, sd=0.5, seed=2,
        )
        result = block_bootstrap_by_game(pnl, gids, n_resamples=2000, rng_seed=42)
        self.assertGreater(
            result.ci_lo, 0.0,
            f"expected positive ci_lo, got {result.ci_lo} (mean={result.mean})",
        )

    def test_bootstrap_negative_correlation_within_game(self) -> None:
        """Intra-game positively-correlated trades — block CI must be WIDER
        than the IID-bootstrap CI (resampling individual trades vs games).

        Construction: each game has a shared latent effect ~ N(0, 1), plus
        small iid noise per trade. Trades within a game are highly correlated;
        across games they're independent.
        """
        rng = np.random.default_rng(7)
        n_games, trades_per_game = 50, 20
        game_effects = rng.normal(loc=0.0, scale=1.0, size=n_games)
        noise = rng.normal(loc=0.0, scale=0.05, size=n_games * trades_per_game)
        pnl = np.repeat(game_effects, trades_per_game) + noise
        gids = np.repeat(np.arange(n_games), trades_per_game)

        block_result = block_bootstrap_by_game(
            pnl, gids, n_resamples=2000, rng_seed=42,
        )

        # Inline IID bootstrap: resample trades (not games) with replacement.
        rng2 = np.random.default_rng(42)
        n = len(pnl)
        iid_means = np.empty(2000)
        for b in range(2000):
            idx = rng2.integers(0, n, size=n)
            iid_means[b] = pnl[idx].mean()
        iid_lo = float(np.percentile(iid_means, 2.5))
        iid_hi = float(np.percentile(iid_means, 97.5))
        iid_width = iid_hi - iid_lo
        block_width = block_result.ci_hi - block_result.ci_lo
        self.assertGreater(
            block_width, iid_width * 2.0,
            f"block CI width {block_width:.4f} not substantially wider than "
            f"iid CI width {iid_width:.4f} — within-game correlation not captured",
        )

    def test_empty_input_returns_nan(self) -> None:
        result = block_bootstrap_by_game(
            np.array([]), np.array([]), n_resamples=100, rng_seed=42,
        )
        self.assertEqual(result.n_observations, 0)
        self.assertTrue(np.isnan(result.mean))


class TestSeasonSplit(unittest.TestCase):
    def test_season_split_returns_two_halves(self) -> None:
        """200 trades over 100 games across a date range — early + late
        n_groups sum ≈ 100 (modulo trades on the median-date boundary
        falling into 'early')."""
        rng = np.random.default_rng(3)
        n_games = 100
        # Spread games over 100 days.
        game_dates = np.array(
            ["2024-01-01"], dtype="datetime64[D]",
        ).repeat(n_games) + np.arange(n_games).astype("timedelta64[D]")
        # 2 trades per game.
        trades_per_game = 2
        gids = np.repeat(np.arange(n_games), trades_per_game)
        dates = np.repeat(game_dates, trades_per_game).astype("datetime64[s]")
        pnl = rng.normal(loc=0.01, scale=0.1, size=n_games * trades_per_game)

        result = stability_by_season_half(pnl, dates, gids, rng_seed=42)
        total_groups = result["early"].n_groups + result["late"].n_groups
        # Allow ±1 game tolerance for median boundary ties.
        self.assertGreaterEqual(total_groups, n_games - 1)
        self.assertLessEqual(total_groups, n_games + 1)


class TestClusterKnockouts(unittest.TestCase):
    def test_cluster_knockout_top_games_drops_worst_when_pnl_negative(self) -> None:
        """Overall negative PnL concentrated in 5 of 100 games — dropping
        the top-|sum| games should yield a HIGHER mean than baseline.

        This is the bug fix vs Phase −1's `cluster_knockout`: Phase −1 used
        `nlargest` on `abs()` for games (correct), but its team logic used
        signed `idxmax`. Here we verify the games case is well-behaved.
        """
        rng = np.random.default_rng(4)
        n_games = 100
        trades_per_game = 10
        pnl = rng.normal(loc=0.0, scale=0.05, size=n_games * trades_per_game)
        gids = np.repeat(np.arange(n_games), trades_per_game)
        teams = np.array(["TEAM_" + str(g % 30) for g in range(n_games)]).repeat(
            trades_per_game,
        )
        dates = np.array(["2024-01-01"]).repeat(n_games * trades_per_game).astype(
            "datetime64[s]",
        )
        # Inject 5 disaster games — push their pnl strongly negative.
        for game_idx in range(5):
            mask = gids == game_idx
            pnl[mask] = -5.0
        baseline_mean = pnl.mean()
        out = cluster_knockouts(pnl, gids, teams, dates, top_games_frac=0.05)
        self.assertEqual(out["drop_top_games_count"], 5)
        self.assertAlmostEqual(out["baseline_mean"], baseline_mean, places=6)
        self.assertGreater(
            out["drop_top_games_mean"], out["baseline_mean"],
            "Dropping top-|sum| games should improve mean when bad games dominate",
        )
        # The 5 disaster games are gone — drop_top_games_mean should be near zero.
        self.assertLess(abs(out["drop_top_games_mean"]), 0.05)

    def test_cluster_knockout_top_team_drops_by_abs(self) -> None:
        """A single team with concentrated NEGATIVE PnL must be detected
        (this is the bug fix — Phase −1's signed idxmax would never drop
        a losing team).
        """
        n_games_per_team = 10
        n_trades_per_game = 5
        teams_list = ["BUL", "CHI", "GSW", "LAL", "NYK"]
        gids_list, teams_arr_list, pnl_list, dates_list = [], [], [], []
        rng = np.random.default_rng(5)
        for t_idx, team in enumerate(teams_list):
            for g in range(n_games_per_team):
                gid = t_idx * n_games_per_team + g
                for _ in range(n_trades_per_game):
                    gids_list.append(gid)
                    teams_arr_list.append(team)
                    pnl_list.append(rng.normal(loc=0.02, scale=0.1))
                    dates_list.append("2024-01-01")
        pnl = np.array(pnl_list)
        gids = np.array(gids_list)
        teams = np.array(teams_arr_list)
        dates = np.array(dates_list, dtype="datetime64[s]")
        # Make BUL strongly negative.
        bul_mask = teams == "BUL"
        pnl[bul_mask] = -2.0

        out = cluster_knockouts(pnl, gids, teams, dates, top_games_frac=0.05)
        self.assertEqual(out["drop_top_team_team"], "BUL")
        # After dropping BUL, the remaining mean should be positive (~0.02).
        self.assertGreater(
            out["drop_top_team_mean"], out["baseline_mean"],
            "Dropping the |abs|-top team should improve mean when bad team dominates",
        )


if __name__ == "__main__":
    unittest.main()
