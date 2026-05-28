"""Tests for research.scorer.promotion_gate.evaluate_trial."""
from __future__ import annotations

import unittest

import numpy as np

from research.scorer.promotion_gate import evaluate_trial


def _build_synthetic_val(
    n_games: int,
    trades_per_game: int,
    mean_pnl: float,
    sd_pnl: float,
    date_start: str = "2024-01-01",
    n_teams: int = 30,
    seed: int = 0,
) -> dict:
    """Generate a synthetic validation batch suitable for evaluate_trial.

    Returns kwargs ready to splat into `evaluate_trial(**)`.
    Teams are rotated round-robin to avoid concentration.
    """
    rng = np.random.default_rng(seed)
    n_trades = n_games * trades_per_game

    pnl = rng.normal(loc=mean_pnl, scale=sd_pnl, size=n_trades)
    gids = np.repeat(np.arange(n_games), trades_per_game)
    # Spread games over enough days that the median date is well-defined.
    days = np.arange(n_games)
    game_dates = (np.datetime64(date_start, "D") + days.astype("timedelta64[D]")).astype("datetime64[s]")
    dates = np.repeat(game_dates, trades_per_game)
    # Round-robin teams so that each game has a distinct home_team rotation.
    home_teams = np.repeat(
        np.array([f"TEAM_{g % n_teams}" for g in range(n_games)]),
        trades_per_game,
    )
    # Primary team alternates between home and away to avoid trivial parity.
    primary_teams = np.repeat(
        np.array([f"TEAM_{(g + 1) % n_teams}" for g in range(n_games)]),
        trades_per_game,
    )
    return dict(
        val_pnl_per_trade=pnl,
        val_game_id_per_trade=gids,
        val_date_per_trade=dates,
        val_home_team_per_trade=home_teams,
        val_primary_team_per_trade=primary_teams,
        n_total_trials_in_registry=1,
        sharpe_variance_in_registry=1.0,
        rng_seed=42,
    )


class TestPromotionGate(unittest.TestCase):
    def test_synthetic_clean_pass(self) -> None:
        """Clean positive edge, stable, diversified — must pass all gates."""
        kwargs = _build_synthetic_val(
            n_games=400,
            trades_per_game=4,
            mean_pnl=0.04,
            sd_pnl=0.10,
            n_teams=30,
            seed=10,
        )
        decision = evaluate_trial(**kwargs)
        self.assertTrue(
            decision.passed,
            f"Expected pass; got reasons: {decision.reasons}",
        )
        self.assertGreater(decision.block_bootstrap_ci_lo, 0.0)
        self.assertGreaterEqual(decision.n_trades_val, 200)

    def test_overfit_canary_rejected_by_block_bootstrap(self) -> None:
        """Mean PnL is positive only due to 2-3 outlier games — block
        bootstrap CI lower bound is negative, gate must reject and the
        reason string must mention block_bootstrap.
        """
        rng = np.random.default_rng(99)
        n_games = 250
        trades_per_game = 4
        # Baseline: tiny noise around zero.
        pnl = rng.normal(loc=0.0, scale=0.01, size=n_games * trades_per_game)
        gids = np.repeat(np.arange(n_games), trades_per_game)
        # Inject 3 huge winners — drive mean positive but variance up.
        for huge_game in [3, 17, 42]:
            mask = gids == huge_game
            pnl[mask] = 5.0
        days = np.arange(n_games)
        game_dates = (
            np.datetime64("2024-01-01", "D") + days.astype("timedelta64[D]")
        ).astype("datetime64[s]")
        dates = np.repeat(game_dates, trades_per_game)
        home_teams = np.repeat(
            np.array([f"TEAM_{g % 30}" for g in range(n_games)]),
            trades_per_game,
        )
        primary_teams = np.repeat(
            np.array([f"TEAM_{(g + 1) % 30}" for g in range(n_games)]),
            trades_per_game,
        )

        decision = evaluate_trial(
            val_pnl_per_trade=pnl,
            val_game_id_per_trade=gids,
            val_date_per_trade=dates,
            val_home_team_per_trade=home_teams,
            val_primary_team_per_trade=primary_teams,
            n_total_trials_in_registry=1,
            sharpe_variance_in_registry=1.0,
            rng_seed=42,
        )
        self.assertFalse(decision.passed)
        # At least one reason must reference the block bootstrap or
        # concentration check (the two gates designed to catch this case).
        relevant = [
            r for r in decision.reasons
            if "block_bootstrap_ci_lo" in r or "single_game_share" in r
        ]
        self.assertTrue(
            relevant,
            f"Expected block_bootstrap_ci_lo or single_game_share in reasons; got {decision.reasons}",
        )

    def test_thin_data_rejected_by_min_n_trades(self) -> None:
        """100 trades — fails on n_trades gate (default min 200)."""
        kwargs = _build_synthetic_val(
            n_games=50,
            trades_per_game=2,
            mean_pnl=0.05,
            sd_pnl=0.05,
            n_teams=30,
            seed=11,
        )
        decision = evaluate_trial(**kwargs)
        self.assertFalse(decision.passed)
        relevant = [r for r in decision.reasons if "n_trades" in r]
        self.assertTrue(relevant, f"Expected n_trades in reasons; got {decision.reasons}")

    def test_season_dependent_rejected(self) -> None:
        """Positive in early half, zero in late half — fails season_split."""
        rng = np.random.default_rng(13)
        n_games = 300
        trades_per_game = 4
        gids = np.repeat(np.arange(n_games), trades_per_game)
        # Early half (first 150 games): strong positive edge.
        # Late half: pure noise around zero.
        pnl = np.zeros(n_games * trades_per_game)
        early_game_mask = gids < 150
        late_game_mask = ~early_game_mask
        pnl[early_game_mask] = rng.normal(
            loc=0.10, scale=0.10, size=int(early_game_mask.sum()),
        )
        pnl[late_game_mask] = rng.normal(
            loc=0.0, scale=0.10, size=int(late_game_mask.sum()),
        )
        days = np.arange(n_games)
        game_dates = (
            np.datetime64("2024-01-01", "D") + days.astype("timedelta64[D]")
        ).astype("datetime64[s]")
        dates = np.repeat(game_dates, trades_per_game)
        home_teams = np.repeat(
            np.array([f"TEAM_{g % 30}" for g in range(n_games)]),
            trades_per_game,
        )
        primary_teams = np.repeat(
            np.array([f"TEAM_{(g + 1) % 30}" for g in range(n_games)]),
            trades_per_game,
        )

        decision = evaluate_trial(
            val_pnl_per_trade=pnl,
            val_game_id_per_trade=gids,
            val_date_per_trade=dates,
            val_home_team_per_trade=home_teams,
            val_primary_team_per_trade=primary_teams,
            n_total_trials_in_registry=1,
            sharpe_variance_in_registry=1.0,
            rng_seed=42,
        )
        self.assertFalse(decision.passed)
        relevant = [r for r in decision.reasons if "season_split" in r]
        self.assertTrue(
            relevant,
            f"Expected season_split in reasons; got {decision.reasons}",
        )


if __name__ == "__main__":
    unittest.main()
