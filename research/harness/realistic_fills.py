"""
Wave 0 — realistic fill simulator for the research harness.

Decides which side of the cost line every future backtest trial lands on.
Phase -1 EDA's headline result: phenomenon C (long the team on a 8-2/10-2/12-4
scoring run, exit +4 game-minutes later) clears the gate at <= 2.5 cents
round-trip cost (Kalshi-baseline) and FAILS at 4 cents (Polymarket-honest).
The fee asymmetry between the two venues is the load-bearing piece.

This module deliberately re-exports / re-implements the orderbook-walk
algorithm and fee formulas already living in
`kalshi_arbitrage.mock_execution` and the market-impact piece from
`kalshi_arbitrage.risk_engine`. We do NOT rewrite the math; we adapt the
interfaces so the research harness can call them without dragging in the
production async + aiohttp baggage.

Public surface:

- `OrderbookSnapshot`  — normalized snapshot (bids/asks tuples).
- `FillResult`         — outcome of one simulated leg (timing + kill reasons).
- `normalize_orderbook(...)` — accept Kalshi or Polymarket dict shape.
- `simulate_fill(...)` — walk the book, apply impact, compute fees.
- `round_trip_cost_bps(...)` — Phase 0 cost-floor probe convenience.

Fee unit convention
-------------------
`FillResult.fees_paid` is in **dollars** (USDC for PM, USD for Kalshi).
`round_trip_cost_bps` separately exposes `net_cost_in_probability_units`,
defined as `(round-trip dollar cost) / (size * max_payoff_per_contract)`
where `max_payoff_per_contract == 1.0` for both venues. The two unit
systems coexist; do not conflate them. The 'probability units' number is
the headline Phase -1 used (e.g. "C fails at 4 cents").
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
from typing import Literal, Optional, Sequence

import venue_fees

from research.harness.cost_profile import get_active_profile


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderbookSnapshot:
    """Normalized orderbook snapshot.

    Both Kalshi's `{"yes_bids":[{"price","size"},...], "yes_asks":[...]}` and
    Polymarket's `{"bids":[...], "asks":[...]}` shapes get converted to this
    common form before fill simulation.

    Attributes
    ----------
    venue: "kalshi" | "polymarket".
    side: "yes" | "no". For Kalshi YES vs NO; for PM, "yes" maps to the
        home/away token being modeled.
    bids: [(price, size), ...] sorted descending by price.
    asks: [(price, size), ...] sorted ascending by price.
    ts_wall: unix epoch seconds at which this snapshot was taken.
    ts_game_sec: game-clock at snapshot (None if unknown).
    fee_rate_bps: token-specific PM taker-fee-rate (basis points). None for
        Kalshi (Kalshi uses the fixed 7 bps formula).
    """

    venue: Literal["kalshi", "polymarket"]
    side: Literal["yes", "no"]
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    ts_wall: float
    ts_game_sec: Optional[float] = None
    fee_rate_bps: Optional[int] = None


@dataclass(frozen=True)
class FillResult:
    """Result of simulating one leg of a trade.

    Modeled on `tools/dry_run_execution.py::DryRunResult` for taxonomy
    consistency (timing + kill reasons).

    Attributes
    ----------
    filled: True if at least some quantity was filled.
    avg_price: average fill price across levels (NaN if not filled).
    filled_size: actual filled volume in contracts (may be < requested).
    requested_size: input size requested.
    fees_paid: in DOLLARS (USDC for PM, USD for Kalshi). See module docstring.
    slippage_bps: avg-fill-price vs best-of-book * 10000 (signed: positive
        means worse-than-best for the taker).
    kill_reason: one of `None` | "no_liquidity" | "snapshot_stale" |
        "size_zero" | "no_side". `None` if the leg filled.
    fills: per-level [(price, size), ...] actually consumed.
    latency_ms_assumed: latency assumption used.
    ts_wall: unix epoch seconds when the fill was simulated.
    """

    filled: bool
    avg_price: float
    filled_size: float
    requested_size: float
    fees_paid: float
    slippage_bps: float
    kill_reason: Optional[str]
    fills: tuple[tuple[float, float], ...]
    latency_ms_assumed: int
    ts_wall: float


# ---------------------------------------------------------------------------
# Fee primitives — reused from kalshi_arbitrage/mock_execution.py::FeeModel
# but local-copied so this module has no async / aiohttp dependency.
# ---------------------------------------------------------------------------


def _kalshi_taker_fee(price: float, size: float) -> float:
    """Kalshi's exact taker fee: `ceil(0.07 * size * price * (1-price) * 100) / 100`.

    Non-linear; highest at price=0.5. Delegates to ``venue_fees`` (the repo-wide
    canonical schedule, 2026-06-12). Scaled by the active CostProfile's
    ``kalshi_taker_multiplier`` (default 1.0), applied after the cent-ceil to
    preserve the profile knob's historical semantics.
    """
    return (venue_fees.kalshi_taker_fee(price, size)
            * get_active_profile().kalshi_taker_multiplier)


def _polymarket_curve_fee(price: float, size: float, fee_rate_bps: int) -> float:
    """Official Polymarket parabolic taker fee: ``C × (bps/10000) × p × (1−p)``.

    Identical formula to
    `kalshi_arbitrage.mock_execution.FeeModel.polymarket_taker_fee` — verified
    against docs.polymarket.com/trading/fees (2026-06-09). Per-category rate in
    bps: Geopolitics 0, Sports 300, Politics/Finance/Tech/Mentions 400,
    Economics/Culture/Weather/Other 500, Crypto 700. Same shape as Kalshi's
    curve: peaks at p=0.5, ~0 at the extremes. Returns USDC dollars.

    (2026-06-09: replaced the legacy ``(rate/4000)·tv·(p(1−p))²`` shape; trials
    recorded before this date were charged the legacy curve + 2% flat.)
    """
    if fee_rate_bps <= 0:
        return 0.0
    return venue_fees.pm_taker_fee(price, size, fee_rate_bps=fee_rate_bps)


# Legacy knob: the official schedule has NO flat-on-notional piece (verified
# 2026-06-09). Kept as a profile-driven conservatism knob — PESSIMISTIC still
# charges it on top so prior "worst-case" framing stays worst-case; OFFICIAL
# profiles set it to 0.
POLYMARKET_FLAT_TAKER_RATE = 0.02  # 2% of notional, taker side (legacy knob).

# Default PM fee_rate_bps when token-specific live data is unavailable
# (= highest non-crypto official category rate; conservative).
POLYMARKET_DEFAULT_FEE_RATE_BPS = 500


def _polymarket_taker_fee(
    price: float,
    size: float,
    fee_rate_bps: Optional[int] = None,
) -> float:
    """Polymarket's full taker fee: official parabolic + profile flat knob.

    The parabolic piece's ``fee_rate_bps`` and the flat ``%-of-notional``
    conservatism knob are read from the active CostProfile when not
    overridden. The official 2026 schedule has no flat piece; profiles that
    keep it are deliberately over-charging.

    Returns USDC dollars.
    """
    profile = get_active_profile()
    rate = profile.pm_curve_fee_rate_bps if fee_rate_bps is None else fee_rate_bps
    curve = _polymarket_curve_fee(price, size, rate)
    flat = profile.pm_flat_taker_rate * price * size
    return curve + flat


# ---------------------------------------------------------------------------
# Market-impact piece — reused from kalshi_arbitrage/risk_engine.py
# (the Almgren-Chriss-style fractional price adjustment).
# ---------------------------------------------------------------------------


_LINEAR_IMPACT = 0.0001     # 1 bps per 100% of depth
_SQRT_IMPACT = 0.001        # square-root coefficient
_MAX_IMPACT_FRACTION = 0.05  # 5% safety cap


def _market_impact_fraction(size: float, depth: float) -> float:
    """Almgren-Chriss-style fractional price adjustment.

    Mirrors `risk_engine.MarketImpactModel.calculate_impact` (linear +
    sqrt). Returned as a fraction of price (e.g. 0.001 = 10 bps). Applied
    ON TOP of the orderbook walk: the walk captures lit-spread cost; the
    impact captures expected adverse selection.
    """
    if depth <= 0:
        return _MAX_IMPACT_FRACTION
    ratio = size / depth
    impact = _LINEAR_IMPACT * ratio + _SQRT_IMPACT * math.sqrt(ratio)
    return min(impact, _MAX_IMPACT_FRACTION)


# ---------------------------------------------------------------------------
# Orderbook walking — reused from mock_execution._fill_from_asks/_bids
# ---------------------------------------------------------------------------


def _walk_asks(
    asks: Sequence[tuple[float, float]],
    target_size: float,
) -> tuple[list[tuple[float, float]], float]:
    """Walk asks (sorted ascending) to fill a buy. Returns (fills, total_filled)."""
    fills: list[tuple[float, float]] = []
    remaining = target_size
    for price, size in sorted(asks, key=lambda lvl: lvl[0]):
        if remaining <= 0:
            break
        take = min(size, remaining)
        if take > 0:
            fills.append((price, take))
            remaining -= take
    return fills, target_size - remaining


def _walk_bids(
    bids: Sequence[tuple[float, float]],
    target_size: float,
) -> tuple[list[tuple[float, float]], float]:
    """Walk bids (sorted descending) to fill a sell. Returns (fills, total_filled)."""
    fills: list[tuple[float, float]] = []
    remaining = target_size
    for price, size in sorted(bids, key=lambda lvl: lvl[0], reverse=True):
        if remaining <= 0:
            break
        take = min(size, remaining)
        if take > 0:
            fills.append((price, take))
            remaining -= take
    return fills, target_size - remaining


def _avg_price(fills: Sequence[tuple[float, float]]) -> float:
    if not fills:
        return float("nan")
    notional = sum(p * s for p, s in fills)
    total = sum(s for _, s in fills)
    return notional / total if total > 0 else float("nan")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _coerce_level(level) -> Optional[tuple[float, float]]:
    """Coerce one orderbook level (dict or tuple/list) to (price, size)."""
    if isinstance(level, dict):
        price = level.get("price")
        size = level.get("size") if level.get("size") is not None else level.get("quantity")
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        price, size = level[0], level[1]
    else:
        return None
    try:
        return (float(price), float(size))
    except (TypeError, ValueError):
        return None


def normalize_orderbook(
    raw: dict,
    venue: str,
    side: str = "yes",
    fee_rate_bps: Optional[int] = None,
    ts_wall: Optional[float] = None,
    ts_game_sec: Optional[float] = None,
) -> OrderbookSnapshot:
    """Normalize a raw orderbook dict into an `OrderbookSnapshot`.

    Accepts Kalshi shape `{"yes_bids":[{"price","size"},...], "yes_asks":[...]}`
    (or `no_bids`/`no_asks` when `side="no"`) OR Polymarket shape
    `{"bids":[...], "asks":[...]}`. Levels may be dicts with "price"/"size"
    keys or 2-tuples `(price, size)`.

    Sorts bids descending, asks ascending. Drops malformed levels silently.
    """
    venue_lc = venue.lower()
    side_lc = side.lower()
    if venue_lc not in ("kalshi", "polymarket"):
        raise ValueError(f"Unknown venue: {venue!r}")
    if side_lc not in ("yes", "no"):
        raise ValueError(f"Unknown side: {side!r}")

    # Locate bid/ask arrays in the raw dict, accepting either schema.
    if venue_lc == "kalshi":
        bids_raw = raw.get(f"{side_lc}_bids") or raw.get("bids") or []
        asks_raw = raw.get(f"{side_lc}_asks") or raw.get("asks") or []
    else:  # polymarket
        bids_raw = raw.get("bids") or raw.get(f"{side_lc}_bids") or []
        asks_raw = raw.get("asks") or raw.get(f"{side_lc}_asks") or []

    bids: list[tuple[float, float]] = []
    for level in bids_raw:
        coerced = _coerce_level(level)
        if coerced is not None:
            bids.append(coerced)

    asks: list[tuple[float, float]] = []
    for level in asks_raw:
        coerced = _coerce_level(level)
        if coerced is not None:
            asks.append(coerced)

    bids.sort(key=lambda lvl: lvl[0], reverse=True)
    asks.sort(key=lambda lvl: lvl[0])

    return OrderbookSnapshot(
        venue=venue_lc,  # type: ignore[arg-type]
        side=side_lc,    # type: ignore[arg-type]
        bids=tuple(bids),
        asks=tuple(asks),
        ts_wall=ts_wall if ts_wall is not None else time.time(),
        ts_game_sec=ts_game_sec,
        fee_rate_bps=fee_rate_bps,
    )


# ---------------------------------------------------------------------------
# Fee dispatch
# ---------------------------------------------------------------------------


def _fees_for_fills(
    venue: str,
    fills: Sequence[tuple[float, float]],
    fee_rate_bps: Optional[int],
) -> float:
    """Sum venue-appropriate taker fees across the per-level fills."""
    total = 0.0
    if venue == "kalshi":
        for price, size in fills:
            total += _kalshi_taker_fee(price, size)
    elif venue == "polymarket":
        for price, size in fills:
            total += _polymarket_taker_fee(price, size, fee_rate_bps)
    else:
        raise ValueError(f"Unknown venue: {venue!r}")
    return total


# ---------------------------------------------------------------------------
# Main fill simulation
# ---------------------------------------------------------------------------


def simulate_fill(
    snapshot: OrderbookSnapshot,
    side: Literal["buy", "sell"],
    size: float,
    latency_ms: int = 250,
    stale_threshold_sec: float = 10.0,
    now_wall: Optional[float] = None,
) -> FillResult:
    """Simulate filling `size` contracts against the relevant book side.

    - For ``side="buy"``: walks the asks (taker buys at ask).
    - For ``side="sell"``: walks the bids (taker sells at bid).
    - Applies market-impact slippage ON TOP of the walk's lit cost.
    - Computes venue-appropriate taker fee per fill level.
    - Partial fills are allowed: if requested size exceeds book depth, we
      fill what's available and report `filled_size < requested_size`.
      Returning `filled=True` here is intentional — refusing partials is a
      strategy decision, not a fill-model decision.

    Stale-snapshot guard: if `snapshot.ts_wall < now_wall - stale_threshold_sec`,
    returns a kill (`filled=False, kill_reason="snapshot_stale"`). For
    historical backtests, pass `stale_threshold_sec=float("inf")` to
    bypass this check.

    Parameters
    ----------
    snapshot : OrderbookSnapshot
        Normalized orderbook.
    side : "buy" | "sell"
        Direction of the trade.
    size : float
        Requested contract size.
    latency_ms : int, default 250
        Latency to assume between snapshot-time and execution-time. Stored
        on the result for downstream accounting; does NOT shift the
        snapshot itself.
    stale_threshold_sec : float, default 10.0
        Reject snapshot if older than this vs `now_wall`. Pass `inf` for
        historical backtests.
    now_wall : float, optional
        Wall-clock to compare against `snapshot.ts_wall`. Defaults to
        `time.time()`; for tests, pass an explicit value to stay deterministic.

    Returns
    -------
    FillResult
    """
    ts_now = now_wall if now_wall is not None else time.time()

    if size <= 0:
        return FillResult(
            filled=False,
            avg_price=float("nan"),
            filled_size=0.0,
            requested_size=float(size),
            fees_paid=0.0,
            slippage_bps=0.0,
            kill_reason="size_zero",
            fills=tuple(),
            latency_ms_assumed=latency_ms,
            ts_wall=ts_now,
        )

    # Stale-snapshot guard (skipped when threshold is +inf for backtests).
    if math.isfinite(stale_threshold_sec) and snapshot.ts_wall < ts_now - stale_threshold_sec:
        return FillResult(
            filled=False,
            avg_price=float("nan"),
            filled_size=0.0,
            requested_size=float(size),
            fees_paid=0.0,
            slippage_bps=0.0,
            kill_reason="snapshot_stale",
            fills=tuple(),
            latency_ms_assumed=latency_ms,
            ts_wall=ts_now,
        )

    # Pick book side.
    if side == "buy":
        levels = snapshot.asks
        best_price = levels[0][0] if levels else None
        fills, filled_size = _walk_asks(levels, size)
        # Buying — market-impact moves the price UP (worse for buyer).
        impact_sign = +1.0
    elif side == "sell":
        levels = snapshot.bids
        best_price = levels[0][0] if levels else None
        fills, filled_size = _walk_bids(levels, size)
        # Selling — market-impact moves the price DOWN (worse for seller).
        impact_sign = -1.0
    else:
        raise ValueError(f"Unknown side: {side!r} (must be 'buy' or 'sell')")

    if not levels or best_price is None:
        return FillResult(
            filled=False,
            avg_price=float("nan"),
            filled_size=0.0,
            requested_size=float(size),
            fees_paid=0.0,
            slippage_bps=0.0,
            kill_reason="no_side",
            fills=tuple(),
            latency_ms_assumed=latency_ms,
            ts_wall=ts_now,
        )

    if filled_size <= 0:
        return FillResult(
            filled=False,
            avg_price=float("nan"),
            filled_size=0.0,
            requested_size=float(size),
            fees_paid=0.0,
            slippage_bps=0.0,
            kill_reason="no_liquidity",
            fills=tuple(),
            latency_ms_assumed=latency_ms,
            ts_wall=ts_now,
        )

    # Lit-cost avg from the walk.
    avg_lit = _avg_price(fills)

    # Add market-impact on top. Depth is the total of the top-10 levels on
    # the side we're walking (mirrors risk_engine convention).
    top_depth = sum(s for _, s in levels[:10])
    impact_frac = _market_impact_fraction(filled_size, top_depth)
    avg_after_impact = avg_lit * (1.0 + impact_sign * impact_frac)
    # Keep prices inside [0, 1] — these are probability prices.
    avg_after_impact = max(0.0, min(1.0, avg_after_impact))

    # Compute slippage in bps vs best-of-book (signed: positive = worse-for-taker).
    if best_price > 0:
        if side == "buy":
            slippage_bps = (avg_after_impact - best_price) / best_price * 10_000.0
        else:
            slippage_bps = (best_price - avg_after_impact) / best_price * 10_000.0
    else:
        slippage_bps = 0.0

    # Fees: computed on the AT-FILL prices (lit), not on the impact-adjusted
    # avg. Fee schedules in both venues key off the trade price, and the
    # impact is a model of adverse selection, not a different fill price
    # that the venue sees. (We're conservative here — we apply impact as
    # cost-on-top, but bill fees as if execution happened at lit prices.)
    fees_paid = _fees_for_fills(snapshot.venue, fills, snapshot.fee_rate_bps)

    return FillResult(
        filled=True,
        avg_price=avg_after_impact,
        filled_size=filled_size,
        requested_size=float(size),
        fees_paid=fees_paid,
        slippage_bps=slippage_bps,
        kill_reason=None,
        fills=tuple(fills),
        latency_ms_assumed=latency_ms,
        ts_wall=ts_now,
    )


# ---------------------------------------------------------------------------
# Round-trip cost probe (the Phase 0 headline number)
# ---------------------------------------------------------------------------


def round_trip_cost_bps(
    snapshot_at_entry: OrderbookSnapshot,
    snapshot_at_exit: OrderbookSnapshot,
    size: float,
    latency_ms: int = 250,
) -> dict:
    """Convenience: simulate a buy at entry + sell at exit; report round-trip cost.

    Returns a dict with:

    - ``entry_fill``, ``exit_fill``: the underlying `FillResult`s.
    - ``gross_entry_price``, ``gross_exit_price``: avg fill prices.
    - ``round_trip_fee``: sum of fees_paid on both legs (dollars).
    - ``round_trip_slippage``: dollars lost to slippage (avg-price vs
      best-of-book) across both legs.
    - ``net_cost_in_dollars``: total dollar drag = (entry_price - exit_price)
      * filled_size + fees. Positive means the round-trip COST money even
      if prices were unchanged (i.e. you paid the spread + fees).
    - ``net_cost_in_probability_units``: the headline Phase -1 number =
      `net_cost_in_dollars / (filled_size * max_payoff)` where max payoff
      per contract is $1. So this is "cents of probability-mass per
      contract", e.g. 0.025 = 2.5 cents.

    A backtest at $0.025 net_cost_in_probability_units (Kalshi-baseline)
    clears Phase -1's gate; $0.04 (Polymarket-honest) does not.
    """
    # For round-trip cost probes we want to bypass staleness checks — the
    # caller is feeding historical snapshots.
    entry = simulate_fill(
        snapshot_at_entry,
        side="buy",
        size=size,
        latency_ms=latency_ms,
        stale_threshold_sec=float("inf"),
    )
    exit_ = simulate_fill(
        snapshot_at_exit,
        side="sell",
        size=size,
        latency_ms=latency_ms,
        stale_threshold_sec=float("inf"),
    )

    if not entry.filled or not exit_.filled:
        return {
            "entry_fill": entry,
            "exit_fill": exit_,
            "gross_entry_price": entry.avg_price,
            "gross_exit_price": exit_.avg_price,
            "round_trip_fee": entry.fees_paid + exit_.fees_paid,
            "round_trip_slippage": float("nan"),
            "net_cost_in_dollars": float("nan"),
            "net_cost_in_probability_units": float("nan"),
            "kill_reason": entry.kill_reason or exit_.kill_reason,
        }

    filled_size = min(entry.filled_size, exit_.filled_size)
    entry_price = entry.avg_price
    exit_price = exit_.avg_price
    fees = entry.fees_paid + exit_.fees_paid

    # Slippage component: how much did the (impact-adjusted) avg deviate
    # from the best-of-book on each leg? We just report this in dollars
    # for diagnostics — the avg_price already bakes it in to the spread.
    entry_best = snapshot_at_entry.asks[0][0] if snapshot_at_entry.asks else entry_price
    exit_best = snapshot_at_exit.bids[0][0] if snapshot_at_exit.bids else exit_price
    slippage_dollars = (
        (entry_price - entry_best) * filled_size
        + (exit_best - exit_price) * filled_size
    )

    # If prices were unchanged between entry and exit, this is the pure
    # round-trip cost of crossing the spread twice + fees.
    net_cost_dollars = (entry_price - exit_price) * filled_size + fees

    # Max payoff per contract on a binary 0/1 market is $1.
    max_payoff_total = filled_size * 1.0
    net_cost_in_probability_units = (
        net_cost_dollars / max_payoff_total if max_payoff_total > 0 else float("nan")
    )

    return {
        "entry_fill": entry,
        "exit_fill": exit_,
        "gross_entry_price": entry_price,
        "gross_exit_price": exit_price,
        "round_trip_fee": fees,
        "round_trip_slippage": slippage_dollars,
        "net_cost_in_dollars": net_cost_dollars,
        "net_cost_in_probability_units": net_cost_in_probability_units,
        "kill_reason": None,
    }


__all__ = [
    "OrderbookSnapshot",
    "FillResult",
    "normalize_orderbook",
    "simulate_fill",
    "round_trip_cost_bps",
    "POLYMARKET_FLAT_TAKER_RATE",
    "POLYMARKET_DEFAULT_FEE_RATE_BPS",
]
