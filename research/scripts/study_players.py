#!/usr/bin/env python3
"""Player-by-player substitution -> odds reaction, pooled over many games.

Builds (and caches) every completed game in a date range, assembles the
per-substitution reaction table, then aggregates it per player: how does the
market's score-residual win probability drift after THIS player enters/exits?
Benjamini-Hochberg FDR correction + an economic-size gate keep the ranking honest.

Examples:
    # full season (Polymarket has the whole season; Kalshi only ran playoffs)
    python study_players.py --start 2025-10-21 --end 2026-05-22 --platforms polymarket
    # quick demo on cached playoff games
    python study_players.py --start 2026-04-15 --end 2026-05-22 --platforms polymarket
"""
import argparse
import os

from research.nba_odds_study import batch, player_study, schedule


def print_summary(s: dict, outdir: str) -> None:
    print("\n" + "=" * 70)
    print("  PLAYER-BY-PLAYER SUBSTITUTION EDGE")
    print("=" * 70)
    if s.get("n_players_tested", 0) == 0:
        print("  " + s.get("note", "no players met the sample threshold"))
        return
    print(f"  players tested (>= {s['min_n']} sub events): {s['n_players_tested']}")
    print(f"  reaction horizon: +{s['horizon_min']} min | cost gate: {s['cost_wp']}pp | "
          f"FDR q<{s['q_thresh']}")
    print(f"  spread of per-player effects: {s['effect_pp_std_across_players']}pp\n")
    print(f"  significant after BH + cost gate: {s['n_significant']}")
    for r in s["significant_players"]:
        print(f"      {r['player']:<26} {r['team']}  {r['effect_pp']:+.2f}pp  "
              f"n={r['n']}  t={r['t']:.1f}  q={r['q']:.3f}")
    print("\n  TOP (market under-rates their status change):")
    for r in s["top"]:
        print(f"      {r['player']:<26} {r['team']}  {r['effect_pp']:+.2f}pp  n={r['n']}  "
              f"t={r['t']:.1f}  q={r['q']:.2f}")
    print("\n  BOTTOM (market over-rates):")
    for r in s["bottom"]:
        print(f"      {r['player']:<26} {r['team']}  {r['effect_pp']:+.2f}pp  n={r['n']}  "
              f"t={r['t']:.1f}  q={r['q']:.2f}")
    print("\n  Outputs:", outdir)
    for f in ("player_reaction.csv", "8_player_volcano.png", "9_player_ranks.png", "coverage.csv"):
        print("   -", os.path.join(outdir, f))
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--platforms", default="polymarket", help="comma list, or 'all'")
    ap.add_argument("--min-n", type=int, default=20, help="min sub events per player")
    ap.add_argument("--horizon", type=int, default=3, help="reaction window minutes")
    ap.add_argument("--cost-wp", type=float, default=1.5)
    ap.add_argument("--outdir", default=os.path.join("market_data", "nba_studies", "_player_study"))
    a = ap.parse_args()

    platforms = None if a.platforms.strip() == "all" else set(a.platforms.split(","))
    games = schedule.completed_games(a.start, a.end)
    if a.limit:
        games = games[: a.limit]
    print(f"{len(games)} completed games {a.start}..{a.end} | platforms={a.platforms}; building (cached) ...")
    reactions, coverage = batch.sub_reactions(games, platforms=platforms)
    ok = int(coverage["ok"].sum()) if len(coverage) else 0
    print(f"\ngames with data: {ok}/{len(games)} | usable sub events: {len(reactions)}")
    os.makedirs(a.outdir, exist_ok=True)
    coverage.to_csv(os.path.join(a.outdir, "coverage.csv"), index=False)
    if reactions.empty:
        print("No reactions collected.")
        return
    reactions.to_csv(os.path.join(a.outdir, "sub_reactions.csv"), index=False)
    summary = player_study.run(reactions, a.outdir, min_n=a.min_n, horizon=a.horizon, cost_wp=a.cost_wp)
    print_summary(summary, a.outdir)


if __name__ == "__main__":
    main()
