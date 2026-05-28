"""Shared utilities for Phase −1 EDA analyses.

No pickle access. Reads from research/cache/*_v1.parquet which was produced by
cache_io.py --build-features. All phenomenon scripts in research/notebooks/
import from here.

Cost model (v2.1 plan defaults):
  - Kalshi platform fee: 0%
  - Effective bid-ask spread: 2¢ (paid once per round trip)
  - Slippage: 0.5¢ per round trip
  → COST_PER_TRADE = 0.025  (in probability units, 0..1)

The four Phase −1 gate criteria (all must hold for a phenomenon to pass):
  1. mean trade PnL > 0 after COST_PER_TRADE
  2. 95% block-bootstrap (by game_id) CI lower bound on mean PnL > 0
  3. ≥ 200 trades
  4. cluster knockout stability: drop top 5% contributing games, top team, and
     earliest season-quarter — all re-estimates must remain positive after costs
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
PARQUET_DIR = REPO_ROOT / "research" / "cache"
REPORT_DIR = REPO_ROOT / "research" / "reports"
FIGURE_DIR = REPO_ROOT / "research" / "figures"

COST_PER_TRADE = 0.025
MIN_TRADES = 200
BOOTSTRAP_N = 2000
BOOTSTRAP_CI = 0.95
TOP_GAMES_FRAC = 0.05


def load_minutes() -> pd.DataFrame:
    return pd.read_parquet(PARQUET_DIR / "minutes_v1.parquet")


def load_subs() -> pd.DataFrame:
    return pd.read_parquet(PARQUET_DIR / "subs_v1.parquet")


def load_games() -> pd.DataFrame:
    return pd.read_parquet(PARQUET_DIR / "games_v1.parquet")


def block_bootstrap_mean(values: np.ndarray, groups: np.ndarray,
                         n_boot: int = BOOTSTRAP_N,
                         ci: float = BOOTSTRAP_CI,
                         seed: int = 42) -> dict:
    """Bootstrap the mean of `values`, resampling whole groups (e.g. games).

    Returns dict with mean, ci_lo, ci_hi, n_trades, n_groups.
    """
    rng = np.random.default_rng(seed)
    values = np.asarray(values, dtype=float)
    groups = np.asarray(groups)
    if len(values) == 0:
        return {"mean": np.nan, "ci_lo": np.nan, "ci_hi": np.nan,
                "n_trades": 0, "n_groups": 0}

    # Group lookup: list of indices per group
    unique_groups, inv = np.unique(groups, return_inverse=True)
    idx_by_group = [np.where(inv == g)[0] for g in range(len(unique_groups))]
    g_means = np.empty(n_boot)
    n_groups = len(unique_groups)

    for b in range(n_boot):
        chosen = rng.integers(0, n_groups, size=n_groups)
        # Concatenate trade values from selected groups
        sample = np.concatenate([values[idx_by_group[c]] for c in chosen])
        g_means[b] = sample.mean() if len(sample) else np.nan

    alpha = (1 - ci) / 2
    lo = np.nanpercentile(g_means, 100 * alpha)
    hi = np.nanpercentile(g_means, 100 * (1 - alpha))
    return {"mean": float(values.mean()), "ci_lo": float(lo), "ci_hi": float(hi),
            "n_trades": int(len(values)), "n_groups": int(n_groups)}


def cluster_knockout(trades: pd.DataFrame,
                     game_col: str = "game_id",
                     team_col: str = "primary_team",
                     date_col: str = "date",
                     pnl_col: str = "pnl_net",
                     top_games_frac: float = TOP_GAMES_FRAC) -> dict:
    """Re-estimate mean PnL after dropping suspect clusters.

    Returns per-knockout mean PnL after costs (already in pnl_net).
    Knockouts:
      - drop top-N contributing games (top 5% by |sum of pnl|)
      - drop the single team with largest PnL contribution
      - drop the earliest season-quarter (first 25% of dates)
    """
    if trades.empty:
        return {}
    out = {"baseline_mean": float(trades[pnl_col].mean()), "n_trades": len(trades)}

    # 1) top games knockout
    game_sums = trades.groupby(game_col)[pnl_col].sum().abs()
    n_drop = max(1, int(np.ceil(len(game_sums) * top_games_frac)))
    drop_games = set(game_sums.nlargest(n_drop).index)
    sub = trades[~trades[game_col].isin(drop_games)]
    out["drop_top_games_mean"] = float(sub[pnl_col].mean()) if not sub.empty else np.nan
    out["drop_top_games_n"] = len(sub)
    out["drop_top_games_count"] = n_drop

    # 2) top team knockout
    if team_col in trades.columns:
        team_sums = trades.groupby(team_col)[pnl_col].sum()
        if not team_sums.empty:
            top_team = team_sums.idxmax()
            sub = trades[trades[team_col] != top_team]
            out["drop_top_team_mean"] = float(sub[pnl_col].mean()) if not sub.empty else np.nan
            out["drop_top_team_n"] = len(sub)
            out["drop_top_team_team"] = str(top_team)

    # 3) earliest season-quarter knockout
    if date_col in trades.columns:
        dates = pd.to_datetime(trades[date_col])
        cutoff = dates.quantile(0.25)
        sub = trades[dates > cutoff]
        out["drop_first_quarter_mean"] = float(sub[pnl_col].mean()) if not sub.empty else np.nan
        out["drop_first_quarter_n"] = len(sub)
        out["drop_first_quarter_cutoff"] = str(cutoff.date())

    return out


def season_team_split_stability(trades: pd.DataFrame,
                                game_col: str = "game_id",
                                date_col: str = "date",
                                pnl_col: str = "pnl_net") -> dict:
    """Split val into halves by (a) season chronology and (b) game_id parity,
    return mean PnL + bootstrap CI in each half. Phase −1 doesn't strictly
    require this gate (it lives in Phase 0 promotion_gate), but we compute and
    surface it for context.
    """
    out = {}
    if trades.empty:
        return out
    dates = pd.to_datetime(trades[date_col])
    median_date = dates.median()
    early = trades[dates <= median_date]
    late = trades[dates > median_date]
    out["season_early_mean"] = float(early[pnl_col].mean()) if not early.empty else np.nan
    out["season_late_mean"] = float(late[pnl_col].mean()) if not late.empty else np.nan
    out["season_early_n"] = len(early)
    out["season_late_n"] = len(late)

    # Game-id parity (hash-based, deterministic)
    h = trades[game_col].apply(lambda x: hash(x) & 1)
    even = trades[h == 0]
    odd = trades[h == 1]
    out["parity_even_mean"] = float(even[pnl_col].mean()) if not even.empty else np.nan
    out["parity_odd_mean"] = float(odd[pnl_col].mean()) if not odd.empty else np.nan
    out["parity_even_n"] = len(even)
    out["parity_odd_n"] = len(odd)
    return out


@dataclass
class PhenomenonReport:
    name: str
    description: str
    n_trades: int
    n_games: int
    mean_pnl_gross: float
    mean_pnl_net: float        # after COST_PER_TRADE
    ci_lo: float               # block-bootstrap 5th percentile (after costs)
    ci_hi: float               # block-bootstrap 97.5th percentile (after costs)
    win_rate: float
    sharpe: float              # mean / std of per-trade net PnL
    knockouts: dict
    season_team: dict
    passes_gate: bool
    reasons: list[str]

    def to_markdown(self) -> str:
        lines = [
            f"### {self.name}",
            f"_{self.description}_",
            "",
            f"- **Trades**: {self.n_trades:,}  (across {self.n_games:,} games)",
            f"- **Mean PnL gross**: {self.mean_pnl_gross:+.4f}  per trade",
            f"- **Mean PnL net (after {COST_PER_TRADE:.3f} cost)**: {self.mean_pnl_net:+.4f}",
            f"- **95% block-bootstrap CI on net mean**: [{self.ci_lo:+.4f}, {self.ci_hi:+.4f}]",
            f"- **Win rate (net > 0)**: {self.win_rate:.2%}",
            f"- **Sharpe (net)**: {self.sharpe:+.3f}",
            f"- **Cluster knockouts (net mean)**:",
            f"  - drop top-5% games ({self.knockouts.get('drop_top_games_count', '?')}): "
            f"{self.knockouts.get('drop_top_games_mean', float('nan')):+.4f}",
            f"  - drop top team ({self.knockouts.get('drop_top_team_team', '?')}): "
            f"{self.knockouts.get('drop_top_team_mean', float('nan')):+.4f}",
            f"  - drop earliest-quarter (≤ {self.knockouts.get('drop_first_quarter_cutoff', '?')}): "
            f"{self.knockouts.get('drop_first_quarter_mean', float('nan')):+.4f}",
            f"- **Season-half stability**: early={self.season_team.get('season_early_mean', float('nan')):+.4f} "
            f"vs late={self.season_team.get('season_late_mean', float('nan')):+.4f}",
            f"- **Gate**: {'✅ PASS' if self.passes_gate else '❌ FAIL'}",
        ]
        if self.reasons:
            lines.append(f"- **Reasons**: " + "; ".join(self.reasons))
        return "\n".join(lines)


def evaluate_phenomenon(name: str, description: str, trades: pd.DataFrame,
                        gross_col: str = "pnl_gross",
                        game_col: str = "game_id",
                        team_col: str = "primary_team",
                        date_col: str = "date",
                        cost: float = COST_PER_TRADE,
                        min_trades: int = MIN_TRADES) -> PhenomenonReport:
    """Apply the full Phase −1 gate to a per-trade DataFrame.

    `trades` must have columns: gross_col, game_col, date_col (and optionally team_col).
    Each row is one round-trip trade; gross_col is the unrealized PnL (in
    probability units 0..1) before transaction costs.
    """
    if trades.empty:
        return PhenomenonReport(name=name, description=description, n_trades=0,
                                n_games=0, mean_pnl_gross=np.nan,
                                mean_pnl_net=np.nan, ci_lo=np.nan, ci_hi=np.nan,
                                win_rate=np.nan, sharpe=np.nan, knockouts={},
                                season_team={}, passes_gate=False,
                                reasons=["no trades generated"])
    t = trades.copy()
    t["pnl_net"] = t[gross_col] - cost

    boot = block_bootstrap_mean(t["pnl_net"].to_numpy(),
                                t[game_col].to_numpy())
    knock = cluster_knockout(t, game_col=game_col, team_col=team_col,
                             date_col=date_col, pnl_col="pnl_net")
    season = season_team_split_stability(t, game_col=game_col,
                                          date_col=date_col, pnl_col="pnl_net")

    mean_net = boot["mean"]
    ci_lo = boot["ci_lo"]
    sharpe = (t["pnl_net"].mean() / t["pnl_net"].std(ddof=1)) if t["pnl_net"].std(ddof=1) > 0 else np.nan
    win_rate = (t["pnl_net"] > 0).mean()

    reasons = []
    if mean_net <= 0:
        reasons.append(f"mean net PnL {mean_net:+.4f} ≤ 0")
    if ci_lo <= 0:
        reasons.append(f"95% CI lower bound {ci_lo:+.4f} ≤ 0")
    if len(t) < min_trades:
        reasons.append(f"only {len(t)} trades (need ≥ {min_trades})")
    for k in ("drop_top_games_mean", "drop_top_team_mean", "drop_first_quarter_mean"):
        v = knock.get(k)
        if v is None or not np.isfinite(v):
            continue
        if v <= 0:
            reasons.append(f"{k} = {v:+.4f} ≤ 0")
    passes = not reasons

    return PhenomenonReport(
        name=name, description=description,
        n_trades=int(len(t)),
        n_games=int(t[game_col].nunique()),
        mean_pnl_gross=float(t[gross_col].mean()),
        mean_pnl_net=float(mean_net),
        ci_lo=float(ci_lo), ci_hi=float(boot["ci_hi"]),
        win_rate=float(win_rate), sharpe=float(sharpe),
        knockouts=knock, season_team=season,
        passes_gate=passes, reasons=reasons,
    )


if __name__ == "__main__":
    print("eda_kit — utilities only. Use via import from phenomenon scripts.")
    minutes = load_minutes()
    subs = load_subs()
    games = load_games()
    print(f"  minutes: {minutes.shape}")
    print(f"  subs:    {subs.shape}")
    print(f"  games:   {games.shape}")
    print(f"  games date range: {games['date'].min()} → {games['date'].max()}")
    print(f"  home win rate: {games['home_won'].mean():.3f}")
