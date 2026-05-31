#!/usr/bin/env python3
"""Analyze the relationship between NBA in-game events (score & lineup changes)
and Kalshi / Polymarket odds for every market offered on a single game.

Example:
    python analyze_nba_game.py --date 2026-05-22 --away OKC --home SAS

Data sources (all historical, ~1-minute resolution, wall-clock aligned):
  * NBA play-by-play .......... ESPN summary API (scores, substitutions)
  * Kalshi odds ............... candlesticks endpoint (winner / spread / total)
  * Polymarket odds ........... CLOB prices-history (winner / spread / total)
"""
import argparse
import os

from research.nba_odds_study import analysis
from research.nba_odds_study import dataset as ds
from research.nba_odds_study import teams


def _fmt(v):
    return "n/a" if v is None else v


def print_summary(s: dict, outdir: str) -> None:
    fs = s["final_score"]
    print("\n" + "=" * 64)
    print(f"  {s['game']}")
    print("=" * 64)
    print(f"  Final: winner {fs['winner']}  ({fs})")
    pg = s["pregame_home_winprob"]
    print(f"  Pre-game home win prob   Kalshi={_fmt(pg['kalshi'])}  Polymarket={_fmt(pg['polymarket'])}")

    ss = s["score_sensitivity"]
    print("\n  SCORE -> ODDS")
    print(f"    home win prob per point of margin: {ss['prob_per_point_overall']:+.4f}/pt")
    if "prob_per_point_first_half" in ss:
        print(f"      first half {ss['prob_per_point_first_half']:+.4f}/pt"
              f"   second half {ss.get('prob_per_point_second_half'):+.4f}/pt")

    print("\n  EVENT STUDIES (mean Δ win prob a few min after the event)")
    for k, v in s["events"].items():
        chg = next((vv for kk, vv in v.items() if kk.startswith("mean_change")), None)
        print(f"    {k:<22} n={v['n']:<3} Δ={chg:+.4f}" if chg is not None else f"    {k}: n={v['n']}")

    cp = s["cross_platform"]
    print("\n  KALSHI vs POLYMARKET")
    if cp:
        print(f"    mean |divergence| = {cp.get('mean_abs_divergence')}   max = {cp.get('max_abs_divergence')}")
        if "best_lag_min" in cp:
            print(f"    lead-lag: best lag {cp['best_lag_min']} min (corr {cp['best_lag_corr']}); "
                  f"contemporaneous {cp.get('contemporaneous_corr')}")
            print(f"    note: {cp['lead_lag_note']}")

    t = s["totals"]
    print("\n  TOTAL POINTS")
    print(f"    final total = {t['final_total']}")
    for plat in ("kalshi", "polymarket"):
        if f"{plat}_pregame_line" in t:
            print(f"    {plat}: pre-game line {t[f'{plat}_pregame_line']} -> final implied {t[f'{plat}_final_line']}")

    wpi = s.get("player_wp_impact", {})
    if wpi:
        print("\n  WIN-PROBABILITY IMPACT  (% win prob / minute on court, leverage-weighted)")
        print(f"    stints: {wpi['n_stints']}")
        print("    top:")
        for r in wpi["leaders"]:
            print(f"      {r['player']:<26} {r['team']}  {r['wpa_per_min_pct']:+.2f}%/min  ({r['minutes']:.0f}m)")
        print("    bottom:")
        for r in wpi["laggards"]:
            print(f"      {r['player']:<26} {r['team']}  {r['wpa_per_min_pct']:+.2f}%/min  ({r['minutes']:.0f}m)")

    print("\n  Outputs written to:", outdir)
    for f in ("1_overview.png", "2_sensitivity.png", "3_event_studies.png",
              "4_cross_platform.png", "5_player_wp_impact.png", "player_wp_impact.csv",
              "stints.csv", "minute.csv", "odds_long.csv", "summary.json"):
        print("   -", os.path.join(outdir, f))
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="ET game date, YYYY-MM-DD")
    ap.add_argument("--away", required=True, help="away team tricode (e.g. OKC)")
    ap.add_argument("--home", required=True, help="home team tricode (e.g. SAS)")
    ap.add_argument("--outdir", default=None, help="output directory (default: market_data/nba_studies/...)")
    a = ap.parse_args()

    away, home = a.away.upper(), a.home.upper()
    for tri in (away, home):
        if tri not in teams.TRICODES:
            raise SystemExit(f"Unknown tricode {tri!r}. Valid: {', '.join(sorted(teams.TRICODES))}")

    outdir = a.outdir or os.path.join("market_data", "nba_studies", f"{a.date}_{away}_at_{home}")
    print(f"Building dataset for {away} @ {home} on {a.date} ...")
    d = ds.build(a.date, away, home)
    print(f"  plays={len(d.game.plays)} subs={len(d.subs)} "
          f"runs={len(d.runs)} lead_changes={len(d.lead_changes)}")
    print(f"  minute rows={len(d.minute)}  odds series points={len(d.odds_long)}")
    print("Running analysis ...")
    summary = analysis.run(d, outdir)
    print_summary(summary, outdir)


if __name__ == "__main__":
    main()
