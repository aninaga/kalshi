"""Maker / limit-fill execution study for the NBA spread anchoring residual.

DIAGNOSTIC_2's top scoped recommendation: test whether MAKER (passive limit)
fills, instead of crossing the spread as a TAKER, rescue the spread anchoring
residual that nets only ~+1.9c/ct and FAILS the gate as a taker
(SPREADS_FINDINGS.md "REALISTIC EXECUTION RE-CHECK").

THE HONESTY PROBLEM (the whole point of this study)
---------------------------------------------------
A maker fill is NOT free money. Assuming you always get filled at the posted
price is EXACTLY the optimistic-fill fantasy that made the certified totals edge
evaporate (+6.21c -> -1.00c). A resting BUY limit at price q only fills when a
seller is willing to trade at q or below -- i.e. precisely when the market is
moving DOWN through your price. That is adverse selection: conditional on a
fill, the subsequent move (and the eventual settlement) tends AGAINST you. So a
maker model that is to be believed MUST jointly model:

  (1) FILL PROBABILITY -- you do not always get filled. A passive limit posted
      `edge_c` cents better than the current quote fills only if the contract's
      quoted price subsequently trades through your limit within a waiting
      window. We READ that from the SAME contract's real per-minute quote path
      (the listed strike's ladder series), never assume it.

  (2) ADVERSE SELECTION -- conditional on a fill, the realized outcome is worse
      than for the unconditional population, because you were picked off by the
      flow that moved the price through your limit. We do NOT assume a number:
      we settle each MADE fill against the SAME real final-margin outcome and
      let the conditional win rate fall out of the data. If the population that
      fills as a maker is adversely selected, its realized edge will be lower
      than the taker population's -- automatically.

MODEL (per game, one trade attempt, same signal as the taker re-check)
----------------------------------------------------------------------
Same signal/population as research.scripts.spread_realistic (continuation
anchoring, thresh, freshness guard, honest +1-bar latency, listed-strike snap):

  * The signal fires at bar k; honest entry begins at bar j (k + latency).
  * We want to BUY the chosen side (home/away cover of the snapped listed
    strike). The current contract mid for our side at j is `mid_j`.
  * As a MAKER we post a passive BUY limit at `limit = mid_j - edge_c` (we try
    to BUY `edge_c` cents CHEAPER than the current quote -- we want to earn,
    not pay, liquidity). Equivalently we want the contract to tick down to our
    price.
  * FILL RULE (read from data): walk forward from j up to `wait_min` minutes;
    the order FILLS at the first bar t where the contract's real quoted price
    for our side <= `limit`. If no such bar within the window, the order is
    CANCELLED (no trade -- this game contributes NOTHING, which is the honest
    cost of being a maker: you forgo the trades that ran away from you, and
    those are disproportionately the WINNERS).
  * On a fill: entry_price = limit (you got your price). NO half-spread (you
    earned it). MAKER FEE applies (Polymarket maker fee is 0% currently; Kalshi
    maker fee modeled). Held to settlement vs the SAME listed strike.

The adverse selection is therefore ENDOGENOUS and measured: a winner that
gapped up never ticks down to our buy limit, so we never fill it; a loser that
drifts down fills us. The maker population is whatever the real quote paths
select -- exactly the picked-off cohort.

  net pnl/contract (maker, filled) = payoff(final vs strike, side)
                                     - limit_price            # what you paid
                                     - maker_fee(limit_price) # 0 on PM

Head-to-head: the TAKER arm is research.lab.execution.REALISTIC applied to the
SAME signal/population (so cancelled-maker games are simply taker trades the
maker arm declined). Both arms are scored by the SAME promotion gate via
research.lab.evaluate (with ledger_path for N-aware DSR).

Usage::

    python3 -m research.scripts.maker_fill_study --limit 1319 --sweep
    python3 -m research.scripts.maker_fill_study --limit 1319 --gate
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from research.lab import data  # noqa: E402
from research.lab.types import SPREAD, Trade, Trades  # noqa: E402
from research.lab.execution import FillModel, _interp_at, _SHORT_SIDES  # noqa: E402

LEDGER = "research/reports/alpha/ledger.jsonl"

# Polymarket maker fee is currently 0% (rebate/zero on resting liquidity); the
# 2% flat is a TAKER fee. Kalshi charges its standard fee to makers too. We model
# the optimistic-but-real PM maker path (0 fee) AND a Kalshi maker path.
PM_MAKER_FEE_RATE = 0.0


def _kalshi_maker_fee(price: float, size: float = 1.0) -> float:
    """Kalshi maker fee: same nonlinear formula as taker (Kalshi has no rebate)."""
    p = Decimal(str(price)); c = Decimal(str(size))
    raw = Decimal("0.07") * c * p * (Decimal("1") - p)
    return float((raw * 100).to_integral_value(rounding=ROUND_UP) / Decimal("100"))


def _pm_maker_fee(price: float, size: float = 1.0) -> float:
    return float(Decimal(str(price)) * Decimal(str(size)) * Decimal(str(PM_MAKER_FEE_RATE)))


# ---------------------------------------------------------------------------
# The signal: continuation anchoring on the spread book (mirror spread_realistic
# / spread_alpha, expressed over a lab Panel so both arms share the population).
# ---------------------------------------------------------------------------
def _signal_bar_and_side(panel, *, thresh, min_elapsed, max_elapsed, max_stale_min):
    """First qualifying (fresh, in-window) bar; returns (bar, side, sig) or None.

    side is 'cover_home' (bet final margin > snapped strike) or 'cover_away'.
    Direction = continuation: proj margin more extreme than implied -> bet that way.
    """
    e = np.asarray(panel.elapsed_sec, float)
    mar = np.asarray(panel.margin, float)
    impv = np.asarray(panel.mid, float)
    n = panel.n
    with np.errstate(divide="ignore", invalid="ignore"):
        proj = np.where(e > 120, mar * 2880.0 / e, np.nan)
    chg = np.r_[True, np.abs(np.diff(impv)) > 1e-9]
    last_chg = np.maximum.accumulate(np.where(chg, np.arange(n), -1))
    stale = (np.arange(n) - last_chg).astype(float)
    ok = np.isfinite(e) & np.isfinite(impv) & np.isfinite(proj)
    for k in range(n):
        if not ok[k] or e[k] < min_elapsed or e[k] > max_elapsed:
            continue
        if stale[k] > max_stale_min:
            continue
        sig = proj[k] - impv[k]
        if abs(sig) < thresh:
            continue
        proj_home = sig > 0
        side = "cover_home" if proj_home else "cover_away"
        return k, side, float(sig), stale
    return None


def _snap_strike_and_path(panel, side, entry_ts):
    """Snap to the nearest listed strike at entry_ts; return (strike, side_price_path).

    side_price_path[t] = the real quoted price you would PAY to buy `side` at bar
    t for the snapped strike: P(home_margin>strike) for cover_home, its complement
    for cover_away. NaNs where the ladder has no quote.
    """
    xp = np.asarray(panel.minute_ts, float)
    strikes = np.array(sorted(panel.ladder.keys()), float)
    implied = _interp_at(entry_ts, xp, panel.mid)
    if not np.isfinite(implied):
        return None
    si = int(np.argmin(np.abs(strikes - implied)))
    strike = float(strikes[si])
    p_home = np.asarray(panel.ladder[strike], float)
    short = side.strip().lower() in _SHORT_SIDES   # cover_away is a short side
    side_price = (1.0 - p_home) if short else p_home
    return strike, side_price


def _settle(panel, side, strike) -> float:
    bet_above = side.strip().lower() not in _SHORT_SIDES   # cover_home bets above
    out = panel.final_margin
    if out is None or strike is None or not np.isfinite(strike):
        return float("nan")
    ended_above = float(out) > float(strike)
    return 1.0 if (ended_above == bet_above) else 0.0


# ---------------------------------------------------------------------------
# Two execution arms over the SAME population.
# ---------------------------------------------------------------------------
@dataclass
class MakerConfig:
    edge_c: float = 1.0        # cents better than current quote we post our limit
    wait_min: float = 10.0     # minutes we leave the resting limit before cancel
    venue: str = "polymarket"  # maker fee schedule


def _maker_fee(venue, price):
    return _kalshi_maker_fee(price) if venue.lower() == "kalshi" else _pm_maker_fee(price)


def build_arms(panels, *, thresh=6.0, min_elapsed=600.0, max_elapsed=2520.0,
               max_stale_min=2.0, entry_lat_min=1.0, taker_half_spread=1.5,
               taker_venue="polymarket", maker: MakerConfig = MakerConfig()):
    """Return (taker_trades, maker_trades, diag) over the SAME signal population.

    The TAKER arm fires on every qualifying game (realistic listed-strike fill +
    half-spread + taker fee) -- this reproduces spread_realistic via the lab
    FillModel. The MAKER arm posts a passive limit `maker.edge_c` cents inside
    the quote and only trades the games whose real quote path subsequently
    touches the limit within `maker.wait_min`; the rest are CANCELLED (no trade).
    """
    taker_fill = FillModel(half_spread=taker_half_spread / 100.0, venue=taker_venue)
    taker_rows, maker_rows = [], []
    n_signal = n_maker_fill = n_maker_cancel = 0
    fill_lat_sum = 0.0
    for panel in panels:
        if not panel.ladder or panel.final_margin is None:
            continue
        sg = _signal_bar_and_side(panel, thresh=thresh, min_elapsed=min_elapsed,
                                  max_elapsed=max_elapsed, max_stale_min=max_stale_min)
        if sg is None:
            continue
        k, side, sig, stale = sg
        n_signal += 1
        xp = np.asarray(panel.minute_ts, float)
        entry_ts = float(xp[k] + entry_lat_min * 60.0)

        # --- TAKER arm (realistic, crosses the spread) ---
        try:
            fr = taker_fill.fill(panel, entry_ts, side)
        except Exception:
            fr = None
        if fr is not None and np.isfinite(fr.fill_mid):
            payoff = _settle(panel, side, fr.strike)
            if np.isfinite(payoff):
                all_in = fr.all_in_price
                taker_rows.append(Trade(
                    game_id=panel.game_id, date=panel.date, market=panel.market,
                    home_team=panel.home_team,
                    primary_team=(panel.home_team if side == "cover_home" else panel.away_team),
                    side=side, entry_ts=entry_ts, exit_ts=float(xp[-1]),
                    entry_price=float(fr.fill_mid), payoff=float(payoff),
                    pnl=float(payoff - all_in), entry_strike=float(fr.strike),
                    meta={"arm": "taker", "stale_min": float(stale[k]),
                          "half_spread": float(fr.half_spread), "fee": float(fr.fee),
                          "all_in_price": float(all_in)}))

        # --- MAKER arm (passive limit; fill read from the real quote path) ---
        snap = _snap_strike_and_path(panel, side, entry_ts)
        if snap is None:
            continue
        strike, side_price = snap
        # current quote for our side at the entry bar (clamped, like the fill model)
        j = int(np.searchsorted(xp, entry_ts))
        if j >= panel.n:
            j = panel.n - 1
        quote_j = side_price[j]
        if not np.isfinite(quote_j):
            col = side_price[: j + 1]
            col = col[np.isfinite(col)]
            if col.size == 0:
                continue
            quote_j = float(col[-1])
        quote_j = float(min(max(quote_j, 0.01), 0.99))
        limit = quote_j - maker.edge_c / 100.0
        if limit <= 0.01:
            # can't post a sane limit this cheap; treat as no maker trade
            n_maker_cancel += 1
            continue
        # Walk forward within the wait window; fill at first bar whose quoted
        # price for our side <= limit (the market traded through our resting bid).
        e = np.asarray(panel.elapsed_sec, float)
        t_end = entry_ts + maker.wait_min * 60.0
        filled_bar = None
        for t in range(j, panel.n):
            if xp[t] > t_end:
                break
            pr = side_price[t]
            if np.isfinite(pr) and pr <= limit + 1e-9:
                filled_bar = t
                break
        if filled_bar is None:
            n_maker_cancel += 1
            continue
        n_maker_fill += 1
        fill_lat_sum += (xp[filled_bar] - entry_ts) / 60.0
        fee = _maker_fee(maker.venue, limit)
        payoff = _settle(panel, side, strike)
        if not np.isfinite(payoff):
            n_maker_fill -= 1
            n_maker_cancel += 1
            continue
        maker_rows.append(Trade(
            game_id=panel.game_id, date=panel.date, market=panel.market,
            home_team=panel.home_team,
            primary_team=(panel.home_team if side == "cover_home" else panel.away_team),
            side=side, entry_ts=float(xp[filled_bar]), exit_ts=float(xp[-1]),
            entry_price=float(limit), payoff=float(payoff),
            pnl=float(payoff - limit - fee), entry_strike=float(strike),
            meta={"arm": "maker", "stale_min": float(stale[k]),
                  "half_spread": 0.0, "fee": float(fee),
                  "all_in_price": float(limit + fee),
                  "fill_lat_min": float((xp[filled_bar] - entry_ts) / 60.0)}))

    diag = {
        "n_signal": n_signal,
        "n_taker": len(taker_rows),
        "n_maker_fill": n_maker_fill,
        "n_maker_cancel": n_maker_cancel,
        "maker_fill_rate": (n_maker_fill / n_signal) if n_signal else float("nan"),
        "avg_fill_lat_min": (fill_lat_sum / n_maker_fill) if n_maker_fill else float("nan"),
    }
    return Trades(rows=taker_rows), Trades(rows=maker_rows), diag


def _win_rate(trades: Trades) -> float:
    if trades.empty:
        return float("nan")
    p = np.array([t.payoff for t in trades.rows], float)
    return float((p > 0.5).mean())


def _mean_cents(trades: Trades) -> float:
    if trades.empty:
        return float("nan")
    return float(np.mean([t.pnl for t in trades.rows]) * 100.0)


# ---------------------------------------------------------------------------
# CLI: head-to-head gate (taker vs maker) on the SAME population.
# ---------------------------------------------------------------------------
def _gate(trades, ledger_path=LEDGER):
    from research.lab.evaluate import evaluate
    return evaluate(trades, ledger_path=ledger_path)


def _fmt_gate(label, gr):
    cs = gr.cost_sweep or {}
    sweep = "  ".join(f"{c}c:{v['cents']:+.2f}({'P' if v['gate'] else 'F'})"
                      for c, v in sorted(cs.items()))
    print(f"  [{label}] n={gr.n} games={gr.n_games} net={gr.cents_per_contract:+.2f}c "
          f"CI[{gr.ci_lo:+.2f},{gr.ci_hi:+.2f}] gate={'PASS' if gr.passed else 'FAIL'}")
    print(f"        cost-sweep: {sweep}")
    adv = gr.adversarial or {}
    for name, d in adv.items():
        print(f"        adv/{name}: {'ok' if d['passed'] else 'X'} {d['detail']}")
    if gr.reasons:
        print(f"        reasons: {'; '.join(gr.reasons[:4])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--thresh", type=float, default=6.0)
    ap.add_argument("--half-spread", type=float, default=1.5, help="TAKER half-spread, cents")
    ap.add_argument("--edge-c", type=float, default=1.5, help="maker limit improvement, cents")
    ap.add_argument("--wait-min", type=float, default=10.0)
    ap.add_argument("--venue", default="polymarket")
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--sweep", action="store_true", help="sweep maker edge/wait")
    ap.add_argument("--gate", action="store_true", help="full head-to-head gate")
    a = ap.parse_args()

    panels = data.load_panels(SPREAD, split="nontest", limit=a.limit)
    print(f"loaded {len(panels)} nontest spread panels\n", flush=True)

    if a.sweep:
        print("=== MAKER FILL/WIN/ADVERSE-SELECTION SWEEP (vs taker baseline) ===")
        tk0 = None
        for edge_c in (0.5, 1.0, 1.5, 2.0, 3.0):
            for wait in (5.0, 10.0, 20.0):
                tk, mk, diag = build_arms(
                    panels, thresh=a.thresh, taker_half_spread=a.half_spread,
                    taker_venue=a.venue,
                    maker=MakerConfig(edge_c=edge_c, wait_min=wait, venue=a.venue))
                if tk0 is None:
                    tk0 = (_win_rate(tk), _mean_cents(tk), len(tk))
                print(f"  edge={edge_c:.1f}c wait={wait:>4.0f}m | "
                      f"fill_rate={diag['maker_fill_rate']:.3f} "
                      f"maker_win={_win_rate(mk):.3f} maker_net={_mean_cents(mk):+.2f}c "
                      f"(n={len(mk)}) | taker_win={tk0[0]:.3f} taker_net={tk0[1]:+.2f}c",
                      flush=True)
        return

    if a.gate:
        tk, mk, diag = build_arms(
            panels, thresh=a.thresh, taker_half_spread=a.half_spread, taker_venue=a.venue,
            maker=MakerConfig(edge_c=a.edge_c, wait_min=a.wait_min, venue=a.venue))
        print("=== POPULATION / FILL DIAGNOSTICS ===")
        for k, v in diag.items():
            print(f"  {k}: {v}")
        print(f"  taker win={_win_rate(tk):.3f}  maker win={_win_rate(mk):.3f}  "
              f"(ADVERSE SELECTION gap = {_win_rate(tk)-_win_rate(mk):+.3f})")
        print(f"\n=== HEAD-TO-HEAD GATE (ledger={a.ledger}) ===")
        _fmt_gate("TAKER realistic", _gate(tk, a.ledger))
        print()
        _fmt_gate(f"MAKER edge={a.edge_c}c wait={a.wait_min}m {a.venue}", _gate(mk, a.ledger))
        return

    print("pass --sweep or --gate")


if __name__ == "__main__":
    main()
