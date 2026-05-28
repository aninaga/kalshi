"""Block bootstrap by game_id and stability splits — the decisive promotion gate.

This is the trial-grade companion to `research/eda_kit.block_bootstrap_mean`
(Phase −1 reference implementation). The Phase −1 version is per-phenomenon
and reports raw dicts; this version returns a typed `BootstrapResult` for use
in the registry / promotion gate, and adds:

  - `stability_by_season_half`: chronological half-split + CI overlap check.
  - `stability_by_home_team_parity`: home-team-hash-parity split + CI overlap.
  - `cluster_knockouts`: drop top games / top team / earliest quarter.

**Bug fix vs Phase −1's `cluster_knockout`**: the old code did
`team_sums.idxmax()` which drops the team with the largest *positive* PnL.
The gate's intent is "no single team accounts for too much" — symmetric.
This version uses `abs().idxmax()` for both top-games (already abs in Phase −1,
preserved here) and top-team (the fix).

The block is the game_id. Within a game, trades are path-dependent (each
trade conditions on the same trajectory of the same prediction market), so
the IID bootstrap would understate variance by treating correlated trades as
independent. Resampling game_ids with replacement preserves the intra-game
dependence structure.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


# ---------------------------------------------------------------------------
# Core block bootstrap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapResult:
    """Result of `block_bootstrap_by_game`.

    Attributes
    ----------
    mean : float
        Observed sample mean of `pnl_per_trade` (deterministic, not a
        bootstrap statistic).
    ci_lo : float
        (1-ci)/2 percentile of the bootstrap distribution of resampled means.
    ci_hi : float
        1 - (1-ci)/2 percentile of the bootstrap distribution.
    n_observations : int
        Total trades that went in.
    n_groups : int
        Distinct game_ids.
    n_resamples : int
        Bootstrap iterations.
    rng_seed : int
        Seed used (recorded for reproducibility).
    """
    mean: float
    ci_lo: float
    ci_hi: float
    n_observations: int
    n_groups: int
    n_resamples: int
    rng_seed: int


def block_bootstrap_by_game(
    pnl_per_trade: np.ndarray,
    game_id_per_trade: np.ndarray,
    n_resamples: int = 5000,
    ci: float = 0.95,
    rng_seed: int = 42,
) -> BootstrapResult:
    """Resample game_ids with replacement, concatenate that game's trades,
    compute the mean. Repeat `n_resamples` times. Return percentile CI.

    Parameters
    ----------
    pnl_per_trade : np.ndarray, shape (n_trades,)
        Per-trade PnL already net of all costs.
    game_id_per_trade : np.ndarray, shape (n_trades,)
        Group labels for the bootstrap. Resampling is at this level.
    n_resamples : int
        Number of bootstrap iterations. 5000 is the default — empirically
        stable for percentile CIs at the 95% level on trial sizes in the
        100–10k range. Higher gives smoother CIs but doesn't materially
        change the pass/fail decision.
    ci : float
        Confidence level (default 0.95 → 2.5 / 97.5 percentile cuts).
    rng_seed : int
        Seed for deterministic reproduction. The promotion gate uses 42 by
        default; tests can pass other seeds.

    Returns
    -------
    BootstrapResult

    Notes
    -----
    Edge cases:
      - Empty input → mean / CI = NaN.
      - Single group → CI collapses to the mean (every resample is the same
        data); this is reported but the gate will reject on `n_trades` or
        explicitly via the bootstrap CI being degenerate.
    """
    pnl = np.asarray(pnl_per_trade, dtype=float)
    groups = np.asarray(game_id_per_trade)

    if len(pnl) == 0:
        return BootstrapResult(
            mean=float("nan"),
            ci_lo=float("nan"),
            ci_hi=float("nan"),
            n_observations=0,
            n_groups=0,
            n_resamples=n_resamples,
            rng_seed=rng_seed,
        )
    if len(pnl) != len(groups):
        raise ValueError(
            f"pnl_per_trade length {len(pnl)} != game_id_per_trade length {len(groups)}"
        )

    rng = np.random.default_rng(rng_seed)
    unique_groups, inv = np.unique(groups, return_inverse=True)
    n_groups = len(unique_groups)
    # Precompute per-group index arrays once (vs once per resample).
    idx_by_group: list[np.ndarray] = [
        np.where(inv == g)[0] for g in range(n_groups)
    ]
    resampled_means = np.empty(n_resamples, dtype=float)
    for b in range(n_resamples):
        chosen = rng.integers(0, n_groups, size=n_groups)
        sample = np.concatenate([pnl[idx_by_group[c]] for c in chosen])
        # `sample` cannot be empty because every original group has ≥1 trade.
        resampled_means[b] = sample.mean()

    alpha = (1.0 - ci) / 2.0
    ci_lo = float(np.nanpercentile(resampled_means, 100.0 * alpha))
    ci_hi = float(np.nanpercentile(resampled_means, 100.0 * (1.0 - alpha)))

    return BootstrapResult(
        mean=float(pnl.mean()),
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        n_observations=int(len(pnl)),
        n_groups=int(n_groups),
        n_resamples=int(n_resamples),
        rng_seed=int(rng_seed),
    )


# ---------------------------------------------------------------------------
# Stability splits
# ---------------------------------------------------------------------------


def _cis_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    """Two intervals overlap iff max(lo) <= min(hi)."""
    if not (np.isfinite(lo1) and np.isfinite(hi1) and np.isfinite(lo2) and np.isfinite(hi2)):
        return False
    return max(lo1, lo2) <= min(hi1, hi2)


def _sharpe(values: np.ndarray) -> float:
    """Mean / std (ddof=1) of `values`. Returns NaN on degenerate inputs."""
    arr = np.asarray(values, dtype=float)
    if len(arr) < 2:
        return float("nan")
    std = arr.std(ddof=1)
    if std <= 0 or not np.isfinite(std):
        return float("nan")
    return float(arr.mean() / std)


def stability_by_season_half(
    pnl_per_trade: np.ndarray,
    game_date_per_trade: np.ndarray,
    game_id_per_trade: np.ndarray,
    rng_seed: int = 42,
) -> dict:
    """Split chronologically into two halves by median date; run
    `block_bootstrap_by_game` on each half at the standard 95% CI AND at the
    80% CI (used for the overlap check).

    Returns
    -------
    dict with keys:
      'early'  : BootstrapResult at 95% CI on the early half.
      'late'   : BootstrapResult at 95% CI on the late half.
      'early_80': BootstrapResult at 80% CI on the early half.
      'late_80' : BootstrapResult at 80% CI on the late half.
      'ci_overlap' : bool — True iff BOTH halves have positive 95% CI lower
                     bounds AND their 80% CI intervals overlap.
      'sharpe_early' : float — Sharpe of the early half's per-trade PnL.
      'sharpe_late'  : float — Sharpe of the late half's per-trade PnL.
    """
    pnl = np.asarray(pnl_per_trade, dtype=float)
    dates = np.asarray(game_date_per_trade)
    games = np.asarray(game_id_per_trade)

    # Convert dates to datetime64 for sortable comparison even if input is
    # mixed strings, datetime, or already datetime64.
    if dates.dtype.kind not in ("M",):
        dates = np.array(dates, dtype="datetime64[s]")

    if len(pnl) == 0:
        empty = BootstrapResult(
            mean=float("nan"), ci_lo=float("nan"), ci_hi=float("nan"),
            n_observations=0, n_groups=0, n_resamples=0, rng_seed=rng_seed,
        )
        return {
            "early": empty, "late": empty,
            "early_80": empty, "late_80": empty,
            "ci_overlap": False,
            "sharpe_early": float("nan"),
            "sharpe_late": float("nan"),
        }

    median_date = np.median(dates.astype("datetime64[s]").astype("int64"))
    median_dt = np.datetime64(int(median_date), "s")
    early_mask = dates <= median_dt
    late_mask = ~early_mask

    early = block_bootstrap_by_game(
        pnl[early_mask], games[early_mask], ci=0.95, rng_seed=rng_seed,
    )
    late = block_bootstrap_by_game(
        pnl[late_mask], games[late_mask], ci=0.95, rng_seed=rng_seed + 1,
    )
    early_80 = block_bootstrap_by_game(
        pnl[early_mask], games[early_mask], ci=0.80, rng_seed=rng_seed,
    )
    late_80 = block_bootstrap_by_game(
        pnl[late_mask], games[late_mask], ci=0.80, rng_seed=rng_seed + 1,
    )

    both_positive = (
        np.isfinite(early.ci_lo) and np.isfinite(late.ci_lo)
        and early.ci_lo > 0 and late.ci_lo > 0
    )
    overlap = both_positive and _cis_overlap(
        early_80.ci_lo, early_80.ci_hi, late_80.ci_lo, late_80.ci_hi,
    )

    return {
        "early": early,
        "late": late,
        "early_80": early_80,
        "late_80": late_80,
        "ci_overlap": bool(overlap),
        "sharpe_early": _sharpe(pnl[early_mask]),
        "sharpe_late": _sharpe(pnl[late_mask]),
    }


def stability_by_home_team_parity(
    pnl_per_trade: np.ndarray,
    home_team_per_trade: np.ndarray,
    game_id_per_trade: np.ndarray,
    rng_seed: int = 42,
) -> dict:
    """Split trades by `hash(home_team) & 1` parity; run block_bootstrap
    on each half. Defense against single-counterparty mispricing — if the
    edge only exists for half the universe of teams, that's a regime
    artefact, not a tradeable signal.

    Same return shape as `stability_by_season_half` (without the
    sharpe_early/sharpe_late naming convention — uses 'sharpe_even' and
    'sharpe_odd' to match the parity-split semantics).
    """
    pnl = np.asarray(pnl_per_trade, dtype=float)
    teams = np.asarray(home_team_per_trade)
    games = np.asarray(game_id_per_trade)

    if len(pnl) == 0:
        empty = BootstrapResult(
            mean=float("nan"), ci_lo=float("nan"), ci_hi=float("nan"),
            n_observations=0, n_groups=0, n_resamples=0, rng_seed=rng_seed,
        )
        return {
            "early": empty, "late": empty,
            "early_80": empty, "late_80": empty,
            "ci_overlap": False,
            "sharpe_early": float("nan"),
            "sharpe_late": float("nan"),
        }

    parity = np.array([hash(str(t)) & 1 for t in teams])
    even_mask = parity == 0
    odd_mask = ~even_mask

    even = block_bootstrap_by_game(
        pnl[even_mask], games[even_mask], ci=0.95, rng_seed=rng_seed,
    )
    odd = block_bootstrap_by_game(
        pnl[odd_mask], games[odd_mask], ci=0.95, rng_seed=rng_seed + 1,
    )
    even_80 = block_bootstrap_by_game(
        pnl[even_mask], games[even_mask], ci=0.80, rng_seed=rng_seed,
    )
    odd_80 = block_bootstrap_by_game(
        pnl[odd_mask], games[odd_mask], ci=0.80, rng_seed=rng_seed + 1,
    )

    both_positive = (
        np.isfinite(even.ci_lo) and np.isfinite(odd.ci_lo)
        and even.ci_lo > 0 and odd.ci_lo > 0
    )
    overlap = both_positive and _cis_overlap(
        even_80.ci_lo, even_80.ci_hi, odd_80.ci_lo, odd_80.ci_hi,
    )

    # Keep the same key naming as season_half for promotion_gate symmetry.
    return {
        "early": even,
        "late": odd,
        "early_80": even_80,
        "late_80": odd_80,
        "ci_overlap": bool(overlap),
        "sharpe_early": _sharpe(pnl[even_mask]),
        "sharpe_late": _sharpe(pnl[odd_mask]),
    }


# ---------------------------------------------------------------------------
# Cluster knockouts (bug-fixed vs eda_kit.cluster_knockout)
# ---------------------------------------------------------------------------


def _drop_top_groups_by_abs(
    pnl: np.ndarray,
    group: np.ndarray,
    n_drop: int,
) -> np.ndarray:
    """Return boolean mask True for rows whose group is NOT among the top
    `n_drop` groups by |sum of pnl|. n_drop=0 returns an all-True mask.
    """
    if n_drop <= 0 or len(pnl) == 0:
        return np.ones(len(pnl), dtype=bool)
    unique_groups, inv = np.unique(group, return_inverse=True)
    sums = np.zeros(len(unique_groups), dtype=float)
    np.add.at(sums, inv, pnl)
    abs_sums = np.abs(sums)
    n_drop = min(n_drop, len(unique_groups))
    # Indices of the top-n by |sum|.
    drop_idx = np.argpartition(abs_sums, -n_drop)[-n_drop:]
    drop_set = set(drop_idx.tolist())
    keep_mask = np.array([i not in drop_set for i in inv])
    return keep_mask


def cluster_knockouts(
    pnl_per_trade: np.ndarray,
    game_id_per_trade: np.ndarray,
    team_per_trade: np.ndarray,
    date_per_trade: np.ndarray,
    top_games_frac: float = 0.05,
) -> dict:
    """Drop top 5% contributing games (by |sum pnl|), drop top team (by
    |sum pnl|), drop earliest 25% of season — recompute mean each time.

    **Bug fix vs Phase −1**: top team is selected by `|sum pnl|`, not signed.
    The gate's intent is "no single team accounts for too much" — symmetric
    in sign. Phase −1's `idxmax()` would only drop the *winningest* team and
    miss the case where a single losing team is dominating.

    Returns
    -------
    dict with keys:
      'baseline_mean'           : observed mean (for reference)
      'drop_top_games_mean'     : mean after dropping top top_games_frac games
      'drop_top_team_mean'      : mean after dropping the |abs|-top team
      'drop_first_quarter_mean' : mean after dropping the earliest 25% of dates
      'drop_top_games_count'    : how many games were dropped
      'drop_top_team_team'      : the team that was dropped (str)
      'drop_first_quarter_cutoff_date' : ISO date string at the 25th-percentile
    """
    pnl = np.asarray(pnl_per_trade, dtype=float)
    games = np.asarray(game_id_per_trade)
    teams = np.asarray(team_per_trade)
    dates = np.asarray(date_per_trade)

    out: dict = {
        "baseline_mean": float("nan"),
        "drop_top_games_mean": float("nan"),
        "drop_top_team_mean": float("nan"),
        "drop_first_quarter_mean": float("nan"),
        "drop_top_games_count": 0,
        "drop_top_team_team": None,
        "drop_first_quarter_cutoff_date": None,
    }
    if len(pnl) == 0:
        return out
    out["baseline_mean"] = float(pnl.mean())

    # 1) Top games knockout — drop ceil(top_games_frac * n_games) games by |sum pnl|.
    unique_games = np.unique(games)
    n_drop_games = max(1, int(np.ceil(len(unique_games) * top_games_frac)))
    keep = _drop_top_groups_by_abs(pnl, games, n_drop_games)
    if keep.any():
        out["drop_top_games_mean"] = float(pnl[keep].mean())
    out["drop_top_games_count"] = int(n_drop_games)

    # 2) Top team knockout — drop the team with the largest |sum pnl|.
    unique_teams, team_inv = np.unique(teams, return_inverse=True)
    team_sums = np.zeros(len(unique_teams), dtype=float)
    np.add.at(team_sums, team_inv, pnl)
    if len(unique_teams) > 0:
        top_team_idx = int(np.argmax(np.abs(team_sums)))
        top_team = unique_teams[top_team_idx]
        keep_team = team_inv != top_team_idx
        if keep_team.any():
            out["drop_top_team_mean"] = float(pnl[keep_team].mean())
        out["drop_top_team_team"] = str(top_team)

    # 3) Earliest quarter knockout — drop trades with date ≤ 25th percentile.
    if dates.dtype.kind not in ("M",):
        dates_dt = np.array(dates, dtype="datetime64[s]")
    else:
        dates_dt = dates.astype("datetime64[s]")
    int_dates = dates_dt.astype("int64")
    cutoff_int = int(np.percentile(int_dates, 25.0))
    cutoff_dt = np.datetime64(cutoff_int, "s")
    keep_late = dates_dt > cutoff_dt
    if keep_late.any():
        out["drop_first_quarter_mean"] = float(pnl[keep_late].mean())
    out["drop_first_quarter_cutoff_date"] = str(cutoff_dt.astype("datetime64[D]"))

    return out
