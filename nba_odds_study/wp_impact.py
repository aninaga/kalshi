"""Win-Probability Impact (WPA +/-): a market-odds version of NBA plus/minus.

The game is split into *stints* — maximal intervals between substitutions (by
either team) during which the on-court ten is fixed. Each stint's change in the
home team's market win probability is credited (per minute) to every on-court
player of that team, and debited to every on-court opponent. Summed over a
player's floor time this is their **win-probability added (WPA)**, and divided
by their minutes it is **WPA per minute** — the stat the user asked for.

Two flavors:
  * ``wpa``       — raw win-prob swing. Because win prob barely moves per point
                    in a blowout but a lot per point in a close game, this is
                    effectively *leverage-weighted* plus/minus (clutch minutes
                    count more, garbage time ~0).
  * ``resid_wpa`` — same, but on win prob with the score-implied part removed
                    (home_wp minus its linear fit on margin). Isolates the
                    market re-rating a team *beyond* what the scoreboard says.
"""
from __future__ import annotations

import os

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from . import dataset as ds  # noqa: E402
from . import espn  # noqa: E402


def _wp_columns(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    cols = [c for c in ("kalshi_home_winprob", "pm_home_winprob") if c in df]
    wp = df[cols].mean(axis=1)
    m = wp.notna() & df["margin"].notna()
    if m.sum() >= 5:
        b1, b0 = np.polyfit(df["margin"][m], wp[m], 1)
        resid = wp - (b0 + b1 * df["margin"])
    else:
        resid = wp * np.nan
    return wp, resid


def compute(d: ds.Dataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (players_df, stints_df)."""
    g = d.game
    df = d.minute
    wp, resid = _wp_columns(df)
    ig = (df["game_min"] >= 0)
    ts = df.index[ig].to_numpy(dtype=float)
    wp_y = wp[ig].to_numpy(dtype=float)
    rs_y = resid[ig].to_numpy(dtype=float)
    ok = ~np.isnan(wp_y)
    ts, wp_y, rs_y = ts[ok], wp_y[ok], rs_y[ok]

    def at(arr, t):
        return float(np.interp(t, ts, arr))

    bounds = [g.first_ts] + sorted({s["ts"] for s in d.subs
                                    if g.first_ts < s["ts"] < g.last_ts}) + [g.last_ts]

    stars = set(g.stars[g.home_tri]) | set(g.stars[g.away_tri])
    acc: dict[str, dict] = {}
    stints = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b <= a:
            continue
        mins = (b - a) / 60.0
        d_home = at(wp_y, b) - at(wp_y, a)
        d_home_r = at(rs_y, b) - at(rs_y, a)
        mid = (a + b) / 2
        on = {tri: [] for tri in (g.home_tri, g.away_tri)}
        for player, iv in g.on_intervals.items():
            if espn.is_on(iv, mid):
                tri = g.player_team.get(player)
                if tri not in on:
                    continue
                on[tri].append(player)
                sign = 1 if tri == g.home_tri else -1
                a_ = acc.setdefault(player, {"team": tri, "min": 0.0, "wpa": 0.0, "rwpa": 0.0,
                                             "is_star": player in stars})
                a_["min"] += mins
                a_["wpa"] += sign * d_home
                a_["rwpa"] += sign * d_home_r
        stints.append({"start_ts": a, "end_ts": b, "minutes": round(mins, 2),
                       "d_home_wp": round(d_home, 4),
                       f"on_{g.home_tri}": len(on[g.home_tri]),
                       f"on_{g.away_tri}": len(on[g.away_tri])})

    rows = []
    for player, a_ in acc.items():
        gm = g.minutes.get(player, 0)  # boxscore (basketball) minutes
        denom = gm or np.nan
        rows.append({
            "player": player, "team": a_["team"], "is_star": a_["is_star"],
            "minutes": gm,
            "wall_min": round(a_["min"], 1),
            "wpa": round(a_["wpa"], 4),
            "wpa_per_min_pct": round(100 * a_["wpa"] / denom, 3),
            "resid_wpa": round(a_["rwpa"], 4),
            "resid_wpa_per_min_pct": round(100 * a_["rwpa"] / denom, 3),
        })
    players = pd.DataFrame(rows).sort_values(["team", "wpa_per_min_pct"], ascending=[True, False])
    return players, pd.DataFrame(stints)


def chart(players: pd.DataFrame, game_title: str, home: str, away: str, path: str) -> None:
    fig, ax = plt.subplots(1, 2, figsize=(14, 6.5))
    for i, tri in enumerate((home, away)):
        sub = players[players["team"] == tri].sort_values("wpa_per_min_pct")
        colors = ["tab:orange" if s else "tab:gray" for s in sub["is_star"]]
        ax[i].barh(sub["player"], sub["wpa_per_min_pct"], color=colors)
        ax[i].axvline(0, color="k", lw=.6)
        ax[i].set_title(f"{tri}")
        ax[i].set_xlabel("WPA per minute (% win prob / min on court)")
        for y, (v, m) in enumerate(zip(sub["wpa_per_min_pct"], sub["minutes"])):
            ax[i].text(v, y, f" {v:+.2f}  ({m:.0f}m)", va="center",
                       ha="left" if v >= 0 else "right", fontsize=7)
    fig.suptitle(f"{game_title}  —  Win-Probability Impact per minute on court\n"
                 "(orange = star; leverage-weighted: close/late minutes count most)", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, .94))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def write(d: ds.Dataset, outdir: str) -> dict:
    players, stints = compute(d)
    os.makedirs(outdir, exist_ok=True)
    players.to_csv(os.path.join(outdir, "player_wp_impact.csv"), index=False)
    stints.to_csv(os.path.join(outdir, "stints.csv"), index=False)
    g = d.game
    chart(players, f"{g.away_tri} @ {g.home_tri}  {g.date}", g.home_tri, g.away_tri,
          os.path.join(outdir, "5_player_wp_impact.png"))
    top = players.sort_values("wpa_per_min_pct", ascending=False)
    return {
        "n_stints": len(stints),
        "leaders": top.head(3)[["player", "team", "wpa_per_min_pct", "minutes"]].to_dict("records"),
        "laggards": top.tail(3)[["player", "team", "wpa_per_min_pct", "minutes"]].to_dict("records"),
    }
