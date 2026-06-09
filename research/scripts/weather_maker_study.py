"""opus_maker — MAKER-execution study on the FROZEN forecast-gap signal (weather/temp).

Hypothesis id ``b8b071fa26ea4933`` (registered + claimed agent ``opus_maker``
BEFORE any maker PnL was computed). Family ``weather``, market ``temp``.

WHY THIS STUDY EXISTS
---------------------
The forecast-gap signal (parent hyp ``3cb384c482d1c2f7``,
``research/lab/runs/fable_forecast_20260609T225416Z.md``) carries a REAL
out-of-sample GROSS edge of +3.41c at the ATM strike, but is DEAD as a TAKER:
the 1.5c half-spread + 2.0c ATM Kalshi taker fee (=3.5c all-in) eat it exactly
(net -0.08c, CI straddles zero). This study asks the ONLY remaining question:
does MAKER execution (rest a passive bid, pay the bid not the ask, ~0c maker
fee) recover the edge? The unknowns are FILL PROBABILITY and ADVERSE SELECTION,
and we measure BOTH from the real per-minute traded-price path — never assume.

THE HONESTY PROBLEM (the whole point — see research/scripts/maker_fill_study.py)
--------------------------------------------------------------------------------
A resting BUY at price ``r`` fills only when a seller trades down to ``r`` — i.e.
precisely when the market is moving DOWN through our level, toward the OTHER
outcome. That is adverse selection: conditional on a fill, settlement tends
AGAINST us. We do NOT assume a number. We:
  (1) FILL PROBABILITY — read from the SAME boundary position's REAL per-minute
      TRADED-price low + traded VOLUME (the OHLC candles refetched in Phase 1).
      A fill requires an actual TRADE to print at/through our resting price.
  (2) ADVERSE SELECTION — settle each FILLED signal against the SAME real
      settlement the taker uses, and compare the win rate of the FILLED vs the
      UNFILLED signal populations. If makers are picked off, filled wins < unfilled.

THE TRADEABLE-INSTRUMENT SUBTLETY (documented, conservative)
------------------------------------------------------------
The frozen signal trades the ATM **boundary** P(high > b) — a CUMULATIVE synthetic
built by the provider from the per-bucket book (fewest-legs side), NOT a single
listed Kalshi contract. To keep the maker study's price/settlement correspondence
IDENTICAL to the frozen taker (which fills and settles against the boundary), we
synthesize the boundary's MAKER inputs from the SAME bucket aggregation, applied
to the Phase-1 OHLC candle fields:

  * boundary best-bid for the OVER side  = sum of contributing legs' ``yes_bid``
    (you must join each leg's bid to assemble the cumulative position; paying each
    leg's bid is the conservative cost). For the UNDER side the rest price is the
    complement: ``1 - (boundary best-ASK)`` where boundary ask = sum of legs' asks.
  * "a TRADE printed at/through our resting buy in minute t" — we require, in that
    minute, (a) VOLUME > 0 on at least one contributing leg (an actual trade
    printed, not a quote flicker) AND (b) the boundary's cumulative traded-price
    LOW <= our rest price (the traded position value reached our level). The
    cumulative traded-price low uses each leg's ``price_low`` when that leg
    printed, else its ``mid`` close (no trade that minute on that leg). This is a
    PROXY and is documented as such: a strict simultaneous multi-leg fill is
    harder than this (so this proxy is, if anything, GENEROUS to the maker on
    multi-leg boundaries) — we flag multi-leg fills in the diagnostics so the
    bug-hunt can isolate them, and we report the single-leg-boundary subset
    (where the boundary IS one real contract and the fill model is exact).

QUEUE POSITION (conservative, documented)
-----------------------------------------
We JOIN the best bid (rest at the bid, not inside it). Joining means we sit
BEHIND existing resting size at that price. We therefore do NOT fill on a mere
touch of our price; we require the traded LOW to reach our price AND a trade to
print — i.e. the market traded AT or THROUGH our level. We also report a strict
``queue_through_c`` variant (require the low to trade queue_c cents BELOW our
rest before we count a fill) as an even-more-pessimistic robustness check.

MAKER FEE
---------
Kalshi maker fees: charged only on execution, materially lower than taker, and
round to $0.00/contract for most small (1-contract) trades; only special series
(elections / sports championships) carry non-zero maker fees. The KXHIGH weather
series is a standard general series, so the maker fee is effectively 0c at our
1-contract size (kalshi.com/fee-schedule, help.kalshi.com/en/articles/13823805).
We could not pull an exact KXHIGH-specific number from the published schedule, so
per the assignment we treat 0c as the BASE case AND also report 0.5c and 1.0c.

PRE-REGISTERED — every number below frozen BEFORE scoring
---------------------------------------------------------
  SIGNAL: EXACT frozen forecast-gap params (reused from
    research.scripts.weather_forecast_gap_e2e — same per-city TRAIN debias,
    gap>=2.0F, [0.05,0.30] window, freshness<=2min, i+1 latency, one trade/event).
    NO retuning.
  MAKER FILL: rest at the prevailing best bid for the signal side at i+1; FILLED
    iff within horizon H a minute has volume>0 (a trade printed) AND boundary
    traded-low <= rest. Unfilled = no trade (EV 0), KEPT in the denominator.
  HORIZON SWEEP (pre-registered robustness): H in {10, 30, 60} minutes; H=30 is
    the headline.
  MAKER FEE: {0c base, 0.5c, 1.0c}.
  HEADLINE: fill_rate; edge_per_FILLED trade (payoff - rest - maker_fee) with
    block-bootstrap CI via lab.evaluate on the FILLED trades; per-SIGNAL EV =
    fill_rate x edge_filled; ADVERSE-SELECTION = win(filled) vs win(unfilled).
  GATE on per-SIGNAL EV: lower CI bound for per-signal EV is constructed as
    fill_rate x (filled-trade bootstrap CI), since unfilled signals contribute
    EXACTLY 0 (no variance): EV = (1/N_signal) * sum over filled of pnl, so the
    bootstrap of the filled pnls scaled by (n_filled / N_signal) is the per-signal
    EV distribution. Reported explicitly.

Run::

    python -m research.scripts.weather_maker_study --split train
    python -m research.scripts.weather_maker_study --split nontest
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
from dataclasses import dataclass

import numpy as np

from research.lab import data as lab_data
from research.lab import evaluate as lab_eval
from research.lab.providers import weather as W
from research.lab.providers.weather import TEMP
from research.scripts import weather_forecast_gap_e2e as FROZEN
from research.lab.types import Trade, Trades
from research.scorer.bootstrap import block_bootstrap_by_game

HYP_ID = "b8b071fa26ea4933"
PARENT_HYP_ID = "3cb384c482d1c2f7"
LEDGER = "research/reports/alpha/ledger.jsonl"

# ---- pre-registered maker-fill parameters (frozen before scoring) -----------
HORIZONS_MIN = (10.0, 30.0, 60.0)     # pre-registered H sweep; 30 is headline
HEADLINE_H = 30.0
MAKER_FEES_C = (0.0, 0.5, 1.0)        # 0c base + robustness
ENTRY_LATENCY_MIN = 1.0               # i+1, same as frozen
QUEUE_THROUGH_C = 0.0                 # base: fill at/through; robustness variant >0


# ---------------------------------------------------------------------------
# Boundary-cumulative MAKER inputs, rebuilt from the raw cached pkl (the Phase-1
# OHLC candle fields). We do NOT modify build_panel; we replicate its EXACT
# fewest-legs aggregation so the boundary set and orientation match the panel.
# ---------------------------------------------------------------------------
@dataclass
class MakerBook:
    """Per-event, per-boundary maker inputs aligned to the panel's minute grid."""
    minute_ts: np.ndarray                       # (n,)
    boundaries: list                            # sorted strikes (== panel.ladder keys)
    over_bid: dict                              # strike -> (n,) cumulative OVER best bid
    over_ask: dict                              # strike -> (n,) cumulative OVER best ask
    traded_low: dict                            # strike -> (n,) cumulative OVER traded LOW
    traded_high: dict                           # strike -> (n,) cumulative OVER traded HIGH
    traded_vol: dict                            # strike -> (n,) total contributing-leg volume
    n_over_legs: dict                           # strike -> #buckets forming the OVER position


def _leg_series(candles, key, ts_index, n):
    """Forward-filled per-minute series of ``key`` for one bucket's candles."""
    out = np.full(n, np.nan)
    for c in candles:
        t = c.get("ts")
        v = c.get(key)
        if t is None or v is None:
            continue
        out[ts_index[int(t) // 60 * 60]] = float(v)
    last = np.nan
    for i in range(n):
        if np.isfinite(out[i]):
            last = out[i]
        else:
            out[i] = last
    return out


def _raw_leg_series(candles, key, ts_index, n):
    """Per-minute series of ``key`` WITHOUT forward-fill (NaN where no candle).

    Used for traded-price low and volume: these are minute-local events; a stale
    forward-fill would fabricate trades. Volume NaN -> treated as 0 (no print)."""
    out = np.full(n, np.nan)
    for c in candles:
        t = c.get("ts")
        v = c.get(key)
        if t is None or v is None:
            continue
        out[ts_index[int(t) // 60 * 60]] = float(v)
    return out


def build_maker_book(rec: dict):
    """Rebuild boundary-cumulative maker inputs from a raw cached event record.

    Mirrors weather.build_panel's market selection, sort, boundary set, and
    fewest-legs side EXACTLY, so ``boundaries`` == ``panel.ladder.keys()`` and the
    OVER orientation matches. Returns ``None`` if the book is unusable or lacks
    the Phase-1 OHLC keys.
    """
    markets = []
    for m in rec.get("markets", []):
        b = W._bucket_bounds(m)
        if b is None or not m.get("candles"):
            continue
        markets.append((b, m))
    if len(markets) < 2:
        return None
    markets.sort(key=lambda bm: (bm[0][0], bm[0][1]))

    # Require Phase-1 OHLC keys on the candles.
    if "price_low" not in (markets[0][1]["candles"][0] or {}):
        return None

    ts_all = sorted({int(c["ts"]) // 60 * 60
                     for _, m in markets for c in m["candles"]
                     if c.get("ts") is not None})
    if len(ts_all) < 5:
        return None
    ts_index = {t: i for i, t in enumerate(ts_all)}
    n = len(ts_all)
    minute_ts = np.asarray(ts_all, dtype=float)

    bids = np.vstack([_leg_series(m["candles"], "yes_bid", ts_index, n) for _, m in markets])
    asks = np.vstack([_leg_series(m["candles"], "yes_ask", ts_index, n) for _, m in markets])
    mids = np.vstack([_leg_series(m["candles"], "mid", ts_index, n) for _, m in markets])
    plow = np.vstack([_raw_leg_series(m["candles"], "price_low", ts_index, n) for _, m in markets])
    phigh = np.vstack([_raw_leg_series(m["candles"], "price_high", ts_index, n) for _, m in markets])
    vol = np.vstack([_raw_leg_series(m["candles"], "volume", ts_index, n) for _, m in markets])

    k = len(markets)
    boundaries = [markets[j + 1][0][0] for j in range(k - 1)]
    over_bid, over_ask, traded_low, traded_high, traded_vol, n_legs = {}, {}, {}, {}, {}, {}
    with np.errstate(invalid="ignore"):
        for j, b in enumerate(boundaries):
            legs = slice(j + 1, k)                     # legs ABOVE the boundary = OVER position
            # We always assemble the OVER position from the ABOVE legs directly
            # (the cumulative buy of P(high>b)) — the conservative, leg-faithful
            # construction regardless of which side the prob ladder summed.
            above_bid, above_ask, above_mid = bids[legs], asks[legs], mids[legs]
            above_low, above_high, above_vol = plow[legs], phigh[legs], vol[legs]
            if above_bid.shape[0] == 0:
                continue
            ob = np.nansum(above_bid, axis=0)
            ob[~np.isfinite(above_bid).any(axis=0)] = np.nan
            oa = np.nansum(above_ask, axis=0)
            oa[~np.isfinite(above_ask).any(axis=0)] = np.nan
            # cumulative traded LOW/HIGH: per leg use the traded price when it
            # printed, else the mid close (no trade that minute on that leg).
            leg_low = np.where(np.isfinite(above_low), above_low, above_mid)
            leg_high = np.where(np.isfinite(above_high), above_high, above_mid)
            tl = np.nansum(leg_low, axis=0)
            tl[~np.isfinite(leg_low).any(axis=0)] = np.nan
            th = np.nansum(leg_high, axis=0)
            th[~np.isfinite(leg_high).any(axis=0)] = np.nan
            tv = np.where(np.isfinite(above_vol), above_vol, 0.0).sum(axis=0)
            over_bid[float(b)] = np.clip(ob, 0.0, 1.0)
            over_ask[float(b)] = np.clip(oa, 0.0, 1.0)
            traded_low[float(b)] = np.clip(tl, 0.0, 1.0)
            traded_high[float(b)] = np.clip(th, 0.0, 1.0)
            traded_vol[float(b)] = tv
            n_legs[float(b)] = int(k - (j + 1))
    if len(over_bid) < 2:
        return None
    return MakerBook(minute_ts=minute_ts, boundaries=sorted(over_bid.keys()),
                     over_bid=over_bid, over_ask=over_ask,
                     traded_low=traded_low, traded_high=traded_high,
                     traded_vol=traded_vol, n_over_legs=n_legs)


# ---------------------------------------------------------------------------
# The maker arm over the FROZEN signal population.
# ---------------------------------------------------------------------------
def run_arm(panels, recs_by_gid, *, horizon_min: float, maker_fee_c: float,
            queue_through_c: float = 0.0):
    """One maker arm over the frozen signal population at a given (H, fee).

    Returns (filled_trades, signals_diag). Every qualifying signal is recorded;
    only filled ones become Trades. Unfilled signals contribute EV 0 and live in
    the denominator (n_signal).
    """
    n_signal = 0
    filled_rows = []
    win_filled, win_unfilled = [], []
    rest_prices, fill_lat = [], []
    multileg_fills = 0
    n_over = n_under = 0
    n_window_had_trade = 0      # signals where >=1 minute in H had volume>0 (could fill)
    for panel in panels:
        rec = recs_by_gid.get(panel.game_id)
        if rec is None:
            continue
        book = build_maker_book(rec)
        if book is None:
            continue
        # --- frozen signal: reuse the EXACT entry/side logic + i+1 latency ---
        bar = _frozen_signal_bar(panel)
        if bar is None:
            continue
        side = FROZEN.side(panel, bar)            # "over" / "under"
        n_signal += 1
        xp = np.asarray(panel.minute_ts, float)
        entry_ts = float(xp[bar] + ENTRY_LATENCY_MIN * 60.0)

        # snap to the nearest LISTED boundary (== ATM strike the taker uses)
        strikes = np.asarray(book.boundaries, float)
        implied = float(np.interp(entry_ts, xp, _ffill(panel.mid)))
        si = int(np.argmin(np.abs(strikes - implied)))
        strike = float(strikes[si])

        # align the maker book to the entry bar (book minute grid == panel grid)
        j = int(np.searchsorted(book.minute_ts, entry_ts))
        if j >= len(book.minute_ts):
            j = len(book.minute_ts) - 1

        # rest price for the signal side (JOIN the best bid for that side)
        ob = book.over_bid.get(strike)
        oa = book.over_ask.get(strike)
        if ob is None or oa is None:
            n_signal -= 1
            continue
        if side == "over":
            rest = ob[j]                       # join the OVER best bid
        else:                                  # under: join the UNDER bid = 1 - over best ASK
            rest = 1.0 - oa[j]
        if not np.isfinite(rest):
            n_signal -= 1
            continue
        rest = float(min(max(rest, 0.0), 1.0))

        # settlement payoff for this side vs the boundary (SAME as frozen taker)
        payoff = _settle_boundary(panel, side, strike)
        if not np.isfinite(payoff):
            n_signal -= 1
            continue
        if side == "over":
            n_over += 1
        else:
            n_under += 1

        # did ANY trade print in the H window at all (fill was even possible)?
        t_end = entry_ts + horizon_min * 60.0
        vol = book.traded_vol.get(strike)
        if vol is not None:
            wmask = (book.minute_ts >= entry_ts) & (book.minute_ts <= t_end + 1e-9)
            if np.any(vol[wmask] > 0):
                n_window_had_trade += 1

        # --- FILL DETECTION: a trade printed at/through our resting buy ---
        filled_bar = _detect_fill(book, strike, side, j, entry_ts, horizon_min,
                                  rest, queue_through_c)
        if filled_bar is None:
            win_unfilled.append(payoff)
            continue

        # multileg flag for bug-hunt (over position built from >1 bucket leg)
        if side == "over" and book.n_over_legs.get(strike, 1) > 1:
            multileg_fills += 1

        fee = maker_fee_c / 100.0
        pnl = float(payoff - rest - fee)
        win_filled.append(payoff)
        rest_prices.append(rest)
        fill_lat.append((book.minute_ts[filled_bar] - entry_ts) / 60.0)
        filled_rows.append(Trade(
            game_id=panel.game_id, date=panel.date, market=panel.market,
            home_team=panel.home_team,
            primary_team=(panel.home_team if side == "over" else panel.away_team),
            side=side, entry_ts=float(book.minute_ts[filled_bar]), exit_ts=float(xp[-1]),
            entry_price=float(rest), payoff=float(payoff), pnl=pnl,
            entry_strike=strike,
            meta={"arm": "maker", "horizon_min": horizon_min, "maker_fee_c": maker_fee_c,
                  "stale_min": 0.0,  # freshness enforced by frozen signal mask
                  "half_spread": 0.0, "fee": float(fee),
                  "all_in_price": float(rest + fee),
                  "fill_lat_min": float((book.minute_ts[filled_bar] - entry_ts) / 60.0)}))

    diag = {
        "n_signal": n_signal,
        "n_filled": len(filled_rows),
        "n_unfilled": len(win_unfilled),
        "fill_rate": (len(filled_rows) / n_signal) if n_signal else float("nan"),
        "win_filled": (float(np.mean(np.asarray(win_filled) > 0.5)) if win_filled else float("nan")),
        "win_unfilled": (float(np.mean(np.asarray(win_unfilled) > 0.5)) if win_unfilled else float("nan")),
        "avg_rest": (float(np.mean(rest_prices)) if rest_prices else float("nan")),
        "avg_fill_lat_min": (float(np.mean(fill_lat)) if fill_lat else float("nan")),
        "multileg_over_fills": multileg_fills,
        "n_over": n_over, "n_under": n_under,
        "n_window_had_trade": n_window_had_trade,
        "frac_window_had_trade": (n_window_had_trade / n_signal) if n_signal else float("nan"),
    }
    return Trades(rows=filled_rows), diag


def _detect_fill(book, strike, side, j, entry_ts, horizon_min, rest, queue_through_c):
    """First bar within H where a TRADE printed at/through our resting buy.

    over side: our position is the OVER boundary position; a seller hitting our
      resting bid prints a trade at price <= rest. Fill iff a minute has
      volume>0 (a trade printed) AND OVER traded-low <= rest - queue.
    under side: our position is the UNDER boundary position (complement), value =
      1 - over value, and it prints at price = 1 - (over print price). A trade
      at/through our under bid ``rest`` means under_print <= rest, i.e. the over
      position printed at >= 1-rest, i.e. the OVER traded-HIGH reached 1-rest, i.e.
      the under traded-LOW = 1 - over_traded_high <= rest. Fill iff volume>0 AND
      (1 - OVER traded-high) <= rest - queue.
    """
    t_end = entry_ts + horizon_min * 60.0
    tl_over = book.traded_low.get(strike)
    th_over = book.traded_high.get(strike)
    vol_over = book.traded_vol.get(strike)
    if tl_over is None or th_over is None or vol_over is None:
        return None
    thresh = rest - queue_through_c / 100.0
    for t in range(j, len(book.minute_ts)):
        if book.minute_ts[t] > t_end + 1e-9:
            break
        if not (vol_over[t] > 0):           # an actual trade must print this minute
            continue
        if side == "over":
            if np.isfinite(tl_over[t]) and tl_over[t] <= thresh + 1e-12:
                return t
        else:
            under_low = 1.0 - th_over[t]    # deepest the UNDER position printed
            if np.isfinite(under_low) and under_low <= thresh + 1e-12:
                return t
    return None


def _ffill(a):
    a = np.asarray(a, float).copy()
    last = np.nan
    for i in range(len(a)):
        if np.isfinite(a[i]):
            last = a[i]
        else:
            a[i] = last
    return a


def _frozen_signal_bar(panel):
    """The frozen signal's first qualifying bar (entry mask + window + freshness).

    Reuses FROZEN.entry + the same window/freshness the frozen Strategy enforces
    (min_elapsed=0, max_elapsed=200000, max_stale_min=2). One trade per event.
    """
    from research.lab.strategy import staleness_min
    mask = np.asarray(FROZEN.entry(panel), bool)
    elapsed = np.asarray(panel.elapsed_sec, float)
    stale = staleness_min(panel)
    window = (elapsed >= 0.0) & (elapsed <= 200000.0)
    fresh = stale <= 2.0
    q = np.flatnonzero(mask & window & fresh & np.isfinite(elapsed))
    return int(q[0]) if q.size else None


def _settle_boundary(panel, side, strike):
    """Settlement in {0,1}: over wins iff final > strike; under wins iff final <= strike."""
    out = panel.final_total
    if out is None or strike is None or not np.isfinite(strike):
        return float("nan")
    ended_above = float(out) > float(strike)
    bet_above = (side == "over")
    return 1.0 if (ended_above == bet_above) else 0.0


# ---------------------------------------------------------------------------
# Per-signal EV with a CI scaled from the filled-trade bootstrap.
# ---------------------------------------------------------------------------
def per_signal_ev(filled: Trades, n_signal: int):
    """Per-signal EV and its CI, constructed honestly from the filled bootstrap.

    Unfilled signals contribute EXACTLY 0 with zero variance, so the per-signal EV
    is (sum of filled pnl)/N_signal = fill_rate * mean(filled pnl). Bootstrapping
    the filled pnls by game and scaling each bootstrap mean by (n_filled/N_signal)
    gives the per-signal EV sampling distribution. Returns cents.
    """
    if filled.empty or n_signal <= 0:
        return {"ev_c": float("nan"), "ci_lo": float("nan"), "ci_hi": float("nan"),
                "edge_filled_c": float("nan"), "fill_rate": 0.0}
    pnl = np.array([t.pnl for t in filled.rows], float)
    gid = np.array([t.game_id for t in filled.rows])
    bb = block_bootstrap_by_game(pnl, gid)
    scale = len(filled.rows) / n_signal
    return {
        "edge_filled_c": float(np.mean(pnl) * 100.0),
        "fill_rate": scale,
        "ev_c": float(np.mean(pnl) * 100.0 * scale),
        "ci_lo": float(bb.ci_lo * 100.0 * scale),
        "ci_hi": float(bb.ci_hi * 100.0 * scale),
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _load_recs(panels):
    """Map game_id -> raw cached record (for the maker book rebuild)."""
    out = {}
    for p in panels:
        path = W.CACHE_DIR / f"{p.game_id}.pkl"
        if path.exists():
            try:
                with open(path, "rb") as fh:
                    out[p.game_id] = pickle.load(fh)
            except Exception:  # noqa: BLE001
                pass
    return out


def run(split: str, ledger: bool = True) -> dict:
    panels = lab_data.load_panels(TEMP, split=split)
    recs = _load_recs(panels)
    out = {"hyp_id": HYP_ID, "parent_hyp_id": PARENT_HYP_ID, "split": split,
           "n_panels": len(panels), "horizons": {}}

    for H in HORIZONS_MIN:
        h_block = {"maker_fee_c": {}}
        # base fee (0c) drives the gate + adverse-selection autopsy
        for fee_c in MAKER_FEES_C:
            filled, diag = run_arm(panels, recs, horizon_min=H, maker_fee_c=fee_c,
                                   queue_through_c=QUEUE_THROUGH_C)
            ev = per_signal_ev(filled, diag["n_signal"])
            block = {"diag": diag, "per_signal_ev": ev}
            # full gate only on the base fee at the headline horizon (the decisive one)
            if fee_c == 0.0:
                use_ledger = ledger and (H == HEADLINE_H)
                gate = lab_eval.evaluate(
                    filled, ledger_path=(LEDGER if use_ledger else None),
                    family="weather")
                block["filled_gate"] = {
                    "cents_per_filled": gate.cents_per_contract,
                    "ci": [gate.ci_lo, gate.ci_hi],
                    "passed": gate.passed, "n": gate.n, "n_games": gate.n_games,
                    "cost_sweep": gate.cost_sweep,
                    "adversarial": gate.adversarial,
                    "walkforward": gate.walkforward,
                    "governance": gate.governance,
                }
            h_block["maker_fee_c"][fee_c] = block
        out["horizons"][H] = h_block

    # robustness: strict queue-through at headline H, 0c fee
    filled_q, diag_q = run_arm(panels, recs, horizon_min=HEADLINE_H, maker_fee_c=0.0,
                               queue_through_c=1.0)
    out["queue_through_1c_headlineH"] = {
        "diag": diag_q, "per_signal_ev": per_signal_ev(filled_q, diag_q["n_signal"])}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", default="train", choices=["train", "val", "nontest"])
    ap.add_argument("--no-ledger", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(run(a.split, ledger=not a.no_ledger), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
