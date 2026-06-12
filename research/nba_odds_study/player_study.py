"""Player-by-player market reaction to substitutions, pooled over a season.

For each player we pool every time they ENTER (sign +1) and every time they
EXIT (sign -1) and measure the subbing team's score-residual win-prob drift in
the next few minutes. The pooled mean is a *market-implied on/off*: positive =>
after this player's status changes the market subsequently moves in his team's
favor beyond the score (i.e. the market under-rated the change at the moment of
the sub -> potentially tradeable).

Caveats surfaced in the output:
  * A sub is a *swap* (X out, Y in), so a player's number blends him with his
    usual replacement; true isolation needs a regression (RAPM-style) — phase 2.
  * Testing hundreds of players inflates false positives, so p-values get a
    Benjamini-Hochberg FDR correction and an economic-size (vs cost) gate.
"""
from __future__ import annotations

import os

import matplotlib
import numpy as np
import pandas as pd
import scipy.stats as st

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _bh(pvals: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg q-values."""
    m = len(pvals)
    order = np.argsort(pvals)
    ranked = pvals[order] * m / (np.arange(m) + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    q = np.empty(m)
    q[order] = np.clip(ranked, 0, 1)
    return q


def compute(reactions: pd.DataFrame, min_n: int = 20, horizon: int = 3,
            cost_wp: float = 1.5, q_thresh: float = 0.10) -> pd.DataFrame:
    col = f"mlres_{horizon}"
    tcol = f"tot_{horizon}"
    players = set(reactions["player_in"].dropna()) | set(reactions["player_out"].dropna())
    rows = []
    for p in players:
        on = reactions.loc[reactions["player_in"] == p, col].dropna().to_numpy()
        off = reactions.loc[reactions["player_out"] == p, col].dropna().to_numpy()
        pooled = np.concatenate([on, -off])
        n = len(pooled)
        if n < min_n:
            continue
        mean = pooled.mean()
        sem = pooled.std(ddof=1) / np.sqrt(n)
        t = mean / sem if sem else np.nan
        pval = 2 * st.t.sf(abs(t), n - 1) if sem else np.nan
        team = reactions.loc[(reactions["player_in"] == p) | (reactions["player_out"] == p), "team"]
        tot_on = reactions.loc[reactions["player_in"] == p, tcol].dropna()
        rows.append({
            "player": p, "team": team.mode().iat[0] if len(team) else None,
            "n": n, "n_on": len(on), "n_off": len(off),
            "effect_pp": 100 * mean, "sem_pp": 100 * sem, "t": t, "p": pval,
            "on_pp": 100 * on.mean() if len(on) else np.nan,
            "off_pp": 100 * off.mean() if len(off) else np.nan,
            "tot_on_pts": tot_on.mean() if len(tot_on) else np.nan,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["q"] = np.nan
    ok = df["p"].notna()
    df.loc[ok, "q"] = _bh(df.loc[ok, "p"].to_numpy())
    df["significant"] = (df["q"] < q_thresh) & (df["effect_pp"].abs() > cost_wp)
    return df.sort_values("effect_pp", ascending=False).reset_index(drop=True)


def run(reactions: pd.DataFrame, outdir: str, min_n: int = 20, horizon: int = 3,
        cost_wp: float = 1.5, q_thresh: float = 0.10) -> dict:
    os.makedirs(outdir, exist_ok=True)
    df = compute(reactions, min_n, horizon, cost_wp, q_thresh)
    if df.empty:
        return {"n_players": 0, "note": f"no players with >= {min_n} sub events"}
    df.to_csv(os.path.join(outdir, "player_reaction.csv"), index=False)
    sig = df[df["significant"]]
    _chart_volcano(df, cost_wp, os.path.join(outdir, "8_player_volcano.png"))
    _chart_ranks(df, os.path.join(outdir, "9_player_ranks.png"))
    return {
        "n_players_tested": int(len(df)),
        "min_n": min_n, "horizon_min": horizon, "cost_wp": cost_wp, "q_thresh": q_thresh,
        "n_significant": int(len(sig)),
        "significant_players": sig[["player", "team", "n", "effect_pp", "t", "q"]].to_dict("records"),
        "top": df.head(8)[["player", "team", "n", "effect_pp", "t", "q"]].to_dict("records"),
        "bottom": df.tail(8)[["player", "team", "n", "effect_pp", "t", "q"]].to_dict("records"),
        "effect_pp_std_across_players": round(float(df["effect_pp"].std()), 3),
    }


def _chart_volcano(df, cost_wp, path):
    fig, ax = plt.subplots(figsize=(10, 7))
    y = -np.log10(df["p"].clip(lower=1e-6))
    sig = df["significant"]
    ax.scatter(df.loc[~sig, "effect_pp"], y[~sig], s=18, color="tab:gray", alpha=.6, label="ns")
    ax.scatter(df.loc[sig, "effect_pp"], y[sig], s=36, color="tab:red", label="BH q<0.1 & >cost")
    ax.axvline(cost_wp, color="r", ls="--", lw=.8)
    ax.axvline(-cost_wp, color="r", ls="--", lw=.8)
    ax.axvline(0, color="k", lw=.5)
    for _, r in pd.concat([df.head(6), df.tail(6), df[sig]]).drop_duplicates("player").iterrows():
        ax.annotate(r["player"], (r["effect_pp"], -np.log10(max(r["p"], 1e-6))),
                    fontsize=6, alpha=.8)
    ax.set_xlabel("market-implied on/off: residual win-prob drift @ +%d min (pp/event)" % 3)
    ax.set_ylabel("-log10(p)")
    ax.set_title("Per-player substitution edge (volcano)\n"
                 "red dotted = assumed cost; points past it AND BH-significant = candidate edges")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _chart_ranks(df, path):
    top = pd.concat([df.head(15), df.tail(15)]).drop_duplicates("player")
    fig, ax = plt.subplots(figsize=(9, 9))
    colors = ["tab:red" if s else "steelblue" for s in top["significant"]]
    ax.barh(top["player"], top["effect_pp"], xerr=top["sem_pp"], color=colors, capsize=2)
    ax.axvline(0, color="k", lw=.6)
    ax.set_xlabel("residual win-prob drift, on/off pooled (pp/event)")
    ax.set_title("Players the market reacts to most after subs (±SEM)\n"
                 "top = market under-rates when they change status; bottom = over-rates")
    for y, (v, n) in enumerate(zip(top["effect_pp"], top["n"])):
        ax.text(v, y, f" n={n}", va="center", ha="left" if v >= 0 else "right", fontsize=6)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
