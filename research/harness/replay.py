"""Single-game replay engine — the load-bearing piece of Phase 0.

Walks one game's bars chronologically, applies a ``StrategySpec``'s entry/
exit conditions against the bar context + registered features, calls
``simulate_fill`` at every entry/exit, returns a ``TrialResult`` with
per-game PnL.

The replay engine is the load-bearing piece of Phase 0 — every backtest,
every overfit-canary test, every promotion gate flows through it. The
invariants this module enforces (in order of importance):

1. **Live-availability** on every feature read: a feature must not be
   resolved before its registered ``available_at_offset_sec``. The
   registry already asserts this; we additionally catch the resulting
   ``RuntimeError`` and SKIP the bar (record a reason in
   ``TrialResult.skipped``) rather than crash the run.

2. **No lookahead**: at bar t, only data up to and including bar t is
   visible to entry / exit conditions. The exit price uses bar t+1's
   orderbook (the next bar after the exit condition fires) to model
   realistic execution latency. If there is no bar t+1, the exit is
   marked-to-market against the last bar's mid (or settled at 0/1 if the
   position carries through end-of-game).

3. **At most one open position per game** (Phase 0 simplification — the
   position state machine is ``None -> OPEN -> CLOSED -> None``; the spec
   can re-enter on a subsequent bar once the previous position closes).

4. **Reproducibility**: replay_game(spec, gid, seed) is byte-identical
   when called twice with the same inputs. ``rng_seed`` is stored for
   future use; the Wave 1 path is deterministic.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Literal, Optional

import pandas as pd

from research.features.registry import REGISTRY
from research.harness.realistic_fills import (
    FillResult,
    OrderbookSnapshot,
    simulate_fill,
)
from research.harness.strategy_spec import (
    Condition,
    StrategySpec,
    eval_condition,
)
from research.lake.reader import load_game


# --------------------------------------------------------------------------- #
# Public dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Trade:
    """One completed trade: entry + exit (or settlement)."""

    game_id: str
    side: Literal["long_home", "long_away"]
    entry_ts_wall: int
    entry_ts_game_sec: float
    entry_price: float
    entry_fill: FillResult
    exit_ts_wall: int
    exit_ts_game_sec: float
    exit_price: float
    exit_fill: Optional[FillResult]   # None when settled at 0/1 (no execution)
    settle_value: Optional[float]
    contracts_filled: float
    pnl_gross: float
    pnl_net: float
    exit_reason: Literal[
        "explicit_exit",
        "time_stop",
        "end_of_game",
        "settle_at_zero",
        "settle_at_one",
    ]


@dataclass(frozen=True)
class TrialResult:
    """Per-game replay outcome: a list of Trades + summary stats."""

    spec_hash: str
    game_id: str
    n_trades: int
    trades: List[Trade]
    pnl_gross: float
    pnl_net: float
    notional_traded: float
    avg_holding_sec: float
    skipped: List[str]
    rng_seed: int
    synthetic_depth_used: bool = False  # flag when we synthesized depth from a PM mid


# --------------------------------------------------------------------------- #
# Synthetic orderbook construction
# --------------------------------------------------------------------------- #


def synthesize_orderbook_from_mid(
    venue: str,
    side: str,
    mid: float,
    ts_wall: int,
    ts_game_sec: float,
    spread_bps: int = 100,
    depth_per_level: float = 1000.0,
) -> OrderbookSnapshot:
    """Build a synthetic orderbook with a 1¢ half-spread on either side of ``mid``.

    Used when the lake only has a mid price (Polymarket REST is mid-only;
    Kalshi's candle endpoint is mid-only in this cache). 1¢ each side is
    deliberately conservative — real PM books often quote tighter, but a
    backtest that assumes a tight book and then fails to fill live is
    worse than one that overestimates spread by half a cent.
    """
    # 100 bps * mid is one variant of "half spread"; this implementation
    # interprets ``spread_bps`` as a flat probability-points half-spread:
    # 100 bps -> 1¢, i.e. ±0.005 around the mid.
    half_spread = spread_bps / 20_000.0
    bid_top = max(0.001, mid - half_spread)
    ask_top = min(0.999, mid + half_spread)
    bids = tuple(
        (max(0.001, bid_top - i * 0.005), depth_per_level) for i in range(3)
    )
    asks = tuple(
        (min(0.999, ask_top + i * 0.005), depth_per_level) for i in range(3)
    )
    fee_rate_bps = 1000 if venue == "polymarket" else None
    return OrderbookSnapshot(
        venue=venue,  # type: ignore[arg-type]
        side=side,    # type: ignore[arg-type]
        bids=bids,
        asks=asks,
        ts_wall=float(ts_wall),
        ts_game_sec=ts_game_sec,
        fee_rate_bps=fee_rate_bps,
    )


# --------------------------------------------------------------------------- #
# Bar iterator
# --------------------------------------------------------------------------- #


def _build_bars(game: dict, venue: str) -> List[dict]:
    """Return a list of bar dicts in chronological order.

    Each bar dict carries:
      - all pbp columns (home_score, away_score, margin, elapsed_game_sec, ...)
      - ``minute_ts`` (the join key)
      - ``pm_home_mid`` / ``pm_away_mid`` (forward-filled from polymarket_ticks)
      - ``kalshi_yes_bid`` / ``kalshi_yes_ask`` / ``kalshi_mid`` / ``kalshi_team``
        (forward-filled from kalshi_ticks; usually all-None in this 2025-26 cache)

    Bars whose ``elapsed_game_sec`` is NaN/None (pre-game / pre-tip rows)
    are dropped — the strategy contract is keyed off the game clock, and
    a NaN clock would defeat the registry's availability assertion.
    """
    pbp = game["pbp"].sort_values("minute_ts").reset_index(drop=True)
    pm = game["polymarket"]
    kalshi = game["kalshi"]

    # Filter to rows with a real game clock; this drops pre-tip warm-up bars.
    pbp = pbp[pbp["elapsed_game_sec"].notna()].reset_index(drop=True)

    if pbp.empty:
        return []
    # Force minute_ts to int64; an empty / object-dtype column would
    # propagate to the merge below and crash with a dtype mismatch.
    pbp["minute_ts"] = pbp["minute_ts"].astype("int64")

    # Build per-minute PM home/away mids by pivoting on team. The lake
    # labels only the home winner-token; we synthesize the away token by
    # mid_away = 1 - mid_home (binary market complement). Empty PM frames
    # come back with ``object`` dtype on minute_ts (parquet behavior), and
    # merge_asof refuses to join object-dtype keys to int64. Force the
    # dtype here so the join works uniformly across "no quotes" and
    # "rich quotes" games.
    pm_home = (
        pm[pm["team"] == "home"][["minute_ts", "mid"]]
        .rename(columns={"mid": "pm_home_mid"})
    )
    if pm_home.empty:
        pm_home = pd.DataFrame(
            {
                "minute_ts": pd.Series(dtype="int64"),
                "pm_home_mid": pd.Series(dtype="float64"),
            }
        )
    else:
        pm_home["minute_ts"] = pm_home["minute_ts"].astype("int64")
    pm_home = pm_home.sort_values("minute_ts").reset_index(drop=True)

    # Kalshi side: usually empty in this cache; merge_asof on an empty
    # frame chokes on dtype mismatches, so we conditionally skip the merge.
    merged = pd.merge_asof(
        pbp.sort_values("minute_ts"),
        pm_home,
        on="minute_ts",
        direction="backward",
    )
    if not kalshi.empty:
        k_subset = (
            kalshi[["minute_ts", "team", "yes_bid", "yes_ask", "mid"]]
            .rename(
                columns={
                    "team": "kalshi_team",
                    "yes_bid": "kalshi_yes_bid",
                    "yes_ask": "kalshi_yes_ask",
                    "mid": "kalshi_mid",
                }
            )
        )
        k_subset["minute_ts"] = k_subset["minute_ts"].astype("int64")
        k_subset = k_subset.sort_values("minute_ts").reset_index(drop=True)
        merged = pd.merge_asof(
            merged.sort_values("minute_ts"),
            k_subset,
            on="minute_ts",
            direction="backward",
        )
    else:
        for col in (
            "kalshi_team",
            "kalshi_yes_bid",
            "kalshi_yes_ask",
            "kalshi_mid",
        ):
            merged[col] = None

    # Convert to a list of plain dicts so downstream code never sees a
    # pandas Series (subtle bool/NaN comparison footguns).
    bars: List[dict] = []
    for _, row in merged.iterrows():
        d = row.to_dict()
        # Synthesize pm_away_mid as 1 - pm_home_mid (binary market complement).
        pm_home_mid = d.get("pm_home_mid")
        if pm_home_mid is not None and not (
            isinstance(pm_home_mid, float) and math.isnan(pm_home_mid)
        ):
            d["pm_away_mid"] = 1.0 - float(pm_home_mid)
        else:
            d["pm_away_mid"] = None
            d["pm_home_mid"] = None
        bars.append(d)

    return bars


# --------------------------------------------------------------------------- #
# Orderbook resolution
# --------------------------------------------------------------------------- #


def _make_orderbook(
    venue: str,
    side: Literal["long_home", "long_away"],
    bar: dict,
) -> tuple[Optional[OrderbookSnapshot], bool]:
    """Return (snapshot, synthetic_flag) for the requested venue / side at bar.

    Synthetic flag is True when we built the book from a mid rather than
    real bid/ask levels. Returns (None, _) if no usable quote exists at
    this bar (e.g. PM tick missing or kalshi book empty).
    """
    ts_wall = int(bar["minute_ts"])
    ts_game_sec = float(bar["elapsed_game_sec"])

    if venue == "polymarket":
        if side == "long_home":
            mid = bar.get("pm_home_mid")
            book_side = "yes"
        else:
            mid = bar.get("pm_away_mid")
            book_side = "yes"   # the away token's own "yes"
        if mid is None or (isinstance(mid, float) and math.isnan(mid)):
            return None, False
        return synthesize_orderbook_from_mid(
            "polymarket", book_side, float(mid), ts_wall, ts_game_sec
        ), True

    if venue == "kalshi":
        # Kalshi yes-token represents one team (column ``kalshi_team``).
        # If the registered team matches the long side, walk the yes book;
        # otherwise we are buying the NO side, which is equivalent to a yes
        # at price (1 - yes_mid).
        kalshi_team = bar.get("kalshi_team")
        yes_bid = bar.get("kalshi_yes_bid")
        yes_ask = bar.get("kalshi_yes_ask")
        kalshi_mid = bar.get("kalshi_mid")
        # Decide which book "side" we want. The lake only stores one yes-side
        # per ticker; for now we only support the case where kalshi_team
        # matches the long side. Otherwise we fall back to synthesizing from
        # 1 - mid.
        want_team = "home" if side == "long_home" else "away"
        if kalshi_mid is None or (
            isinstance(kalshi_mid, float) and math.isnan(kalshi_mid)
        ):
            return None, False

        # If we have real yes_bid/yes_ask AND the team matches: real book.
        same_team = kalshi_team == want_team
        if same_team and yes_bid is not None and yes_ask is not None:
            # Pad depth via synthesis on top of the top-of-book quote.
            bid_top = float(yes_bid)
            ask_top = float(yes_ask)
            bids = tuple(
                (max(0.001, bid_top - i * 0.005), 1000.0) for i in range(3)
            )
            asks = tuple(
                (min(0.999, ask_top + i * 0.005), 1000.0) for i in range(3)
            )
            return (
                OrderbookSnapshot(
                    venue="kalshi",
                    side="yes",
                    bids=bids,
                    asks=asks,
                    ts_wall=float(ts_wall),
                    ts_game_sec=ts_game_sec,
                    fee_rate_bps=None,
                ),
                False,
            )

        # Otherwise synthesize from mid (possibly inverting for the no side).
        effective_mid = float(kalshi_mid) if same_team else 1.0 - float(kalshi_mid)
        return synthesize_orderbook_from_mid(
            "kalshi", "yes", effective_mid, ts_wall, ts_game_sec
        ), True

    raise ValueError(f"unknown venue: {venue!r}")


# --------------------------------------------------------------------------- #
# Side resolution
# --------------------------------------------------------------------------- #


def _resolve_side(
    spec_side: str, bar: dict, features: dict
) -> Optional[Literal["long_home", "long_away"]]:
    """Translate the spec's abstract side label to a concrete home/away pick.

    Returns None if the feature needed for the decision is unavailable at
    this bar (e.g. ``recent_run_signed`` before its 240s offset).
    """
    if spec_side == "long_home":
        return "long_home"
    if spec_side == "long_away":
        return "long_away"

    margin = bar.get("margin")
    if margin is None or (isinstance(margin, float) and math.isnan(margin)):
        return None

    if spec_side == "long_trailing":
        # Positive margin -> home leading -> trailing is away.
        return "long_away" if float(margin) > 0 else "long_home"

    rr = features.get("recent_run_signed")
    if rr is None or (isinstance(rr, float) and math.isnan(rr)):
        return None

    if spec_side == "long_hot":
        return "long_home" if float(rr) > 0 else "long_away"
    if spec_side == "long_cold":
        return "long_away" if float(rr) > 0 else "long_home"

    raise ValueError(f"unknown spec side: {spec_side!r}")


# --------------------------------------------------------------------------- #
# Context construction
# --------------------------------------------------------------------------- #


def _build_context(
    bar: dict,
    position: Optional[dict],
    features: dict,
) -> dict:
    """Build the eval-context dict for a single bar.

    Position-state keys are present (with None defaults) regardless of
    whether a position is open, so an expression like
    ``pos_side is None`` evaluates safely on every bar.
    """
    elapsed = float(bar["elapsed_game_sec"])
    margin = bar.get("margin")
    total = bar.get("total")
    home_score = bar.get("home_score")
    away_score = bar.get("away_score")

    ctx: dict = {
        # bar columns
        "home_score": float(home_score) if home_score is not None else None,
        "away_score": float(away_score) if away_score is not None else None,
        "elapsed_game_sec": elapsed,
        "margin": float(margin) if margin is not None else None,
        "total": float(total) if total is not None else None,
    }

    # Position-state keys: None when flat, populated when open.
    if position is None:
        ctx.update(
            {
                "pos_side": None,
                "pos_entry_price": None,
                "pos_entry_elapsed": None,
                "pos_size": None,
                "minutes_since_entry": None,
                "unrealized_pnl_bps": None,
            }
        )
    else:
        cur_mark = features.get("__mark_price__") or position["entry_price"]
        ctx.update(
            {
                "pos_side": position["side"],
                "pos_entry_price": position["entry_price"],
                "pos_entry_elapsed": position["entry_game_sec"],
                "pos_size": position["size"],
                "minutes_since_entry": (elapsed - position["entry_game_sec"]) / 60.0,
                "unrealized_pnl_bps": (
                    (cur_mark - position["entry_price"])
                    / max(position["entry_price"], 1e-6)
                    * 10_000.0
                ),
            }
        )

    # Features declared on the spec, resolved by the registry.
    for fname, fval in features.items():
        if fname.startswith("__"):
            continue
        ctx[fname] = fval

    return ctx


# --------------------------------------------------------------------------- #
# Replay engine
# --------------------------------------------------------------------------- #


def replay_game(
    spec: StrategySpec,
    game_id: str,
    rng_seed: int = 0,
    venue_override: Optional[str] = None,
    allow_test: bool = False,
) -> TrialResult:
    """Walk one game's bars, apply ``spec``, return a TrialResult.

    See the module docstring for the full invariant list. The path is
    deterministic; ``rng_seed`` is plumbed through for future use
    (sampling, stochastic execution, etc.) but does not change behaviour
    in Wave 1.

    Parameters
    ----------
    spec : StrategySpec
        Must already have ``.validate()``-ed (we re-call validate here
        defensively so a Codex worker that constructed the spec by hand
        still gets the live-safety guard).
    game_id : str
    rng_seed : int, default 0
    venue_override : str, optional
        For testing. Pass "polymarket" or "kalshi" to override ``spec.venue``.
    allow_test : bool, default False
        Forwarded to ``load_game``. The caller is responsible for setting
        this only when an unlock token has been burned.
    """
    spec.validate()

    venue = venue_override or spec.venue
    if venue == "either":
        # Phase 0 picks one venue per game; "either" defaults to polymarket
        # because that's where the cache density is in this 2025-26 lake.
        venue = "polymarket"

    game = load_game(game_id, allow_test=allow_test)
    bars = _build_bars(game, venue=venue)

    spec_hash = spec.spec_hash()
    trades: List[Trade] = []
    skipped: List[str] = []
    synthetic_used = False

    position: Optional[dict] = None
    # Pre-compute spec hash for the Trade records.

    last_bar_idx = len(bars) - 1
    for i, bar in enumerate(bars):
        # ---- Resolve declared features (with live-availability guard). ----
        game_clock = float(bar["elapsed_game_sec"])
        features: dict = {}
        feature_unavailable = False
        for fname in spec.features:
            try:
                # The registry asserts game_clock >= available_at_offset_sec.
                features[fname] = REGISTRY.compute(fname, bar, game_clock)
            except RuntimeError as exc:
                # Feature not yet knowable -> skip this bar (don't crash).
                feature_unavailable = True
                skipped.append(
                    f"bar@{int(bar['minute_ts'])}: feature {fname!r} unavailable ({exc})"
                )
                break

        if feature_unavailable:
            continue

        # ---- Build context, evaluate exit FIRST if open, then entry. ----
        context = _build_context(bar, position, features)

        # --- Exit path ---
        if position is not None:
            time_stop_fired = (
                game_clock - position["entry_game_sec"] >= spec.time_stop_sec
            )
            explicit_exit = False
            try:
                for cond in spec.exit_conditions:
                    if eval_condition(cond, context):
                        explicit_exit = True
                        break
            except Exception as exc:
                skipped.append(
                    f"bar@{int(bar['minute_ts'])}: exit-condition eval failed ({exc})"
                )

            if explicit_exit or time_stop_fired:
                exit_reason: str = (
                    "explicit_exit" if explicit_exit else "time_stop"
                )

                # The exit price comes from bar t+1's orderbook (one bar
                # of execution latency). If there's no bar t+1, mark-to-
                # market at THIS bar (exit_reason becomes 'end_of_game').
                next_idx = i + 1
                if next_idx < len(bars):
                    exit_bar = bars[next_idx]
                else:
                    exit_bar = bar
                    exit_reason = "end_of_game"

                trade = _close_position(
                    spec=spec,
                    game_id=game_id,
                    venue=venue,
                    position=position,
                    exit_bar=exit_bar,
                    exit_reason=exit_reason,
                    game_meta=game["meta"],
                )
                if trade is not None:
                    trades.append(trade)
                position = None
                # After closing, don't immediately re-enter on the same bar.
                continue

        # --- Entry path ---
        if position is None:
            # Don't enter on the last bar — there's no bar t+1 to source
            # the exit fill from, and same-minute entry/exit would model
            # zero-latency execution (a lookahead violation).
            if i == last_bar_idx:
                continue

            try:
                entry_fires = eval_condition(spec.entry_condition, context)
            except Exception as exc:
                skipped.append(
                    f"bar@{int(bar['minute_ts'])}: entry-condition eval failed ({exc})"
                )
                continue

            if not entry_fires:
                continue

            resolved_side = _resolve_side(spec.side, bar, features)
            if resolved_side is None:
                skipped.append(
                    f"bar@{int(bar['minute_ts'])}: side {spec.side!r} unresolved "
                    f"(feature unavailable or margin missing)"
                )
                continue

            snap, synthetic = _make_orderbook(venue, resolved_side, bar)
            if snap is None:
                skipped.append(
                    f"bar@{int(bar['minute_ts'])}: no {venue} quote for {resolved_side}"
                )
                continue
            synthetic_used = synthetic_used or synthetic

            fill = simulate_fill(
                snap,
                side="buy",
                size=spec.sizing.value,
                latency_ms=250,
                stale_threshold_sec=float("inf"),
                # Determinism: pin now_wall to the bar's minute_ts so
                # FillResult.ts_wall doesn't drift between identical replays.
                now_wall=float(bar["minute_ts"]),
            )

            if fill.kill_reason is not None or not fill.filled:
                skipped.append(
                    f"bar@{int(bar['minute_ts'])}: entry fill killed "
                    f"({fill.kill_reason})"
                )
                continue

            position = {
                "side": resolved_side,
                "size": fill.filled_size,
                "entry_price": fill.avg_price,
                "entry_fill": fill,
                "entry_wall": int(bar["minute_ts"]),
                "entry_game_sec": game_clock,
            }

    # ---- End-of-iteration: if a position is still open, settle. ----
    if position is not None:
        trade = _settle_position(
            spec=spec,
            game_id=game_id,
            position=position,
            last_bar=bars[-1] if bars else None,
            game_meta=game["meta"],
        )
        if trade is not None:
            trades.append(trade)

    # ---- Aggregate. ----
    pnl_gross = sum(t.pnl_gross for t in trades)
    pnl_net = sum(t.pnl_net for t in trades)
    notional = sum(abs(t.contracts_filled * t.entry_price) for t in trades)
    if trades:
        avg_hold = sum(t.exit_ts_game_sec - t.entry_ts_game_sec for t in trades) / len(trades)
    else:
        avg_hold = 0.0

    return TrialResult(
        spec_hash=spec_hash,
        game_id=game_id,
        n_trades=len(trades),
        trades=trades,
        pnl_gross=pnl_gross,
        pnl_net=pnl_net,
        notional_traded=notional,
        avg_holding_sec=avg_hold,
        skipped=skipped,
        rng_seed=rng_seed,
        synthetic_depth_used=synthetic_used,
    )


def replay_games(
    spec: StrategySpec,
    game_ids: List[str],
    rng_seed: int = 0,
    allow_test: bool = False,
) -> List[TrialResult]:
    """Convenience wrapper: replay_game over a list of game_ids.

    Wave 1 doesn't parallelise; run-batch (a later wave) will if needed.
    """
    return [
        replay_game(spec, gid, rng_seed=rng_seed, allow_test=allow_test)
        for gid in game_ids
    ]


# --------------------------------------------------------------------------- #
# Position closure helpers
# --------------------------------------------------------------------------- #


def _close_position(
    spec: StrategySpec,
    game_id: str,
    venue: str,
    position: dict,
    exit_bar: Optional[dict],
    exit_reason: str,
    game_meta,
) -> Optional[Trade]:
    """Build the Trade record for a position closing via execution.

    If ``exit_bar`` is None (the exit fires on the last bar), the position
    is marked-to-market against the entry bar's last available mid; in
    practice the caller should have routed last-bar exits to settlement.
    """
    if exit_bar is None:
        # No t+1 bar -> mark-to-market at entry (degenerate); the caller
        # converts to settlement upstream when at end-of-game with a known
        # outcome.
        return _settle_position(
            spec=spec,
            game_id=game_id,
            position=position,
            last_bar=None,
            game_meta=game_meta,
        )

    snap, _ = _make_orderbook(venue, position["side"], exit_bar)
    if snap is None:
        # No quote at the next bar; fall back to settlement / mark-to-zero.
        return _settle_position(
            spec=spec,
            game_id=game_id,
            position=position,
            last_bar=exit_bar,
            game_meta=game_meta,
            forced_reason=exit_reason if exit_reason in (
                "explicit_exit", "time_stop", "end_of_game"
            ) else None,
        )

    exit_fill = simulate_fill(
        snap,
        side="sell",
        size=position["size"],
        latency_ms=250,
        stale_threshold_sec=float("inf"),
        # Determinism: see entry-side comment.
        now_wall=float(exit_bar["minute_ts"]),
    )

    if not exit_fill.filled:
        # Fall back to settlement-on-failure (rare).
        return _settle_position(
            spec=spec,
            game_id=game_id,
            position=position,
            last_bar=exit_bar,
            game_meta=game_meta,
            forced_reason=exit_reason,
        )

    contracts = min(position["size"], exit_fill.filled_size)
    entry_price = position["entry_price"]
    exit_price = exit_fill.avg_price
    entry_fill: FillResult = position["entry_fill"]

    pnl_gross = (exit_price - entry_price) * contracts
    pnl_net = pnl_gross - entry_fill.fees_paid - exit_fill.fees_paid

    return Trade(
        game_id=game_id,
        side=position["side"],
        entry_ts_wall=position["entry_wall"],
        entry_ts_game_sec=position["entry_game_sec"],
        entry_price=entry_price,
        entry_fill=entry_fill,
        exit_ts_wall=int(exit_bar["minute_ts"]),
        exit_ts_game_sec=float(exit_bar["elapsed_game_sec"]),
        exit_price=exit_price,
        exit_fill=exit_fill,
        settle_value=None,
        contracts_filled=contracts,
        pnl_gross=pnl_gross,
        pnl_net=pnl_net,
        exit_reason=exit_reason,  # type: ignore[arg-type]
    )


def _settle_position(
    spec: StrategySpec,
    game_id: str,
    position: dict,
    last_bar: Optional[dict],
    game_meta,
    forced_reason: Optional[str] = None,
) -> Optional[Trade]:
    """Settle a still-open position against the game's final outcome.

    Settlement does not pay execution fees (the contract simply pays out
    1.0 or 0.0 at the venue). exit_reason is one of
    ``settle_at_one`` / ``settle_at_zero``.
    """
    home_won = None
    try:
        hw = game_meta.get("home_won")
        if hw is not None and not (isinstance(hw, float) and math.isnan(hw)):
            home_won = bool(hw)
    except Exception:
        home_won = None

    if home_won is None:
        # Outcome unknown — mark to last available mid if we have a bar,
        # else stamp with entry price (PnL = 0).
        if last_bar is not None:
            mid = last_bar.get("pm_home_mid")
            if position["side"] == "long_away" and mid is not None:
                mid = 1.0 - float(mid)
            if mid is None:
                mid = position["entry_price"]
        else:
            mid = position["entry_price"]
        exit_price = float(mid)
        settle_value = None
        exit_reason = forced_reason or "end_of_game"
    else:
        won = home_won if position["side"] == "long_home" else (not home_won)
        exit_price = 1.0 if won else 0.0
        settle_value = exit_price
        exit_reason = "settle_at_one" if won else "settle_at_zero"

    if last_bar is None:
        # No bar to mark exit timing against; use entry timing (degenerate).
        exit_wall = position["entry_wall"]
        exit_game_sec = position["entry_game_sec"]
    else:
        exit_wall = int(last_bar["minute_ts"])
        exit_game_sec = float(last_bar["elapsed_game_sec"])

    contracts = position["size"]
    entry_price = position["entry_price"]
    entry_fill: FillResult = position["entry_fill"]
    pnl_gross = (exit_price - entry_price) * contracts
    pnl_net = pnl_gross - entry_fill.fees_paid  # no exit fee on settlement

    return Trade(
        game_id=game_id,
        side=position["side"],
        entry_ts_wall=position["entry_wall"],
        entry_ts_game_sec=position["entry_game_sec"],
        entry_price=entry_price,
        entry_fill=entry_fill,
        exit_ts_wall=exit_wall,
        exit_ts_game_sec=exit_game_sec,
        exit_price=exit_price,
        exit_fill=None,
        settle_value=settle_value,
        contracts_filled=contracts,
        pnl_gross=pnl_gross,
        pnl_net=pnl_net,
        exit_reason=exit_reason,  # type: ignore[arg-type]
    )


__all__ = [
    "Trade",
    "TrialResult",
    "replay_game",
    "replay_games",
    "synthesize_orderbook_from_mid",
]
