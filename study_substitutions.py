#!/usr/bin/env python3
"""Pooled study: do NBA substitutions create a tradeable move in Kalshi/Polymarket odds?

Builds (and caches) every completed game in a date range, then for each
substitution measures the market's reaction in the following minutes — split by
sub type and market — to test for under/over-reaction beyond trading costs.

Example:
    python study_substitutions.py --start 2026-04-15 --end 2026-05-22
    python study_substitutions.py --start 2026-05-01 --end 2026-05-22 --limit 10
"""
import argparse
import os

from nba_odds_study import batch, schedule, sub_study


def print_summary(s: dict, outdir: str) -> None:
    print("\n" + "=" * 70)
    print(f"  SUBSTITUTION -> ODDS REACTION   ({s['n_subs']} subs across {s['n_games']} games)")
    print("=" * 70)
    print("  sub types:", {k: int(v) for k, v in s["by_type"].items()})
    c = s["cost_assumption"]
    print(f"  cost assumption: moneyline {c['ml_pp']}pp round-trip, total {c['total_pts']}pt\n")

    print("  RESIDUAL win-prob drift @ +3 min (score removed; own-team perspective)")
    print(f"    {'type':<13}{'n':>5}{'mean pp':>10}{'t':>7}{'signed':>9}{'hit%':>7}")
    for r in s["ml_residual_drift_+3min_pp"]:
        print(f"    {r['type']:<13}{r['n']:>5}{r['mean']:>10.2f}{r['t']:>7.1f}"
              f"{r['signed']:>9.2f}{100*r['hit_rate']:>7.0f}")

    print("\n  TOTAL-line drift @ +3 min (points)")
    print(f"    {'type':<13}{'n':>5}{'mean':>10}{'t':>7}{'signed':>9}{'hit%':>7}")
    for r in s["total_line_drift_+3min_pts"]:
        print(f"    {r['type']:<13}{r['n']:>5}{r['mean']:>10.2f}{r['t']:>7.1f}"
              f"{r['signed']:>9.2f}{100*r['hit_rate']:>7.0f}")

    print("\n  VERDICT (moneyline, signed vs cost):")
    for typ, v in s["verdict_moneyline"].items():
        print(f"    {typ:<13} {v}")

    print("\n  Outputs:", outdir)
    for f in ("6_sub_reaction_trajectories.png", "7_sub_edge_by_type.png",
              "sub_reactions.csv", "sub_reaction_summary.csv", "coverage.csv"):
        print("   -", os.path.join(outdir, f))
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="range start YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="range end YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=None, help="cap number of games (quick runs)")
    ap.add_argument("--cost-wp", type=float, default=1.5, help="assumed moneyline round-trip cost (pp)")
    ap.add_argument("--outdir", default=os.path.join("market_data", "nba_studies", "_substitution_study"))
    a = ap.parse_args()

    games = schedule.completed_games(a.start, a.end)
    if a.limit:
        games = games[: a.limit]
    print(f"{len(games)} completed games in {a.start}..{a.end}; building (cached) ...")
    reactions, coverage = batch.sub_reactions(games)
    ok = int(coverage["ok"].sum()) if len(coverage) else 0
    print(f"\ngames with data: {ok}/{len(games)} | usable sub events: {len(reactions)}")
    os.makedirs(a.outdir, exist_ok=True)
    coverage.to_csv(os.path.join(a.outdir, "coverage.csv"), index=False)
    if reactions.empty:
        print("No reactions collected.")
        return
    summary = sub_study.run(reactions, a.outdir, cost_wp=a.cost_wp)
    print_summary(summary, a.outdir)


if __name__ == "__main__":
    main()
