"""Pooled substitution -> odds-reaction analysis: is there a tradeable edge?

For each sub *type* we test, in the minutes after the sub, whether the market
keeps drifting in the fair-value direction (under-reaction -> follow) or snaps
back (over-reaction -> fade), using score-residual win prob so the signal isn't
just the scoreboard. Drift is compared against an assumed round-trip cost.
"""
from __future__ import annotations

import os

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OFFSETS = [-2, -1, 0, 1, 2, 3]
# fair-value direction for the subbing team's own win prob / the game total
EXPECTED = {"star_out": -1, "starter_out": -1, "star_in": 1, "starter_in": 1}


def _directional(df: pd.DataFrame, prefix: str, k: int, scale: float) -> pd.DataFrame:
    out = []
    for typ, sign in EXPECTED.items():
        v = df[df["type"] == typ][f"{prefix}_{k}"].dropna()
        if len(v) < 5:
            continue
        mean, sem = v.mean(), v.std() / np.sqrt(len(v))
        out.append({
            "type": typ, "n": len(v),
            "mean": scale * mean, "sem": scale * sem,
            "t": mean / sem if sem else np.nan,
            "signed": scale * mean * sign,           # >0 => under-reaction (follow)
            "hit_rate": float((np.sign(v) == sign).mean()),
        })
    return pd.DataFrame(out)


def _traj(df: pd.DataFrame, typ: str, prefix: str, scale: float):
    sub = df[df["type"] == typ]
    if len(sub) < 5:
        return None
    mean = [scale * sub[f"{prefix}_{k}"].mean() for k in OFFSETS]
    sem = [scale * sub[f"{prefix}_{k}"].std() / np.sqrt(sub[f"{prefix}_{k}"].notna().sum()) for k in OFFSETS]
    return np.array(mean), np.array(sem), len(sub)


def run(reactions: pd.DataFrame, outdir: str, cost_wp: float = 1.5, cost_tot: float = 1.0) -> dict:
    os.makedirs(outdir, exist_ok=True)
    summary = {"n_subs": len(reactions), "n_games": reactions["game"].nunique(),
               "by_type": reactions["type"].value_counts().to_dict(),
               "cost_assumption": {"ml_pp": cost_wp, "total_pts": cost_tot}}

    ml3 = _directional(reactions, "mlres", 3, 100.0)
    ml2 = _directional(reactions, "mlres", 2, 100.0)
    tot3 = _directional(reactions, "tot", 3, 1.0)
    summary["ml_residual_drift_+3min_pp"] = ml3.to_dict("records")
    summary["ml_residual_drift_+2min_pp"] = ml2.to_dict("records")
    summary["total_line_drift_+3min_pts"] = tot3.to_dict("records")

    verdict = {}
    for _, r in ml3.iterrows():
        if r["t"] >= 2 and r["signed"] >= cost_wp:
            v = f"UNDER-reacts: follow ({r['signed']:.2f}pp > {cost_wp}pp, t={r['t']:.1f})"
        elif r["t"] <= -2 and r["signed"] <= -cost_wp:
            v = f"OVER-reacts: fade ({r['signed']:.2f}pp, t={r['t']:.1f})"
        else:
            v = f"no edge ({r['signed']:+.2f}pp vs {cost_wp}pp cost, t={r['t']:.1f})"
        verdict[r["type"]] = v
    summary["verdict_moneyline"] = verdict

    _chart_traj(reactions, outdir)
    _chart_edge(ml3, tot3, cost_wp, cost_tot, outdir)
    reactions.to_csv(os.path.join(outdir, "sub_reactions.csv"), index=False)
    pd.concat([ml3.assign(metric="ml_res_+3"), ml2.assign(metric="ml_res_+2"),
               tot3.assign(metric="tot_+3")]).to_csv(
        os.path.join(outdir, "sub_reaction_summary.csv"), index=False)
    return summary


def _chart_traj(df, outdir):
    fig, ax = plt.subplots(1, 3, figsize=(16, 4.6))
    specs = [("mlres", 100.0, "residual win prob (pp)"),
             ("mlraw", 100.0, "raw win prob (pp)"),
             ("tot", 1.0, "implied total line (pts)")]
    for j, (prefix, scale, ylab) in enumerate(specs):
        for typ, color in (("star_out", "tab:red"), ("star_in", "tab:green"),
                           ("starter_out", "tab:orange"), ("starter_in", "tab:blue")):
            tr = _traj(df, typ, prefix, scale)
            if tr is None:
                continue
            mean, sem, n = tr
            ax[j].plot(OFFSETS, mean, "-o", ms=3, color=color, label=f"{typ} (n={n})")
            ax[j].fill_between(OFFSETS, mean - sem, mean + sem, color=color, alpha=.12)
        ax[j].axhline(0, color="k", lw=.5)
        ax[j].axvline(0, color="purple", lw=.6, ls=":")
        ax[j].set_xlabel("minutes from substitution")
        ax[j].set_ylabel(ylab)
        ax[j].legend(fontsize=7)
    ax[0].set_title("own-team residual WP (score removed)")
    ax[1].set_title("own-team raw WP")
    ax[2].set_title("game total line")
    fig.suptitle("Mean market reaction after a substitution, by type (±SEM)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, .94))
    fig.savefig(os.path.join(outdir, "6_sub_reaction_trajectories.png"), dpi=130)
    plt.close(fig)


def _chart_edge(ml3, tot3, cost_wp, cost_tot, outdir):
    fig, ax = plt.subplots(1, 2, figsize=(13, 5))
    if not ml3.empty:
        ax[0].bar(ml3["type"], ml3["signed"], yerr=ml3["sem"], capsize=4, color="tab:blue")
        ax[0].axhline(cost_wp, color="r", ls="--", lw=1, label=f"+cost ({cost_wp}pp)")
        ax[0].axhline(-cost_wp, color="r", ls="--", lw=1)
    ax[0].axhline(0, color="k", lw=.6)
    ax[0].set_title("Signed residual win-prob drift @ +3 min\n(>cost = follow, <-cost = fade)")
    ax[0].set_ylabel("signed drift (pp)")
    ax[0].legend(fontsize=8)
    if not tot3.empty:
        ax[1].bar(tot3["type"], tot3["signed"], yerr=tot3["sem"], capsize=4, color="tab:green")
        ax[1].axhline(cost_tot, color="r", ls="--", lw=1, label=f"+cost ({cost_tot}pt)")
        ax[1].axhline(-cost_tot, color="r", ls="--", lw=1)
    ax[1].axhline(0, color="k", lw=.6)
    ax[1].set_title("Signed total-line drift @ +3 min")
    ax[1].set_ylabel("signed drift (points)")
    ax[1].legend(fontsize=8)
    for a in ax:
        a.tick_params(axis="x", rotation=20)
    fig.suptitle("Substitution edge by type (positive = market under-reacted)", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, .93))
    fig.savefig(os.path.join(outdir, "7_sub_edge_by_type.png"), dpi=130)
    plt.close(fig)
