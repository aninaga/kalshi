"""Analyses + charts relating NBA score/lineup changes to market odds.

All win-probability series are from the home team's perspective. To separate a
lineup effect from the obvious score effect, several analyses use a *residual*
win probability: home win prob minus the part explained by the current margin.
"""
from __future__ import annotations

import json
import os

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from . import dataset as ds  # noqa: E402
from . import wp_impact  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _blended_home_wp(df: pd.DataFrame) -> pd.Series:
    cols = [c for c in ("kalshi_home_winprob", "pm_home_winprob") if c in df]
    return df[cols].mean(axis=1)


def _residualize(wp: pd.Series, margin: pd.Series) -> pd.Series:
    m = wp.notna() & margin.notna()
    if m.sum() < 5:
        return wp * np.nan
    b1, b0 = np.polyfit(margin[m], wp[m], 1)
    return wp - (b0 + b1 * margin)


def _ts_to_gmin(ts: int, tip_min: int) -> float:
    return (int(ts) // 60 * 60 - tip_min) / 60.0


def _event_matrix(series: pd.Series, events: list[dict], pre: int, post: int) -> pd.DataFrame | None:
    """Rows = events, columns = minute offsets, values = sign*(value - value@0)."""
    rows = []
    for ev in events:
        em = int(ev["ts"]) // 60 * 60
        base = series.get(em, np.nan)
        if pd.isna(base):
            continue
        sign = ev.get("sign", 1)
        rows.append({k: sign * (series.get(em + k * 60, np.nan) - base) for k in range(-pre, post + 1)})
    if not rows:
        return None
    return pd.DataFrame(rows)


def _implied_total(odds_long: pd.DataFrame, platform: str, index) -> pd.Series:
    """Strike where P(over)=0.5, interpolated across the total ladder each minute."""
    sub = odds_long[(odds_long.platform == platform) & (odds_long.kind == "total")
                    & odds_long.strike.notna()].copy()
    if sub.empty:
        return pd.Series(np.nan, index=index, dtype=float)
    sub["minute"] = sub.ts // 60 * 60
    piv = sub.sort_values("ts").groupby(["minute", "strike"]).prob.last().unstack("strike")
    piv = piv.reindex(index).ffill()
    strikes = np.array(sorted(piv.columns))
    out = []
    for _, row in piv.iterrows():
        y = row[strikes].to_numpy(dtype=float)
        ok = ~np.isnan(y)
        if ok.sum() < 2:
            out.append(np.nan)
            continue
        xs, ys = strikes[ok], y[ok]
        order = np.argsort(ys)  # interp needs increasing x (=prob here)
        out.append(float(np.interp(0.5, ys[order], xs[order])))
    return pd.Series(out, index=index)


def _period_starts(game, tip_min):
    seen, marks = set(), []
    for p in game.plays:
        per = p["period"]
        if per and per not in seen:
            seen.add(per)
            marks.append((per, _ts_to_gmin(p["ts"], tip_min)))
    return marks


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def run(d: ds.Dataset, outdir: str) -> dict:
    os.makedirs(outdir, exist_ok=True)
    g = d.game
    df = d.minute.copy()
    df["home_wp"] = _blended_home_wp(df)
    df["home_wp_resid"] = _residualize(df["home_wp"], df["margin"])
    ig = ds.in_game(df)
    tip_min = int(d.minute.index[(d.minute["game_min"] == 0).argmax()])
    home, away = g.home_tri, g.away_tri
    title = f"{away} @ {home}  {g.date}"

    summary: dict = {"game": title, "event_id": g.event_id}
    fp = g.plays[-1]
    winner = home if fp["home_score"] > fp["away_score"] else away
    summary["final_score"] = {home: fp["home_score"], away: fp["away_score"], "winner": winner}
    pre = df[df["game_min"] < 0]
    summary["pregame_home_winprob"] = {
        "kalshi": round(float(pre["kalshi_home_winprob"].dropna().iloc[-1]), 3) if pre["kalshi_home_winprob"].notna().any() else None,
        "polymarket": round(float(pre["pm_home_winprob"].dropna().iloc[-1]), 3) if pre["pm_home_winprob"].notna().any() else None,
    }

    _overview(d, df, ig, tip_min, title, outdir)
    summary["score_sensitivity"] = _sensitivity(ig, title, outdir, home)
    summary["events"] = _event_studies(d, df, title, outdir)
    summary["cross_platform"] = _cross_platform(ig, title, outdir)
    summary["totals"] = _totals(d, df, ig, tip_min, title, outdir, fp)
    summary["player_wp_impact"] = wp_impact.write(d, outdir)

    df.to_csv(os.path.join(outdir, "minute.csv"))
    d.odds_long.to_csv(os.path.join(outdir, "odds_long.csv"), index=False)
    with open(os.path.join(outdir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, default=str)
    return summary


def _overview(d, df, ig, tip_min, title, outdir):
    g = d.game
    fig, ax = plt.subplots(3, 1, figsize=(13, 11), sharex=True)
    x = ig["game_min"]

    ax[0].fill_between(x, ig["margin"], 0, where=ig["margin"] >= 0, color="tab:blue", alpha=.35, label=f"{g.home_tri} leads")
    ax[0].fill_between(x, ig["margin"], 0, where=ig["margin"] < 0, color="tab:red", alpha=.35, label=f"{g.away_tri} leads")
    ax[0].axhline(0, color="k", lw=.6)
    for t in d.lead_changes:
        ax[0].axvline(_ts_to_gmin(t, tip_min), color="purple", lw=.7, ls=":", alpha=.5)
    for s in d.subs:
        if s["is_star"]:
            ax[0].plot(_ts_to_gmin(s["ts"], tip_min), 0, "v", color="darkorange", ms=5, alpha=.7)
    ax[0].set_ylabel(f"Score margin\n(+{g.home_tri} / -{g.away_tri})")
    ax[0].set_title(f"{title}  —  score, odds & totals over wall-clock game time\n"
                    "purple dotted = lead change   orange ▽ = star substitution", fontsize=11)
    ax[0].legend(loc="upper left", fontsize=8)

    ax[1].plot(x, ig["kalshi_home_winprob"], label="Kalshi", color="tab:green", lw=1.6)
    ax[1].plot(x, ig["pm_home_winprob"], label="Polymarket", color="tab:blue", lw=1.6)
    if "espn_home_winprob" in ig:
        ax[1].plot(x, ig["espn_home_winprob"], label="ESPN model", color="gray", lw=1.0, ls="--")
    ax[1].axhline(.5, color="k", lw=.5, ls=":")
    ax[1].set_ylabel(f"{g.home_tri} win probability")
    ax[1].set_ylim(0, 1)
    ax[1].legend(loc="upper left", fontsize=8)

    ax[2].plot(x, ig["total"], label="actual total", color="k", lw=1.6)
    ax[2].plot(x, ig["proj_total"], label="pace projection", color="tab:gray", ls="--", lw=1.1)
    for plat, c in (("kalshi", "tab:green"), ("polymarket", "tab:blue")):
        it = _implied_total(d.odds_long, plat, list(df.index)).reindex(ig.index)
        if it.notna().any():
            ax[2].plot(x, it, label=f"{plat} implied line", color=c, lw=1.1, alpha=.8)
    ax[2].set_ylabel("Total points")
    ax[2].set_xlabel("game minutes since tip-off (wall clock)")
    ax[2].legend(loc="upper left", fontsize=8)

    for a in ax:
        for per, gm in _period_starts(g, tip_min):
            a.axvline(gm, color="k", lw=.4, alpha=.25)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "1_overview.png"), dpi=130)
    plt.close(fig)


def _sensitivity(ig, title, outdir, home):
    m = ig["margin"]
    wp = ig["home_wp"]
    ok = m.notna() & wp.notna()
    b1, b0 = np.polyfit(m[ok], wp[ok], 1)
    first = ig[ig["game_min"] <= ig["game_min"].max() / 2]
    second = ig[ig["game_min"] > ig["game_min"].max() / 2]
    res = {"prob_per_point_overall": round(float(b1), 4)}
    for name, seg in (("first_half", first), ("second_half", second)):
        o = seg["margin"].notna() & seg["home_wp"].notna()
        if o.sum() > 5:
            res[f"prob_per_point_{name}"] = round(float(np.polyfit(seg["margin"][o], seg["home_wp"][o], 1)[0]), 4)

    fig, axx = plt.subplots(figsize=(8, 6))
    sc = axx.scatter(m[ok], wp[ok], c=ig["game_min"][ok], cmap="viridis", s=22)
    xs = np.linspace(m[ok].min(), m[ok].max(), 50)
    axx.plot(xs, b0 + b1 * xs, "r-", lw=2, label=f"slope = {b1:.4f}/pt")
    axx.axhline(.5, color="k", lw=.4, ls=":")
    axx.set_xlabel(f"score margin (+{home})")
    axx.set_ylabel(f"{home} win probability (Kalshi/PM avg)")
    axx.set_title(f"{title}\nodds sensitivity to score margin")
    plt.colorbar(sc, label="game minute")
    axx.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "2_sensitivity.png"), dpi=130)
    plt.close(fig)
    return res


def _event_studies(d, df, title, outdir):
    g = d.game
    home = g.home_tri
    stars = set(g.stars[home]) | set(g.stars[g.away_tri])
    resid = df["home_wp_resid"]
    wp = df["home_wp"]
    pre, post = 3, 4
    out = {}

    def own_sign(team):
        return 1 if team == home else -1

    run_ev = [{"ts": r["end_ts"], "sign": own_sign(r["team"])} for r in d.runs]
    lc_ev = [{"ts": t, "sign": 1} for t in d.lead_changes]
    off_ev = [{"ts": s["ts"], "sign": own_sign(s["team"])}
              for s in d.subs if s["player_out"] in stars]
    on_ev = [{"ts": s["ts"], "sign": own_sign(s["team"])}
             for s in d.subs if s["player_in"] in stars]

    panels = [
        ("scoring runs (>=8-0)\nrunning team win prob", _event_matrix(wp, run_ev, pre, post), len(run_ev)),
        ("lead changes\nnew-leader win prob", _event_matrix(wp, lc_ev, pre, post), len(lc_ev)),
        ("star OFF court\nown-team residual wp", _event_matrix(resid, off_ev, pre, post), len(off_ev)),
        ("star ON court\nown-team residual wp", _event_matrix(resid, on_ev, pre, post), len(on_ev)),
    ]
    fig, ax = plt.subplots(1, 4, figsize=(17, 4.3), sharey=False)
    for i, (lbl, M, n) in enumerate(panels):
        if M is None or M.empty:
            ax[i].set_title(f"{lbl}\n(no events)", fontsize=9)
            continue
        offs = list(M.columns)
        mean = M.mean()
        sem = M.std() / max(np.sqrt(len(M)), 1)
        ax[i].axhline(0, color="k", lw=.5)
        ax[i].axvline(0, color="purple", lw=.6, ls=":")
        ax[i].plot(offs, mean.values, "-o", ms=3, color="tab:blue")
        ax[i].fill_between(offs, (mean - sem).values, (mean + sem).values, color="tab:blue", alpha=.2)
        ax[i].set_title(f"{lbl}  (n={n})", fontsize=9)
        ax[i].set_xlabel("minutes from event")
        key = lbl.split("\n")[0]
        out[key] = {"n": n, "mean_change_+{}min".format(post): round(float(mean.get(post, np.nan)), 4)}
    ax[0].set_ylabel("Δ win prob (rebased at event)")
    fig.suptitle(f"{title}  —  event studies (mean ± SEM)", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, .95))
    fig.savefig(os.path.join(outdir, "3_event_studies.png"), dpi=130)
    plt.close(fig)
    return out


def _cross_platform(ig, title, outdir):
    k, p = ig["kalshi_home_winprob"], ig["pm_home_winprob"]
    res = {}
    if k.notna().any() and p.notna().any():
        res["mean_abs_divergence"] = round(float((k - p).abs().mean()), 4)
        res["max_abs_divergence"] = round(float((k - p).abs().max()), 4)
        dk, dp = k.diff(), p.diff()
        lags = range(-5, 6)
        corrs = {lag: dk.corr(dp.shift(-lag)) for lag in lags}
        corrs = {l: c for l, c in corrs.items() if pd.notna(c)}
        if corrs:
            best = max(corrs, key=corrs.get)
            res["contemporaneous_corr"] = round(float(corrs.get(0, np.nan)), 3)
            res["best_lag_min"] = best
            res["best_lag_corr"] = round(float(corrs[best]), 3)
            res["lead_lag_note"] = ("positive lag => Polymarket moves first; "
                                    "resolution limited to 1 min")

    fig, ax = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    x = ig["game_min"]
    ax[0].plot(x, k, label="Kalshi", color="tab:green")
    ax[0].plot(x, p, label="Polymarket", color="tab:blue")
    ax[0].set_ylabel("home win prob")
    ax[0].legend(fontsize=8)
    ax[0].set_title(f"{title}\nKalshi vs Polymarket home win probability")
    ax[1].fill_between(x, (k - p), 0, color="tab:red", alpha=.4)
    ax[1].axhline(0, color="k", lw=.5)
    ax[1].set_ylabel("Kalshi − PM")
    ax[1].set_xlabel("game minutes since tip-off")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "4_cross_platform.png"), dpi=130)
    plt.close(fig)
    return res


def _totals(d, df, ig, tip_min, title, outdir, fp):
    final_total = fp["home_score"] + fp["away_score"]
    res = {"final_total": final_total}
    for plat in ("kalshi", "polymarket"):
        it = _implied_total(d.odds_long, plat, list(df.index)).reindex(ig.index)
        if it.notna().any():
            res[f"{plat}_pregame_line"] = round(float(it.dropna().iloc[0]), 1)
            res[f"{plat}_final_line"] = round(float(it.dropna().iloc[-1]), 1)
    return res
