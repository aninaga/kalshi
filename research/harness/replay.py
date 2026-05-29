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

import bisect
import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Literal, Optional

import numpy as np
import pandas as pd

from research.features.registry import REGISTRY
from research.harness.cost_profile import get_active_profile
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

# Max forward-fill age for a quote merged onto a bar. Beyond this the quote is
# dropped to NaN rather than presented as the current price. Set from measured
# data (research/scripts/measure_staleness.py, 2026-05-29): PM quotes are
# essentially always fresh (>300s in only ~0.16% of bars) while ~11% of Kalshi
# bars would otherwise forward-fill a quote HOURS (up to ~24h) old. 300s keeps
# every fresh quote and kills the stale tail. (WS4)
STALENESS_BOUND_SEC = 300


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
        "unknown_outcome_marked",
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
    spread_bps: int | None = None,
    depth_per_level: float | None = None,
) -> OrderbookSnapshot:
    """Build a synthetic orderbook with a ``spread_bps`` half-spread around ``mid``.

    When ``spread_bps`` is None (the default), read from the active
    ``CostProfile``. ``PESSIMISTIC`` profile uses 100 (1¢ half-spread, 2¢
    round-trip), preserving the pre-2026-05-28 behavior. ``LIVE_PM`` uses 50
    (0.5¢ half-spread). ``ZERO_COST`` uses 0.

    ``depth_per_level`` similarly defaults to the active profile's value.
    ``CALIBRATED_PM`` sets it from real executed-trade data (~median clip);
    the legacy default (1000) was fabricated and made the impact model a no-op.
    The fee_rate_bps used by the curve piece of the PM taker fee also reads
    from the active profile.
    """
    profile = get_active_profile()
    effective_spread_bps = profile.spread_bps if spread_bps is None else spread_bps
    eff_depth = (
        getattr(profile, "depth_per_level", 1000.0)
        if depth_per_level is None else depth_per_level
    )
    # ``spread_bps`` is a flat probability-points half-spread:
    # 100 bps -> 1¢ (i.e. ±0.005 around the mid); 50 bps -> 0.5¢; 0 -> mid.
    half_spread = effective_spread_bps / 20_000.0
    bid_top = max(0.001, mid - half_spread)
    ask_top = min(0.999, mid + half_spread)
    bids = tuple(
        (max(0.001, bid_top - i * 0.005), eff_depth) for i in range(3)
    )
    asks = tuple(
        (min(0.999, ask_top + i * 0.005), eff_depth) for i in range(3)
    )
    fee_rate_bps = profile.pm_curve_fee_rate_bps if venue == "polymarket" else None
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
# On-court lineup reconstruction + winner-orientation helpers (Wave-1 data-
# quality fixes C1/C3). All derive from REAL lake data (subs table, starters
# metadata, the winner-market mid) — nothing is fabricated.
# --------------------------------------------------------------------------- #


def _split_names(value) -> List[str]:
    """Parse a ``;``-separated player-name string from ``games`` metadata.

    Returns an empty list for None / NaN / empty so callers can detect
    "starters unknown" and emit None rather than a fabricated lineup.
    """
    if value is None:
        return []
    if isinstance(value, float) and math.isnan(value):
        return []
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return []
    return [p.strip() for p in s.split(";") if p.strip()]


def _oncourt_timeline(subs, meta, home_tri: Optional[str], away_tri: Optional[str]):
    """Reconstruct the on-court 5-man units per minute from starters + subs.

    Returns ``(minutes, states)`` where ``states[i] == (frozenset_home,
    frozenset_away)`` is the on-court lineup valid from floored-unix-minute
    ``minutes[i]`` onward. ``minutes`` is sorted ascending and begins with
    ``-inf`` (the tip-off starters). Returns ``([], [])`` when starters are
    missing for either side — the caller then emits ``lineup_sig=None`` rather
    than guessing.

    Point-in-time safe: a substitution at wall-clock ``ts`` only affects bars
    whose ``minute_ts >= floor(ts)``, so a bar never sees a future sub.
    """
    if meta is None:
        return [], []
    starters_home = _split_names(meta.get("starters_home"))
    starters_away = _split_names(meta.get("starters_away"))
    if not starters_home or not starters_away:
        return [], []

    on_home = set(starters_home)
    on_away = set(starters_away)
    minutes: List[float] = [float("-inf")]
    states: List[tuple] = [(frozenset(on_home), frozenset(on_away))]

    if subs is not None and len(subs) > 0:
        sdf = subs.sort_values("ts")
        for r in sdf.itertuples(index=False):
            team = getattr(r, "team", None)
            pin = getattr(r, "player_in", None)
            pout = getattr(r, "player_out", None)
            tgt = on_home if team == home_tri else (
                on_away if team == away_tri else None
            )
            if tgt is not None:
                if isinstance(pout, str) and pout in tgt:
                    tgt.discard(pout)
                if isinstance(pin, str) and pin:
                    tgt.add(pin)
            try:
                m = float(int(getattr(r, "ts")) // 60 * 60)
            except (TypeError, ValueError):
                continue
            minutes.append(m)
            states.append((frozenset(on_home), frozenset(on_away)))
    return minutes, states


def _lineup_sig_at(minute_ts: int, minutes: List[float], states: List[tuple]) -> Optional[float]:
    """Stable signature of the on-court lineup at ``minute_ts``.

    Uses a hashlib digest (NOT Python's builtin ``hash()``, which is salted
    per-process and would break the determinism gate) so identical lineups map
    to identical signatures across runs. None when the lineup is unknown.
    """
    if not minutes:
        return None
    idx = bisect.bisect_right(minutes, float(minute_ts)) - 1
    if idx < 0:
        return None
    fh, fa = states[idx]
    canon = "|".join(sorted(fh)) + "#" + "|".join(sorted(fa))
    return float(int(hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12], 16))


def _kalshi_home_wp(mid, team, home_tri: Optional[str], away_tri: Optional[str]) -> Optional[float]:
    """Orient a Kalshi winner-market mid to a HOME win probability.

    The yes-token's ``team`` says which side it pays; if it's the home team the
    mid IS the home win prob, if it's the away team it's ``1 - mid``. None when
    the mid is missing or the team can't be matched (fixes C1: the key
    ``bar['kalshi_home_winprob']`` had no producer before).
    """
    if mid is None or (isinstance(mid, float) and math.isnan(mid)):
        return None
    try:
        m = float(mid)
    except (TypeError, ValueError):
        return None
    if team == home_tri:
        return m
    if team == away_tri:
        return 1.0 - m
    return None


def _quote_age(minute_ts, tick_ts) -> Optional[int]:
    """Forward-fill age (seconds) of a merged quote; None when no in-bound
    quote was matched (the merge tolerance dropped a stale one)."""
    if tick_ts is None or (isinstance(tick_ts, float) and math.isnan(tick_ts)):
        return None
    try:
        return int(minute_ts) - int(tick_ts)
    except (TypeError, ValueError):
        return None


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
    subs = game.get("subs")
    meta = game.get("meta")

    # Filter to rows with a real game clock; this drops pre-tip warm-up bars.
    pbp = pbp[pbp["elapsed_game_sec"].notna()].reset_index(drop=True)

    if pbp.empty:
        return []
    # Force minute_ts to int64; an empty / object-dtype column would
    # propagate to the merge below and crash with a dtype mismatch.
    pbp["minute_ts"] = pbp["minute_ts"].astype("int64")

    # Derived column: signed scoring-run delta over the last 4 game-minutes.
    # elapsed_game_sec is monotonic non-decreasing within a game; for each row
    # we find the latest earlier row whose elapsed_game_sec <= (now - 240) and
    # take margin[now] - margin[then]. NaN when fewer than 240s have elapsed.
    egs = pbp["elapsed_game_sec"].to_numpy(dtype=float)
    margin_arr = pd.to_numeric(pbp["margin"], errors="coerce").to_numpy(dtype=float)
    targets = egs - 240.0
    k_lookup = np.searchsorted(egs, targets, side="right") - 1
    recent_run = np.full(len(egs), np.nan)
    mask = (targets >= 0) & (k_lookup >= 0)
    if mask.any():
        recent_run[mask] = margin_arr[mask] - margin_arr[k_lookup[mask]]
    pbp["recent_run_signed_4m"] = recent_run

    # Derived column: TRUE cumulative lead changes from tip-off through each
    # bar. The lake's ``lead_changes`` is a PER-MINUTE count (value_counts of
    # lead-change timestamps binned to the minute); the registry feature
    # ``lead_changes_cum`` is contractually cumulative, so we cumsum here —
    # the same load-time-derivation pattern as recent_run_signed_4m. Fixes C2.
    if "lead_changes" in pbp.columns:
        _lc = pd.to_numeric(pbp["lead_changes"], errors="coerce").fillna(0.0)
        pbp["lead_changes_cum"] = _lc.cumsum()
    else:
        pbp["lead_changes_cum"] = np.nan

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
    # Carry the matched quote's own timestamp so per-bar forward-fill age is
    # observable, and bound the fill (tolerance) so a stale quote drops to NaN
    # rather than masquerading as the current price (WS4).
    pm_home["pm_tick_ts"] = pm_home["minute_ts"]
    pm_home = pm_home.sort_values("minute_ts").reset_index(drop=True)

    # Kalshi side: usually empty in this cache; merge_asof on an empty
    # frame chokes on dtype mismatches, so we conditionally skip the merge.
    merged = pd.merge_asof(
        pbp.sort_values("minute_ts"),
        pm_home,
        on="minute_ts",
        direction="backward",
        tolerance=STALENESS_BOUND_SEC,
    )
    # Restrict Kalshi to the WINNER market (series KXNBAGAME) so the merged
    # kalshi_* columns are the moneyline book, not a spread/total market that
    # merge_asof would otherwise grab arbitrarily per minute. This makes the
    # kalshi_home_winprob producer correct (C1) and keeps kalshi-venue fills on
    # the moneyline book. (No-op for the PM-only games where kalshi is empty.)
    if not kalshi.empty and "series" in kalshi.columns:
        _kw = kalshi[kalshi["series"] == "KXNBAGAME"]
        kalshi = _kw if not _kw.empty else kalshi
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
        k_subset["kalshi_tick_ts"] = k_subset["minute_ts"]
        k_subset = k_subset.sort_values("minute_ts").reset_index(drop=True)
        # Bound the fill: ~11% of Kalshi bars would otherwise forward-fill a
        # quote hours old (measured 2026-05-29). Stale quotes drop to NaN.
        merged = pd.merge_asof(
            merged.sort_values("minute_ts"),
            k_subset,
            on="minute_ts",
            direction="backward",
            tolerance=STALENESS_BOUND_SEC,
        )
    else:
        for col in (
            "kalshi_team",
            "kalshi_yes_bid",
            "kalshi_yes_ask",
            "kalshi_mid",
            "kalshi_tick_ts",
        ):
            merged[col] = None

    # Convert to a list of plain dicts so downstream code never sees a
    # pandas Series (subtle bool/NaN comparison footguns).
    # On-court lineup reconstruction for a TRUE lineup signature (fixes C3 — the
    # old lineup_hash was a stub over star COUNTS). Built once per game from the
    # real starters + subs; bars index into it by minute. Stable-hashed so the
    # determinism gate holds.
    home_tri = str(meta.get("home_tri")) if meta is not None and meta.get("home_tri") is not None else None
    away_tri = str(meta.get("away_tri")) if meta is not None and meta.get("away_tri") is not None else None
    _lin_minutes, _lin_states = _oncourt_timeline(subs, meta, home_tri, away_tri)

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
        # Kalshi-implied HOME win prob from the winner-market mid (fixes C1:
        # this key had no producer, so compute_kalshi_implied_wp was always None).
        d["kalshi_home_winprob"] = _kalshi_home_wp(
            d.get("kalshi_mid"), d.get("kalshi_team"), home_tri, away_tri
        )
        # Real on-court lineup signature (None when starters/subs insufficient).
        d["lineup_sig"] = _lineup_sig_at(int(d["minute_ts"]), _lin_minutes, _lin_states)
        # Quote freshness (forward-fill age); None when no in-bound quote (WS4).
        d["pm_quote_age_sec"] = _quote_age(d["minute_ts"], d.get("pm_tick_ts"))
        d["kalshi_quote_age_sec"] = _quote_age(d["minute_ts"], d.get("kalshi_tick_ts"))
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


def _current_mark(bar: dict, venue: str, side: str) -> Optional[float]:
    """Return the current mid-price quote for the position's venue+side,
    used to mark a live position to market.

    For polymarket: pm_home_mid for long_home, pm_away_mid for long_away.
    For kalshi: kalshi_mid if the kalshi_team matches the position side,
                else 1 - kalshi_mid (the NO-token complement).
    Returns None if no usable quote exists at this bar.
    """
    if venue == "polymarket":
        if side == "long_home":
            v = bar.get("pm_home_mid")
        elif side == "long_away":
            v = bar.get("pm_away_mid")
        else:
            return None
    elif venue == "kalshi":
        kmid = bar.get("kalshi_mid")
        if kmid is None or (isinstance(kmid, float) and math.isnan(kmid)):
            return None
        want_team = "home" if side == "long_home" else "away"
        kteam = bar.get("kalshi_team")
        return float(kmid) if kteam == want_team else 1.0 - float(kmid)
    else:
        return None
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return float(v)


def _build_context(
    bar: dict,
    position: Optional[dict],
    features: dict,
    venue: Optional[str] = None,
) -> dict:
    """Build the eval-context dict for a single bar.

    Position-state keys are present (with None defaults) regardless of
    whether a position is open, so an expression like
    ``pos_side is None`` evaluates safely on every bar.

    ``venue`` is required to mark an open position to market for
    ``unrealized_pnl_bps``. If omitted (legacy callers), unrealized_pnl_bps
    falls back to 0 — historically the bug masked all TP/SL exits.
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
        # Mark to market using the current venue+side mid quote.
        cur_mark = None
        if venue is not None:
            cur_mark = _current_mark(bar, venue, position["side"])
        # When no quote exists this bar we genuinely do NOT know the mark, so
        # emit unrealized_pnl_bps=None (NOT a fabricated 0 via the entry price).
        # A PnL-based TP/SL must not "see" break-even on a data-gap bar; the
        # exit loop treats a None mark as "cannot evaluate -> hold" (fixes C6).
        if cur_mark is None:
            unrealized_pnl_bps = None
        else:
            unrealized_pnl_bps = (
                (cur_mark - position["entry_price"])
                / max(position["entry_price"], 1e-6)
                * 10_000.0
            )
        ctx.update(
            {
                "pos_side": position["side"],
                "pos_entry_price": position["entry_price"],
                "pos_entry_elapsed": position["entry_game_sec"],
                "pos_size": position["size"],
                "minutes_since_entry": (elapsed - position["entry_game_sec"]) / 60.0,
                "unrealized_pnl_bps": unrealized_pnl_bps,
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
        context = _build_context(bar, position, features, venue=venue)

        # --- Exit path ---
        if position is not None:
            time_stop_fired = (
                game_clock - position["entry_game_sec"] >= spec.time_stop_sec
            )
            explicit_exit = False
            for cond in spec.exit_conditions:
                try:
                    if eval_condition(cond, context):
                        explicit_exit = True
                        break
                except TypeError:
                    # A condition referenced a None mark (no quote this bar):
                    # a PnL-based exit cannot be evaluated on a data-gap bar, so
                    # treat it as not-fired (hold) instead of faking 0 PnL or
                    # aborting the remaining conditions. Part of C6.
                    continue
                except Exception as exc:
                    skipped.append(
                        f"bar@{int(bar['minute_ts'])}: exit-condition eval failed ({exc})"
                    )
                    break

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

            # Entry executes at bar i+1's orderbook — ONE bar of execution
            # latency, SYMMETRIC with the exit path (which already fills at
            # bar i+1). The signal is *detected* on bar i; filling at bar i's
            # own price gave entries a free 1-bar head-start that exits never
            # got, which manufactured a spurious short-horizon momentum edge
            # (see adversarial review 2026-05-28: id=174 +$24.26 -> -$3.62 once
            # entry latency is charged symmetrically). bars[i+1] is guaranteed
            # to exist here because the `i == last_bar_idx` guard above skips
            # the final bar.
            entry_bar = bars[i + 1]
            snap, synthetic = _make_orderbook(venue, resolved_side, entry_bar)
            if snap is None:
                skipped.append(
                    f"bar@{int(bar['minute_ts'])}: no {venue} quote for {resolved_side} at t+1"
                )
                continue
            synthetic_used = synthetic_used or synthetic

            fill = simulate_fill(
                snap,
                side="buy",
                size=spec.sizing.value,
                latency_ms=250,
                stale_threshold_sec=float("inf"),
                # Determinism: pin now_wall to the fill bar's minute_ts so
                # FillResult.ts_wall doesn't drift between identical replays.
                now_wall=float(entry_bar["minute_ts"]),
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
                "entry_wall": int(entry_bar["minute_ts"]),
                "entry_game_sec": float(entry_bar["elapsed_game_sec"]),
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
        # Outcome genuinely unknown -> mark honestly under a DISTINCT reason so
        # it is auditable and never mistaken for a real 0/1 settlement. (In this
        # cache home_won is present for all 1310 games, so this path is latent.)
        exit_reason = forced_reason or "unknown_outcome_marked"
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
