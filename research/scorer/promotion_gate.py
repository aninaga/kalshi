"""Combined promotion gate for trial-grade strategies.

Composes:
  - block_bootstrap_by_game        (decisive)
  - cluster_knockouts              (decisive)
  - stability_by_season_half       (decisive)
  - stability_by_home_team_parity  (decisive)
  - concentration check            (decisive)
  - deflated_sharpe                (informational lower-bound only)

ALL gates are evaluated even if earlier ones fail, so `reasons` aggregates
every failure. This makes it easier for the orchestrator's Claude reviewer
subagent to see the full picture of why a trial was rejected.

Block-bootstrap-by-game is the decisive criterion. DSR is computed but
intentionally given lenient thresholds (DSR > 0.5, p < 0.20) — the real
multiple-testing protection comes from the registry's separation of test/val
splits plus the stability checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from research.scorer.bootstrap import (
    BootstrapResult,
    block_bootstrap_by_game,
    cluster_knockouts,
    stability_by_home_team_parity,
    stability_by_season_half,
)
from research.scorer.dsr import deflated_sharpe


# Promotion gate constants. Tweaking these is a policy decision; document any
# change in the plan file before changing here.
_MIN_N_TRADES_DEFAULT = 200
_DSR_FLOOR = 0.5
_DSR_PVAL_CEIL = 0.20
_SINGLE_GAME_CONCENTRATION_MAX = 0.20
_SINGLE_TEAM_CONCENTRATION_MAX = 0.25


@dataclass(frozen=True)
class PromotionDecision:
    """Output of `evaluate_trial`.

    `reasons` is the empty list iff `passed` is True. Each failing gate
    appends a human-readable string to it. The orchestrator surfaces this
    list to the Claude reviewer subagent.
    """
    passed: bool
    reasons: list[str]
    block_bootstrap_ci_lo: float
    block_bootstrap_ci_hi: float
    block_bootstrap_mean: float
    dsr: float
    dsr_p_value: float
    cluster_knockouts: dict
    season_split: dict
    parity_split: dict
    n_trades_val: int
    n_games_val: int


def _concentration_share(
    pnl: np.ndarray,
    group: np.ndarray,
) -> tuple[float, str]:
    """Return (max single-group share of total |pnl|, that group's label)."""
    if len(pnl) == 0:
        return 0.0, ""
    total_abs = float(np.abs(pnl).sum())
    if total_abs <= 0:
        return 0.0, ""
    unique, inv = np.unique(group, return_inverse=True)
    abs_sums = np.zeros(len(unique), dtype=float)
    np.add.at(abs_sums, inv, np.abs(pnl))
    top_idx = int(np.argmax(abs_sums))
    return float(abs_sums[top_idx] / total_abs), str(unique[top_idx])


def _sharpe(pnl: np.ndarray) -> float:
    arr = np.asarray(pnl, dtype=float)
    if len(arr) < 2:
        return float("nan")
    std = arr.std(ddof=1)
    if std <= 0 or not np.isfinite(std):
        return float("nan")
    return float(arr.mean() / std)


def evaluate_trial(
    val_pnl_per_trade: np.ndarray,
    val_game_id_per_trade: np.ndarray,
    val_date_per_trade: np.ndarray,
    val_home_team_per_trade: np.ndarray,
    val_primary_team_per_trade: np.ndarray,
    n_total_trials_in_registry: int,
    sharpe_variance_in_registry: float = 1.0,
    cost_per_trade_assumed: float = 0.0,
    min_n_trades: int = _MIN_N_TRADES_DEFAULT,
    rng_seed: int = 42,
) -> PromotionDecision:
    """Apply ALL promotion gates; `passed=True` iff every gate passes.

    Parameters
    ----------
    val_pnl_per_trade : np.ndarray (n_trades,)
        Per-trade PnL on the validation set, ALREADY NET OF ALL COSTS. The
        scorer does not subtract `cost_per_trade_assumed` — that arg is
        retained for documentation only (so audit logs can record what cost
        model the harness used to compute these values).
    val_game_id_per_trade : np.ndarray (n_trades,)
        game_id for each trade — the bootstrap block.
    val_date_per_trade : np.ndarray (n_trades,)
        ISO date string or datetime64. Used for season-half split.
    val_home_team_per_trade : np.ndarray (n_trades,)
        Home team (regardless of which side this trade went long). Used for
        the parity-split stability check.
    val_primary_team_per_trade : np.ndarray (n_trades,)
        The team this trade went long. Used for team-concentration and
        cluster-knockout's drop-top-team.
    n_total_trials_in_registry : int
        Total trials across the registry — DSR's multiple-testing N.
    sharpe_variance_in_registry : float
        Variance of Sharpe ratios across the registry. 1.0 is a placeholder;
        the orchestrator should pass the actual cross-trial Sharpe variance
        computed from `registry.api.query()`. With sharpe_variance=1.0 and a
        large n_trials the expected_max_sharpe inflates aggressively, so this
        is the conservative default.
    cost_per_trade_assumed : float
        Documentation only — pnl is already net.
    min_n_trades : int
        Trade-count floor. Default 200 matches Phase −1.
    rng_seed : int

    Gates (failure adds a string to `reasons`)
    ------------------------------------------
      1. block_bootstrap_ci_lo > 0
      2. n_trades >= min_n_trades
      3. ALL THREE cluster knockouts have mean > 0
      4. season_split: both halves' 95% CI lo > 0 AND 80% CIs overlap
      5. parity_split: same
      6. DSR > 0.5 AND p_value < 0.20 (lenient — informational)
      7. Concentration: no single game > 20% of |pnl|; no team > 25%

    All gates evaluated regardless of earlier failures.
    """
    pnl = np.asarray(val_pnl_per_trade, dtype=float)
    games = np.asarray(val_game_id_per_trade)
    dates = np.asarray(val_date_per_trade)
    home_teams = np.asarray(val_home_team_per_trade)
    primary_teams = np.asarray(val_primary_team_per_trade)

    reasons: list[str] = []

    # --- Gate 1 + 2: block bootstrap by game + n_trades floor ---
    boot = block_bootstrap_by_game(
        pnl_per_trade=pnl,
        game_id_per_trade=games,
        rng_seed=rng_seed,
    )
    if not (np.isfinite(boot.ci_lo) and boot.ci_lo > 0):
        reasons.append(
            f"block_bootstrap_ci_lo {boot.ci_lo:+.4f} <= 0 — not significantly profitable"
        )
    if boot.n_observations < min_n_trades:
        reasons.append(
            f"n_trades {boot.n_observations} < {min_n_trades}"
        )

    # --- Gate 3: cluster knockouts ---
    knockouts = cluster_knockouts(
        pnl_per_trade=pnl,
        game_id_per_trade=games,
        team_per_trade=primary_teams,
        date_per_trade=dates,
    )
    for key in (
        "drop_top_games_mean",
        "drop_top_team_mean",
        "drop_first_quarter_mean",
    ):
        v = knockouts.get(key, float("nan"))
        if not np.isfinite(v) or v <= 0:
            reasons.append(f"knockout_{key} {v:+.4f} <= 0")

    # --- Gate 4: season split ---
    season = stability_by_season_half(
        pnl_per_trade=pnl,
        game_date_per_trade=dates,
        game_id_per_trade=games,
        rng_seed=rng_seed,
    )
    early = season["early"]
    late = season["late"]
    season_ok = (
        np.isfinite(early.ci_lo) and np.isfinite(late.ci_lo)
        and early.ci_lo > 0 and late.ci_lo > 0
        and season["ci_overlap"]
    )
    if not season_ok:
        reasons.append(
            f"season_split: early_ci_lo={early.ci_lo:+.4f}, "
            f"late_ci_lo={late.ci_lo:+.4f}, overlap={season['ci_overlap']}"
        )

    # --- Gate 5: parity split ---
    parity = stability_by_home_team_parity(
        pnl_per_trade=pnl,
        home_team_per_trade=home_teams,
        game_id_per_trade=games,
        rng_seed=rng_seed,
    )
    even = parity["early"]
    odd = parity["late"]
    parity_ok = (
        np.isfinite(even.ci_lo) and np.isfinite(odd.ci_lo)
        and even.ci_lo > 0 and odd.ci_lo > 0
        and parity["ci_overlap"]
    )
    if not parity_ok:
        reasons.append(
            f"parity_split: even_ci_lo={even.ci_lo:+.4f}, "
            f"odd_ci_lo={odd.ci_lo:+.4f}, overlap={parity['ci_overlap']}"
        )

    # --- Gate 6: DSR (lenient / informational) ---
    sharpe = _sharpe(pnl)
    if np.isfinite(sharpe):
        dsr, dsr_p = deflated_sharpe(
            sharpe=sharpe,
            n_trials=max(1, n_total_trials_in_registry),
            n_observations=len(pnl),
            sharpe_variance=sharpe_variance_in_registry,
        )
    else:
        dsr, dsr_p = float("nan"), float("nan")
    dsr_ok = (
        np.isfinite(dsr) and np.isfinite(dsr_p)
        and dsr > _DSR_FLOOR and dsr_p < _DSR_PVAL_CEIL
    )
    if not dsr_ok:
        reasons.append(
            f"dsr={dsr:.3f} p={dsr_p:.3f} — DSR below floor "
            f"(needs dsr > {_DSR_FLOOR} and p < {_DSR_PVAL_CEIL})"
        )

    # --- Gate 7: concentration ---
    game_share, top_game = _concentration_share(pnl, games)
    team_share, top_team = _concentration_share(pnl, primary_teams)
    if game_share > _SINGLE_GAME_CONCENTRATION_MAX:
        reasons.append(
            f"single_game_share={game_share:.3f} > {_SINGLE_GAME_CONCENTRATION_MAX} "
            f"(top game: {top_game})"
        )
    if team_share > _SINGLE_TEAM_CONCENTRATION_MAX:
        reasons.append(
            f"single_team_share={team_share:.3f} > {_SINGLE_TEAM_CONCENTRATION_MAX} "
            f"(top team: {top_team})"
        )

    passed = not reasons

    # Convert BootstrapResult inside season/parity to plain dicts for the
    # PromotionDecision so it can be JSON-serialized by the registry without
    # custom encoders.
    def _br_to_dict(br: BootstrapResult) -> dict:
        return {
            "mean": br.mean,
            "ci_lo": br.ci_lo,
            "ci_hi": br.ci_hi,
            "n_observations": br.n_observations,
            "n_groups": br.n_groups,
            "n_resamples": br.n_resamples,
            "rng_seed": br.rng_seed,
        }

    season_serial = {
        "early": _br_to_dict(season["early"]),
        "late": _br_to_dict(season["late"]),
        "early_80": _br_to_dict(season["early_80"]),
        "late_80": _br_to_dict(season["late_80"]),
        "ci_overlap": season["ci_overlap"],
        "sharpe_early": season["sharpe_early"],
        "sharpe_late": season["sharpe_late"],
    }
    parity_serial = {
        "even": _br_to_dict(parity["early"]),
        "odd": _br_to_dict(parity["late"]),
        "even_80": _br_to_dict(parity["early_80"]),
        "odd_80": _br_to_dict(parity["late_80"]),
        "ci_overlap": parity["ci_overlap"],
        "sharpe_even": parity["sharpe_early"],
        "sharpe_odd": parity["sharpe_late"],
    }

    return PromotionDecision(
        passed=bool(passed),
        reasons=reasons,
        block_bootstrap_ci_lo=float(boot.ci_lo),
        block_bootstrap_ci_hi=float(boot.ci_hi),
        block_bootstrap_mean=float(boot.mean),
        dsr=float(dsr),
        dsr_p_value=float(dsr_p),
        cluster_knockouts=knockouts,
        season_split=season_serial,
        parity_split=parity_serial,
        n_trades_val=int(boot.n_observations),
        n_games_val=int(boot.n_groups),
    )
