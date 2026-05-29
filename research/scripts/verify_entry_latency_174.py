"""Verify the adversarial review's headline leakage claim for id=174:

The replay engine fills ENTRIES on the signal bar (bar i) but sources EXITS from
bar i+1 (one bar of latency). The review claims that charging SYMMETRIC entry
latency (fill entries at bar i+1 too) flips val pnl_net +$24.26 -> ~-$11 with a
block-bootstrap CI entirely below zero.

Method: re-implement replay_game's loop verbatim with one knob, entry_latency_bars.
  - entry_latency_bars=0  -> original behavior; MUST reproduce +$24.26 / n=400
                            (proves the copy is faithful)
  - entry_latency_bars=1  -> symmetric with exits; the test.
Everything else (exit logic, fills, costs, features) is the untouched engine.
Runs on VAL only, under live_pm. No registry writes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import sqlite3

from research.harness import replay as R
from research.harness.cost_profile import LIVE_PM, use_profile
from research.harness.run_batch import load_split
from research.harness.strategy_spec import StrategySpec
from research.scorer.bootstrap import block_bootstrap_by_game


def load_spec(trial_id: int) -> StrategySpec:
    conn = sqlite3.connect(str(_ROOT / "market_data" / "trials.db"))
    try:
        row = conn.execute("SELECT spec_json FROM trials WHERE id=?;", (trial_id,)).fetchone()
    finally:
        conn.close()
    return StrategySpec.from_json(row[0])


def replay_game_latency(spec: StrategySpec, game_id: str, entry_latency_bars: int):
    """Faithful copy of replay.replay_game with an entry-latency knob.

    Returns list[Trade]. Exit path and all helpers are the untouched engine.
    """
    spec.validate()
    venue = spec.venue
    if venue == "either":
        venue = "polymarket"

    game = R.load_game(game_id, allow_test=False)
    bars = R._build_bars(game, venue=venue)

    trades = []
    skipped = []
    position = None
    last_bar_idx = len(bars) - 1

    for i, bar in enumerate(bars):
        game_clock = float(bar["elapsed_game_sec"])
        features = {}
        feature_unavailable = False
        for fname in spec.features:
            try:
                features[fname] = R.REGISTRY.compute(fname, bar, game_clock)
            except RuntimeError:
                feature_unavailable = True
                break
        if feature_unavailable:
            continue

        context = R._build_context(bar, position, features, venue=venue)

        # --- Exit path (verbatim) ---
        if position is not None:
            time_stop_fired = (
                game_clock - position["entry_game_sec"] >= spec.time_stop_sec
            )
            explicit_exit = False
            try:
                for cond in spec.exit_conditions:
                    if R.eval_condition(cond, context):
                        explicit_exit = True
                        break
            except Exception:
                pass

            if explicit_exit or time_stop_fired:
                exit_reason = "explicit_exit" if explicit_exit else "time_stop"
                next_idx = i + 1
                if next_idx < len(bars):
                    exit_bar = bars[next_idx]
                else:
                    exit_bar = bar
                    exit_reason = "end_of_game"
                trade = R._close_position(
                    spec=spec, game_id=game_id, venue=venue, position=position,
                    exit_bar=exit_bar, exit_reason=exit_reason, game_meta=game["meta"],
                )
                if trade is not None:
                    trades.append(trade)
                position = None
                continue

        # --- Entry path (ONLY change: fill bar = i + entry_latency_bars) ---
        if position is None:
            if i == last_bar_idx:
                continue
            try:
                entry_fires = R.eval_condition(spec.entry_condition, context)
            except Exception:
                continue
            if not entry_fires:
                continue
            resolved_side = R._resolve_side(spec.side, bar, features)
            if resolved_side is None:
                continue

            fill_idx = i + entry_latency_bars
            if fill_idx > last_bar_idx:
                continue
            fill_bar = bars[fill_idx]

            snap, synthetic = R._make_orderbook(venue, resolved_side, fill_bar)
            if snap is None:
                continue
            fill = R.simulate_fill(
                snap, side="buy", size=spec.sizing.value, latency_ms=250,
                stale_threshold_sec=float("inf"), now_wall=float(fill_bar["minute_ts"]),
            )
            if fill.kill_reason is not None or not fill.filled:
                continue
            position = {
                "side": resolved_side,
                "size": fill.filled_size,
                "entry_price": fill.avg_price,
                "entry_fill": fill,
                "entry_wall": int(fill_bar["minute_ts"]),
                "entry_game_sec": float(fill_bar["elapsed_game_sec"]),
            }

    if position is not None:
        trade = R._settle_position(
            spec=spec, game_id=game_id, position=position,
            last_bar=bars[-1] if bars else None, game_meta=game["meta"],
        )
        if trade is not None:
            trades.append(trade)
    return trades


def run_val(spec, entry_latency_bars):
    pnls, games = [], []
    n_games_traded = 0
    for gid in load_split("val"):
        try:
            trades = replay_game_latency(spec, gid, entry_latency_bars)
        except FileNotFoundError:
            continue
        except Exception:
            continue
        if trades:
            n_games_traded += 1
        for t in trades:
            pnls.append(float(t.pnl_net))
            games.append(gid)
    pnl = np.array(pnls, dtype=float)
    g = np.array(games, dtype=object)
    n = len(pnl)
    total = float(pnl.sum())
    mean = float(pnl.mean()) if n else float("nan")
    wins = float((pnl > 0).mean()) if n else float("nan")
    if n >= 2:
        boot = block_bootstrap_by_game(pnl_per_trade=pnl, game_id_per_trade=g, rng_seed=42)
        ci = (boot.ci_lo, boot.ci_hi)
    else:
        ci = (float("nan"), float("nan"))
    return dict(n=n, games=n_games_traded, total=total, mean=mean, win=wins, ci_lo=ci[0], ci_hi=ci[1])


def main():
    import sys as _sys
    ids = [int(x) for x in _sys.argv[1:]] or [174]
    print("ENTRY-LATENCY VERIFICATION (val, live_pm) — 0-bar (original) vs 1-bar (symmetric w/ exits)")
    for tid in ids:
        spec = load_spec(tid)
        with use_profile(LIVE_PM):
            base = run_val(spec, 0)
            sym = run_val(spec, 1)
        print("\n--- trial id=%d (%s) ---" % (tid, spec.name))
        print("  0-bar: n=%(n)4d pnl=$%(total)7.2f mean=%(mean)+.4f win=%(win).3f CI=[%(ci_lo)+.4f,%(ci_hi)+.4f]" % base)
        print("  1-bar: n=%(n)4d pnl=$%(total)7.2f mean=%(mean)+.4f win=%(win).3f CI=[%(ci_lo)+.4f,%(ci_hi)+.4f]" % sym)
        survives = sym["ci_lo"] > 0
        print("  -> edge survives symmetric latency (CI lo>0)?: %s  (pnl %s)" %
              ("YES" if survives else "NO", "stays +" if sym["total"] > 0 else "flips -"))


if __name__ == "__main__":
    main()
