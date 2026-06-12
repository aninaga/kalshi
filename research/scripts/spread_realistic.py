"""Realistic-execution re-score of the reported NBA spread pace-anchoring edge.

The reported spread edge (research/scripts/spread_alpha.py, SPREADS_FINDINGS.md)
BUYS an at-the-money spread/handicap contract priced ~0.50 and HOLDS TO
SETTLEMENT, scored under a FLAT cost sweep against a *fabricated* signed-home
strike interpolated to where P(home_margin > x) = 0.50 exactly. That
ATM-at-0.50 fill is the binding capital-risk assumption — the EXACT flaw that
killed the totals edge (see research/scripts/totals_realistic.py /
TOTALS_REFINE_FINDINGS.md). This module is the SPREAD analog of that
realistic-execution template:

  1. ACTUAL LISTED STRIKE (no fabricated interpolation). The strategy can only
     buy a *listed* signed-home spread strike. We snap the bet to the listed
     home strike nearest the implied home margin, and use that strike's REAL
     quoted P(home_margin > strike) as the fill mid. PnL settles vs THAT real
     strike, not the interpolated one. This is a measured cost, not an
     assumption.

  2. ENTRY HALF-SPREAD (modeled). The cached `prob` is a single mid/last per
     strike (no explicit book). Venue min tick = 1c; we cross a half-spread to
     take liquidity, central estimate 1.5c, swept 0/1/1.5/2/2.5c — identical to
     the totals template.

  3. POLYMARKET 2% FLAT TAKER FEE on entry notional (mock_execution.FeeModel /
     realistic_fills._polymarket_taker_fee: flat 0.02*price*size + tiny curve).
     Held to settlement -> ONE entry taker fee, NO exit fee.

  net pnl/contract = payoff(final home margin vs LISTED strike, bet side)
                     - (fill_mid + half_spread)        # what you pay to take
                     - pm_taker_fee(fill_mid + half_spread)

Direction (`continuation`), threshold, freshness guard, +1-bar latency, and the
gate are UNCHANGED. Test ids are EXCLUDED (population = non-test games). This is
a re-scoring of the reported edge, not a re-fit. Nothing new is pre-registered.

Usage::

    python3 -m research.scripts.spread_realistic --thresh 6 --half-spread 1.5
    python3 -m research.scripts.spread_realistic --sweep-spread
    python3 -m research.scripts.spread_realistic --half-spread 1.5 --walkforward
    python3 -m research.scripts.spread_realistic --decompose
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import venue_fees  # noqa: E402
from nba_odds_study import batch, schedule  # noqa: E402
from research.scorer.promotion_gate import evaluate_trial  # noqa: E402
from research.scripts.spread_alpha import (  # noqa: E402
    _kalshi_team,
    _implied_home_margin,
)

KINDS = {"spread"}

# Fees from venue_fees (canonical schedule, 2026-06-12). Default = the official
# Polymarket SPORTS taker fee (300 bps parabolic). --legacy-fees reproduces
# pre-2026-06-12 memos (flat 2% of notional + old curve; not a real fee).
PM_FLAT_TAKER_RATE = venue_fees.LEGACY_PM_FLAT_TAKER_RATE        # legacy repro only
PM_CURVE_FEE_RATE_BPS = venue_fees.LEGACY_PM_CURVE_FEE_RATE_BPS  # legacy repro only
_LEGACY_FEES = False


def _pm_taker_fee(price: float, size: float = 1.0) -> float:
    """Official PM sports taker fee (legacy flat-2%+curve under --legacy-fees)."""
    if _LEGACY_FEES:
        return venue_fees.legacy_pm_taker_fee(price, size)
    return venue_fees.pm_taker_fee(price, size, category="sports")


def _cached(game) -> bool:
    key = f"{game['date']}_{game['away']}_at_{game['home']}_spread_all.pkl"
    return os.path.exists(os.path.join(batch.CACHE_DIR, key))


def _home_strike_ladder(odds_long: pd.DataFrame, home_tri: str, away_tri: str, index) -> pd.DataFrame:
    """Per-minute ladder of P(final home margin > signed_home_strike).

    The spread analog of the totals per-strike P(over) pivot. Columns are SIGNED
    HOME STRIKES (a listed contract); values are the real quoted P(home margin >
    strike) at that minute, ffilled onto the minute index. Built from BOTH
    platforms' spread ladders using the same sign convention as
    spread_alpha._implied_home_margin.
    """
    sub = odds_long[(odds_long.kind == "spread") & odds_long.strike.notna()].copy()
    if sub.empty:
        return pd.DataFrame(index=index)
    sub["minute"] = (sub.ts // 60 * 60).astype(int)
    plat = sub.platform.to_numpy()
    keys = sub.key.to_numpy()
    strikes = sub.strike.to_numpy(float)
    probs = sub.prob.to_numpy(float)
    hs = np.full(len(sub), np.nan)
    ph = np.full(len(sub), np.nan)
    for i in range(len(sub)):
        if plat[i] == "kalshi":
            t = _kalshi_team(keys[i])
            if t == home_tri:        # "home wins by over N" -> (N, p)
                hs[i] = strikes[i]
                ph[i] = probs[i]
            elif t == away_tri:      # "away wins by over N" -> P(home > -N) = 1-p
                hs[i] = -strikes[i]
                ph[i] = 1.0 - probs[i]
        else:  # polymarket: "Spread: <home_nick> (S)" -> P(home covers S) = P(home margin > -S)
            hs[i] = -strikes[i]
            ph[i] = probs[i]
    sub = sub.assign(hs=hs, ph=ph).dropna(subset=["hs", "ph"])
    if sub.empty:
        return pd.DataFrame(index=index)
    piv = (sub.sort_values("ts").groupby(["minute", "hs"]).ph.last()
           .unstack("hs").reindex(index).ffill())
    return piv


def _load(game):
    """Returns df, imp(home-margin curve), final_margin, gid, g, ladder pivot."""
    if not _cached(game):
        return None
    try:
        d = batch.load_or_build(game, kinds=KINDS)
    except Exception:  # noqa: BLE001
        return None
    df = d.minute
    if not len(d.odds_long):
        return None
    g = d.game
    idx = list(df.index)
    imp = _implied_home_margin(d.odds_long, g.home_tri, g.away_tri, idx)
    if imp.notna().sum() < 5:
        return None
    mar = df["margin"]
    if mar.dropna().empty:
        return None
    final_margin = float(mar.dropna().iloc[-1])
    piv = _home_strike_ladder(d.odds_long, g.home_tri, g.away_tri, idx)
    if piv.shape[1] < 1:
        return None
    gid = f"{game['date']}_{g.away_tri}_at_{g.home_tri}"
    return df, imp, final_margin, gid, g, piv


def build_trades_realistic(games, *, side, thresh, entry_lat_min, min_elapsed,
                           max_elapsed, max_stale_min, half_spread, gap_cond=0.0,
                           max_entry_elapsed=None):
    """Realistic fills on the ACTUAL listed signed-home strike + half-spread + PM 2% fee."""
    hs = half_spread / 100.0
    rows = []
    for game in games:
        ld = _load(game)
        if ld is None:
            continue
        df, imp, final_margin, gid, g, piv = ld
        strikes = np.array(sorted(piv.columns), dtype=float)
        e = df["elapsed_game_sec"].to_numpy(float)
        mar = df["margin"].to_numpy(float)
        impv = imp.to_numpy(float)
        idxts = df.index.to_numpy(float)
        proj = np.where(e > 120, mar * 2880.0 / e, np.nan)
        chg = np.r_[True, np.abs(np.diff(impv)) > 1e-9]
        last_chg = np.maximum.accumulate(np.where(chg, np.arange(len(impv)), -1))
        stale_min = np.arange(len(impv)) - last_chg
        ok = np.isfinite(e) & np.isfinite(impv) & np.isfinite(proj)
        order = np.argsort(idxts)
        mee = max_entry_elapsed if max_entry_elapsed is not None else max_elapsed
        for k in order:
            if not ok[k] or e[k] < min_elapsed or e[k] > max_elapsed:
                continue
            if e[k] > mee:
                continue
            if stale_min[k] > max_stale_min:
                continue
            sig = proj[k] - impv[k]
            if abs(sig) < thresh or abs(sig) < gap_cond:
                continue
            proj_home = sig > 0
            bet_home = proj_home if side == "continuation" else (not proj_home)

            # honest +1-bar latency: fill on the NEXT bar's listed ladder.
            t_entry = idxts[k] + entry_lat_min * 60
            jj = int(np.searchsorted(idxts, t_entry))
            if jj >= len(idxts):
                jj = len(idxts) - 1
            # implied home margin at entry, to pick the nearest LISTED strike.
            imp_entry = float(np.interp(t_entry, idxts[ok], impv[ok]))
            if not np.isfinite(imp_entry):
                continue
            si = int(np.argmin(np.abs(strikes - imp_entry)))
            strike = float(strikes[si])
            # real quoted P(home margin > strike) of that listed strike at entry bar.
            p_home = piv.iloc[jj, si]
            if not np.isfinite(p_home):
                col = piv.iloc[: jj + 1, si].dropna()
                if col.empty:
                    continue
                p_home = float(col.iloc[-1])
            p_home = float(min(max(p_home, 0.01), 0.99))

            # Fill price you PAY to take = contract mid + half-spread.
            mid = p_home if bet_home else (1.0 - p_home)
            fill = min(mid + hs, 0.999)
            fee = _pm_taker_fee(fill)
            payoff = 1.0 if ((final_margin > strike) == bet_home) else 0.0
            pnl = payoff - fill - fee
            rows.append({"game_id": gid, "date": game["date"], "home_team": g.home_tri,
                         "primary_team": g.home_tri if bet_home else g.away_tri,
                         "bet": "home" if bet_home else "away", "strike": round(strike, 1),
                         "mid": round(mid, 4), "payoff": payoff, "gap": abs(sig),
                         "fill": round(fill, 4), "fee": round(fee, 4),
                         "final": final_margin, "pnl_prob": pnl})
            break
    return pd.DataFrame(rows)


def reprice(base: pd.DataFrame, half_spread: float) -> pd.DataFrame:
    """Re-derive PnL for a new half-spread from a base frame (loaded at hs=0)."""
    hs = half_spread / 100.0
    out = base.copy()
    fill = np.minimum(out["mid"].to_numpy(float) + hs, 0.999)
    fee = np.array([_pm_taker_fee(f) for f in fill])
    out["fill"] = np.round(fill, 4)
    out["fee"] = np.round(fee, 4)
    out["pnl_prob"] = out["payoff"].to_numpy(float) - fill - fee
    return out


def evaluate(trades, extra_cost_c=0.0, n_trials=1):
    if trades.empty or len(trades) < 2:
        return {"n": len(trades), "cents": float("nan"), "gate": False,
                "ci_lo": float("nan"), "ci_hi": float("nan"), "ngames": 0, "reasons": ["no trades"]}
    pnl = trades["pnl_prob"].to_numpy(float) - extra_cost_c / 100.0
    dec = evaluate_trial(
        val_pnl_per_trade=pnl,
        val_game_id_per_trade=trades["game_id"].to_numpy(),
        val_date_per_trade=trades["date"].to_numpy(),
        val_home_team_per_trade=trades["home_team"].to_numpy(),
        val_primary_team_per_trade=trades["primary_team"].to_numpy(),
        n_total_trials_in_registry=n_trials,
        cost_per_trade_assumed=extra_cost_c / 100.0,
    )
    return {"n": int(len(pnl)), "cents": float(pnl.mean() * 100), "gate": bool(dec.passed),
            "ci_lo": float(dec.block_bootstrap_ci_lo * 100), "ci_hi": float(dec.block_bootstrap_ci_hi * 100),
            "ngames": int(dec.n_games_val), "reasons": dec.reasons}


def _breakeven(trades):
    if trades.empty:
        return float("nan")
    return float(trades["pnl_prob"].mean() * 100)


def _walkforward(trades):
    tr = trades.copy()
    tr["month"] = pd.to_datetime(tr["date"]).dt.to_period("M").astype(str)
    months = sorted(tr["month"].unique())
    pos = 0
    print(f"  {'month':<9}{'n':>5}{'win%':>7}{'c/ct':>9}{'CIlo':>8}")
    for m in months:
        sub = tr[tr["month"] == m]
        z = evaluate(sub, 0.0)
        win = (sub["pnl_prob"] > 0).mean()
        if np.isfinite(z["cents"]) and z["cents"] > 0:
            pos += 1
        print(f"  {m:<9}{len(sub):>5}{win*100:>6.0f}%{z['cents']:>9.2f}{z['ci_lo']:>8.2f}", flush=True)
    print(f"  months net-positive (realistic): {pos}/{len(months)}", flush=True)
    return pos, len(months)


def _splits():
    sp = json.loads((_ROOT / "market_data" / "splits.json").read_text())
    return set(sp["train_game_ids"]), set(sp.get("val_game_ids", [])), set(sp.get("test_game_ids", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-10-21")
    ap.add_argument("--end", default="2026-06-08")
    ap.add_argument("--thresh", type=float, default=6.0)
    ap.add_argument("--entry-lat-min", type=float, default=1.0)
    ap.add_argument("--min-elapsed", type=float, default=600.0)
    ap.add_argument("--max-elapsed", type=float, default=2520.0)
    ap.add_argument("--max-stale-min", type=float, default=2.0)
    ap.add_argument("--half-spread", type=float, default=1.5, help="cents per side")
    ap.add_argument("--sweep-spread", action="store_true")
    ap.add_argument("--gap-cond", type=float, default=0.0)
    ap.add_argument("--max-entry-elapsed", type=float, default=None)
    ap.add_argument("--walkforward", action="store_true")
    ap.add_argument("--decompose", action="store_true")
    ap.add_argument("--legacy-fees", action="store_true",
                    help="reproduce pre-2026-06-12 memos (flat 2%% + old curve)")
    a = ap.parse_args()

    global _LEGACY_FEES
    _LEGACY_FEES = a.legacy_fees
    fee_desc = "LEGACY flat-2%+curve" if a.legacy_fees else "official PM sports 300bps"

    games = schedule.completed_games(a.start, a.end)
    train_ids, val_ids, test_ids = _splits()

    def gid_of(gm):
        return f"{gm['date']}_{gm['away']}_at_{gm['home']}"
    # HARD RULE: never use the test ids. Population = all NON-test games.
    games = [g for g in games if gid_of(g) not in test_ids]
    print(f"{len(games)} non-test games; continuation (pre-registered), thresh={a.thresh}, "
          f"REALISTIC execution (listed strike + half-spread + {fee_desc} fee), "
          f"gap_cond={a.gap_cond}, max_entry={a.max_entry_elapsed}\n", flush=True)

    # Build ONCE (slow I/O) at half_spread=0; reprice analytically for sweeps.
    base = build_trades_realistic(games, side="continuation", thresh=a.thresh,
                                  entry_lat_min=a.entry_lat_min, min_elapsed=a.min_elapsed,
                                  max_elapsed=a.max_elapsed, max_stale_min=a.max_stale_min,
                                  half_spread=0.0, gap_cond=a.gap_cond,
                                  max_entry_elapsed=a.max_entry_elapsed)
    if base.empty:
        print("no trades (data not built?)"); return

    if a.decompose:
        payoff = base["payoff"].to_numpy(float)
        mid = base["mid"].to_numpy(float)
        g0 = (payoff - 0.50).mean() * 100          # flat-model gross (reproduces report)
        avg_mid = mid.mean()
        g_mid = (payoff - mid).mean() * 100        # true tradeable gross (no spread/fee)
        fee0 = np.array([_pm_taker_fee(m) for m in mid])
        g_fee = (payoff - mid - fee0).mean() * 100  # realistic, 0 spread
        tr15 = reprice(base, 1.5)
        g15 = tr15["pnl_prob"].mean() * 100
        win = payoff.mean()
        print("=== DECOMPOSITION (full non-test population) ===")
        print(f"  n trades                              : {len(base)}")
        print(f"  win rate (payoff)                     : {win:.4f}")
        print(f"  payoff - 0.50 (flat-model gross)      : {g0:+.2f}c   (reproduces reported artifact)")
        print(f"  avg real listed fill mid              : {avg_mid:.4f}  (NOT 0.50)")
        print(f"  payoff - mid (true tradeable gross)   : {g_mid:+.2f}c")
        avg_fee = fee0.mean() * 100
        print(f"  Polymarket 2% taker fee (avg)         : -{avg_fee:.2f}c")
        print(f"  payoff - mid - fee (realistic, 0sprd) : {g_fee:+.2f}c")
        print(f"  - 1.5c half-spread (central)          : {g15:+.2f}c   <- REALISTIC NET")
        return

    if a.sweep_spread:
        print("=== HALF-SPREAD SENSITIVITY (full non-test population, direction pre-registered) ===")
        for hsv in (0.0, 1.0, 1.5, 2.0, 2.5):
            tr = reprice(base, hsv)
            z = evaluate(tr, 0.0)
            flag = "  <-- gate PASS" if z["gate"] else ""
            print(f"  half_spread={hsv:.1f}c: n={z['n']:>4} games={z['ngames']:>4} "
                  f"net c/ct={z['cents']:+.2f} CI[{z['ci_lo']:+.2f},{z['ci_hi']:+.2f}] "
                  f"gate={z['gate']}{flag}", flush=True)
        return

    tr = reprice(base, a.half_spread)
    avg_fill = tr["fill"].mean()
    avg_fee = tr["fee"].mean()
    avg_mid = tr["mid"].mean()
    print(f"avg mid={avg_mid:.4f}  avg fill price={avg_fill:.4f}  avg PM fee={avg_fee*100:.2f}c  "
          f"half_spread={a.half_spread:.1f}c\n", flush=True)

    print("=== FULL POPULATION, REALISTIC EXECUTION (breakeven probe = extra flat cost) ===")
    be = _breakeven(tr)
    print(f"  realistic net c/ct (extra cost=0): {be:+.2f}")
    print(f"  breakeven: edge survives ~{be:+.2f}c of ADDITIONAL cost on top of realistic fill\n")
    for c in (0.0, 1.0, 2.0):
        z = evaluate(tr, c)
        flag = "  <-- gate PASS" if z["gate"] else ""
        print(f"  +extra {c:.0f}c: n={z['n']:>4} games={z['ngames']:>4} net c/ct={z['cents']:+.2f} "
              f"CI[{z['ci_lo']:+.2f},{z['ci_hi']:+.2f}] gate={z['gate']}{flag}", flush=True)
    z0 = evaluate(tr, 0.0)
    if z0["reasons"]:
        print(f"\n  gate reasons (realistic, +0c):", "; ".join(z0["reasons"][:5]), flush=True)

    if a.walkforward:
        print(f"\n=== MONTHLY WALK-FORWARD (realistic execution) ===")
        _walkforward(tr)


if __name__ == "__main__":
    main()
