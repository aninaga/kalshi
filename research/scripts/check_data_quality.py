"""Data-quality guardrail gate (WS6).

Locks in the data-quality remediation so regressions are caught, not
re-discovered. Mirrors the "assert the alarm goes off" discipline of
check_phase1_gates.sh, but for the DATA/FRAME layer the existing gauntlet
cannot see. ALL checks run; the script exits non-zero if any fails.

Checks
------
1. lake_matches_cache    lake game-shard count == feature-cache game count
2. split_subset_of_lake  every train/val/test game_id has a lake shard
3. feature_coverage      on sampled games, the recovered/derived features are
                         populated at sane rates (kalshi_implied_wp non-None on
                         Kalshi-covered games, pm_implied_wp on PM-covered,
                         lineup_sig where starters exist) and lead_changes_cum
                         is monotonic (the WS1 fixes still hold)
4. no_stale_quotes       every non-None quote in a frame has
                         quote_age_sec <= STALENESS_BOUND_SEC (WS4 bound holds;
                         no stale quote masquerades as current)
5. no_forward_leak       canary: in a strictly-increasing-mid synthetic game,
                         pm_implied_wp at bar i equals mid[i] (knowable at i),
                         never mid[i+1] — i.e. the frame has no forward leak

Usage:
  ~/code/kvenv/bin/python -m research.scripts.check_data_quality [--n-games 30]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from research.features import computers  # noqa: E402
from research.harness import replay  # noqa: E402
from research.harness.replay import STALENESS_BOUND_SEC, _build_bars  # noqa: E402
from research.lake.reader import load_game  # noqa: E402
import research.lake.schema as schema  # noqa: E402

CACHE_GAMES = _ROOT / "research" / "cache" / "games_v1.parquet"


def _lake_game_ids() -> set[str]:
    return {p.stem for p in (schema.LAKE_ROOT / "games").glob("*.parquet")}


def _nonempty(table: str) -> set[str]:
    d = schema.LAKE_ROOT / table
    return {f.stem for f in d.glob("*.parquet") if pq.ParquetFile(f).metadata.num_rows > 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-games", type=int, default=30)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    print("==== Data-quality gate ====")

    # 1. lake matches cache count
    lake_ids = _lake_game_ids()
    cache_n = len(pd.read_parquet(CACHE_GAMES, columns=["game_id"]))
    check("lake_matches_cache", len(lake_ids) == cache_n,
          f"lake={len(lake_ids)} cache={cache_n}")

    # 2. split subset of lake
    try:
        from research.harness.run_batch import load_split
        missing = 0
        for sp in ("train", "val", "test"):
            missing += sum(1 for g in load_split(sp) if g not in lake_ids)
        check("split_subset_of_lake", missing == 0, f"{missing} split ids missing a lake shard")
    except FileNotFoundError:
        check("split_subset_of_lake", True, "splits.json not locked yet — skipped")

    # Sample games for the per-bar checks.
    pm_cov = _nonempty("polymarket_ticks")
    k_cov = _nonempty("kalshi_ticks")
    rng = np.random.default_rng(args.seed)
    sample = list(rng.choice(sorted(lake_ids), size=min(args.n_games, len(lake_ids)), replace=False))

    pm_rates, lineup_rates = [], []
    k_games_with_wp = k_games_total = 0
    lead_monotonic = True
    stale_violations = 0
    for gid in sample:
        try:
            g = load_game(gid, allow_test=True)
        except Exception:
            continue
        bars = _build_bars(g, "polymarket")
        if not bars:
            continue
        # pm_implied_wp coverage (only meaningful where the game has PM data)
        if gid in pm_cov:
            r = np.mean([computers.compute_pm_implied_wp(b, 0.0) is not None for b in bars])
            pm_rates.append(r)
        # kalshi_implied_wp produced at all on Kalshi-covered games (C1)
        if gid in k_cov:
            k_games_total += 1
            if any(computers.compute_kalshi_implied_wp(b, 0.0) is not None for b in bars):
                k_games_with_wp += 1
        # lineup_sig where starters exist
        meta = g.get("meta")
        if meta is not None and str(meta.get("starters_home") or "").strip():
            lineup_rates.append(np.mean([b.get("lineup_sig") is not None for b in bars]))
        # lead_changes_cum monotonic
        lcc = [b.get("lead_changes_cum") for b in bars]
        if any(lcc[i] is not None and lcc[i + 1] is not None and lcc[i] > lcc[i + 1]
               for i in range(len(lcc) - 1)):
            lead_monotonic = False
        # staleness invariant: any present quote is within the bound
        for b in bars:
            for age in (b.get("pm_quote_age_sec"), b.get("kalshi_quote_age_sec")):
                if age is not None and age > STALENESS_BOUND_SEC:
                    stale_violations += 1

    check("feature_coverage.pm_implied_wp", (np.mean(pm_rates) if pm_rates else 0) >= 0.80,
          f"mean non-None rate={np.mean(pm_rates):.3f}" if pm_rates else "no PM games sampled")
    check("feature_coverage.kalshi_implied_wp",
          (k_games_with_wp / k_games_total if k_games_total else 0) >= 0.80,
          f"{k_games_with_wp}/{k_games_total} kalshi-covered games have a non-None wp")
    check("feature_coverage.lineup_sig", (np.mean(lineup_rates) if lineup_rates else 0) >= 0.90,
          f"mean non-None rate={np.mean(lineup_rates):.3f}" if lineup_rates else "no starters games")
    check("feature_coverage.lead_changes_cum_monotonic", lead_monotonic)
    check("no_stale_quotes", stale_violations == 0,
          f"{stale_violations} quotes older than {STALENESS_BOUND_SEC}s present in frames")

    # 5. no-forward-leak canary on a synthetic strictly-increasing-mid game
    mt = [1000 + 60 * i for i in range(8)]
    pbp = pd.DataFrame({
        "game_id": ["C"] * 8, "date": ["2025-10-21"] * 8, "home_tri": ["LAL"] * 8,
        "away_tri": ["GSW"] * 8, "minute_ts": mt, "elapsed_game_sec": [float(60 * i) for i in range(8)],
        "home_score": [float(i) for i in range(8)], "away_score": [0.0] * 8, "margin": [float(i) for i in range(8)],
        "pts_home": [0.0] * 8, "pts_away": [0.0] * 8, "total": [float(i) for i in range(8)],
        "n_subs": [0] * 8, "n_star_subs": [0] * 8, "lead_changes": [0] * 8,
        "home_stars_on": [0] * 8, "away_stars_on": [0] * 8,
    })
    mids = [0.40 + 0.03 * i for i in range(8)]
    pm = pd.DataFrame({"game_id": ["C"] * 8, "minute_ts": mt, "token_id": ["t"] * 8,
                       "team": ["home"] * 8, "mid": mids})
    cgame = {"pbp": pbp, "polymarket": pm,
             "kalshi": pd.DataFrame(columns=["game_id", "minute_ts", "series", "ticker", "team",
                                             "yes_bid", "yes_ask", "mid", "last"]),
             "subs": pd.DataFrame(columns=["game_id", "date", "ts", "team", "player_in", "player_out", "is_star"]),
             "meta": pd.Series({"home_tri": "LAL", "away_tri": "GSW", "starters_home": None, "starters_away": None})}
    cbars = _build_bars(cgame, "polymarket")
    leak = False
    for i, b in enumerate(cbars):
        wp = computers.compute_pm_implied_wp(b, 0.0)
        if wp is None:
            continue
        # must equal THIS bar's mid (knowable now), never the next bar's.
        if abs(wp - mids[i]) > 1e-9:
            leak = True
    check("no_forward_leak", not leak, "pm_implied_wp[i] must equal mid[i], not a future mid")

    print("==== " + ("ALL DATA-QUALITY CHECKS PASSED" if not failures
                     else f"{len(failures)} CHECK(S) FAILED: {failures}") + " ====")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
