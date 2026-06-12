"""research.lab.execution — realistic fills, THE binding capital-risk primitive.

The artifact that falsely certified the NBA totals edge was an at-the-money fill
assumed at exactly 0.50 against a *fabricated* interpolated strike where
P(over)=0.5 by construction (see ``research/TOTALS_REFINE_FINDINGS.md`` /
``research/scripts/totals_realistic.py``). Realistic execution is the WHOLE POINT
of this module: every fill snaps to a *listed* strike on the real ladder, fills at
that strike's REAL quoted probability (never 0.50), crosses a half-spread to take
liquidity, and pays the venue taker fee.

The fill math is ported from ``research/scripts/totals_realistic.py``; fees come
from ``venue_fees`` (repo root), the canonical schedule shared with
``kalshi_arbitrage`` — single source of truth, golden-tested against the
published schedules:

    all_in_price = fill_mid + half_spread + fee

where

  * ``fill_mid`` is the real quoted P(side) of the nearest listed strike,
    interpolated to the entry timestamp (for "under"/"short" sides it is
    ``1 - P(over/cover)``), NOT 0.50;
  * ``half_spread`` is the cost of crossing to take liquidity (default 1.5c);
  * ``fee`` is the official venue taker fee — Polymarket
    ``C x (category_bps/10000) x p x (1-p)`` (sports 300 bps; makers $0), or
    Kalshi ``ceil_cents(0.07 x C x p x (1-p))``.

FEE CORRECTION (2026-06-12): until this date this module charged a flat 2% of
notional on every Polymarket taker fill plus a legacy curve — a fee that does
NOT exist in the official schedule (docs.polymarket.com/trading/fees, verified
2026-06-09/12). It over-charged ATM NBA fills ~2.4x and tail fills ~14x; every
lab verdict recorded before 2026-06-12 paid it. Reproduce historical memos with
``FillModel(legacy_fees=True)``; see ``venue_fees.legacy_pm_taker_fee``.

``FillModel.fill(panel, ts, side) -> FillResult`` is the public surface.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import venue_fees
from research.lab.types import (
    FillResult,
    Panel,
    SHORT_SIDES,
    WINNER,
    is_short_side,
)

# Legacy re-exports (pre-2026-06-12 memo reproduction ONLY; the official
# schedule has no flat-on-notional piece).
PM_FLAT_TAKER_RATE = venue_fees.LEGACY_PM_FLAT_TAKER_RATE
PM_CURVE_FEE_RATE_BPS = venue_fees.LEGACY_PM_CURVE_FEE_RATE_BPS

# Sides that bet the COMPLEMENT of the ladder's quoted P(over/cover) — i.e. the
# line is too HIGH (final outcome < strike). De-duplicated 2026-06-12: this is
# now an ALIAS of the single source of truth ``research.lab.types.SHORT_SIDES``
# (kept for the ``maker_fill_study`` import that already binds this name).
# ``long_away`` lives there now (audit defect C3): it bets the away winner — the
# complement of P(home win) — and was previously in NEITHER set, so it filled
# LONG (paying the home/favorite price) while settling as the away bet,
# fabricating a fake ~+84c/contract edge on longshot fades.
_SHORT_SIDES = SHORT_SIDES


def _interp_at(ts: float, xp: np.ndarray, fp: np.ndarray) -> float:
    """As-of value of ``fp`` at ``ts`` over ``xp`` (backward fill, no look-ahead).

    A fill at entry time ``ts`` must NEVER borrow a quote that printed AFTER
    ``ts``: ``np.interp`` over all samples would bridge to a later (possibly
    near-settlement) price and bleed the future into the entry. We restrict to
    FINITE samples whose ``xp <= ts`` and take the last-known among them (true
    as-of / ffill — matching the ported ``totals_realistic._load`` ffill, which
    also skipped NaNs).

    Returns NaN when no finite sample is at-or-before ``ts`` (every quote is
    later, or all are NaN): the value is genuinely unknown as-of ``ts`` and the
    caller raises rather than fabricating a price from the future.
    """
    fp = np.asarray(fp, dtype=float)
    xp = np.asarray(xp, dtype=float)
    mask = np.isfinite(fp) & np.isfinite(xp) & (xp <= ts)
    if not mask.any():
        return float("nan")
    xs, ys = xp[mask], fp[mask]
    # With future samples excluded, ts is at/after the right edge, so np.interp
    # clamps to the last finite at-or-before sample — exactly the as-of/ffill.
    order = np.argsort(xs)
    return float(np.interp(ts, xs[order], ys[order]))


@dataclass
class FillModel:
    """Realistic execution model: snap-to-strike + half-spread + taker fee.

    Parameters
    ----------
    half_spread : float
        Cost of crossing to take liquidity, in probability units (default 1.5c).
        The venues' min tick is 1c and the measured near-ATM one-step |dprob| is
        ~2c, so 1.5c is the pre-registered central estimate (sweep via
        :func:`cost_sweep`).
    venue : str
        Fee schedule: ``"polymarket"`` (official per-category parabolic) or
        ``"kalshi"`` (``ceil_cents(0.07 x C x p x (1-p))``).
    category : str
        Polymarket fee category for this panel's markets (default "sports" —
        the NBA panels). Ignored for venue="kalshi". See
        ``venue_fees.PM_CATEGORY_FEE_RATE_BPS``.
    legacy_fees : bool
        Charge the retired pre-2026-06-12 PM fee (flat 2% of notional + old
        curve) instead of the official schedule. ONLY for reproducing
        historical memos/verdicts.
    """

    half_spread: float = 0.015
    venue: str = "polymarket"
    category: str = "sports"
    legacy_fees: bool = False

    def fee(self, price: float, size: float = 1.0) -> float:
        """Taker fee in dollars per ``size`` contracts at ``price`` (prob units)."""
        v = self.venue.lower()
        if v == "kalshi":
            return venue_fees.kalshi_taker_fee(price, size)
        if v == "polymarket":
            if self.legacy_fees:
                return venue_fees.legacy_pm_taker_fee(price, size)
            return venue_fees.pm_taker_fee(price, size, category=self.category)
        raise ValueError(f"Unknown venue: {self.venue!r} (kalshi|polymarket)")

    def fill(self, panel: Panel, ts: float, side: str, *,
             strike: float | None = None) -> FillResult:
        """Realistic fill for taking ``side`` on ``panel`` at entry time ``ts``.

        Snaps to the nearest LISTED strike on ``panel.ladder`` (nearest to the
        implied level ``panel.mid`` interpolated at ``ts``), reads that strike's
        REAL quoted probability interpolated at ``ts`` (never 0.50), crosses the
        half-spread, and bills the taker fee.

        ``strike`` PINS the fill to that exact listed strike instead of
        re-snapping at fill time. The re-snap is correct for ATM strategies but
        hazardous for band/strike-conditioned ones: between the signal bar and
        the i+1 fill the mid can cross a bucket midpoint, flipping the snap and
        silently executing the OPPOSITE exposure (found by the fable analyst
        lane, 2026-06-09: 22.6% of unguarded extreme-band fills inverted).
        A pinned strike must already be listed on the ladder — pinning to an
        unlisted strike raises rather than fabricating a quote.

        The winner market has no strike ladder; there the "strike" is the 0.5
        crossing and ``fill_mid`` is the interpolated win-prob (or its complement
        for a short side).

        Raises ``ValueError`` for any ``side`` label in NEITHER the over nor the
        short taxonomy — never silently default a fill to LONG (audit defect C3).
        """
        if panel.n == 0:
            raise ValueError("cannot fill on an empty panel")

        xp = np.asarray(panel.minute_ts, dtype=float)
        # Fail loud on an unknown label rather than defaulting to LONG (C3).
        short = is_short_side(side)

        # --- Winner market: no ladder; the level IS the price. -------------
        if panel.market == WINNER or not panel.ladder:
            p = _interp_at(ts, xp, panel.mid)
            if not np.isfinite(p):
                raise ValueError("winner panel has no finite mid to fill against")
            p = min(max(p, 0.01), 0.99)
            fill_mid = (1.0 - p) if short else p
            all_in = min(fill_mid + self.half_spread, 0.999)
            return FillResult(strike=0.5, fill_mid=fill_mid,
                              half_spread=self.half_spread, fee=self.fee(all_in))

        # --- Ladder markets: pinned strike, or snap to the listed strike. ---
        strikes = np.array(sorted(panel.ladder.keys()), dtype=float)
        if strike is not None:
            si = int(np.argmin(np.abs(strikes - float(strike))))
            if abs(float(strikes[si]) - float(strike)) > 1e-9:
                raise ValueError(
                    f"pinned strike {strike} is not listed on the ladder "
                    f"(listed: {strikes.tolist()})")
        else:
            implied = _interp_at(ts, xp, panel.mid)
            if not np.isfinite(implied):
                raise ValueError("panel has no finite mid to snap a strike to")
            si = int(np.argmin(np.abs(strikes - implied)))
        strike_used = float(strikes[si])

        p_over = _interp_at(ts, xp, panel.ladder[strike_used])
        if not np.isfinite(p_over):
            raise ValueError(f"strike {strike_used} has no finite quote to fill against")
        p_over = min(max(p_over, 0.01), 0.99)

        fill_mid = (1.0 - p_over) if short else p_over
        all_in = min(fill_mid + self.half_spread, 0.999)
        return FillResult(strike=strike_used, fill_mid=fill_mid,
                          half_spread=self.half_spread, fee=self.fee(all_in))


def cost_sweep(values=(0.0, 0.01, 0.015, 0.02, 0.025)) -> list[FillModel]:
    """Return one :class:`FillModel` per half-spread in ``values``.

    Used to probe sensitivity of an edge to execution cost. The returned list is
    ordered by ascending half-spread (i.e. ascending all-in cost), so a
    monotone-decreasing PnL across the sweep is the expected, honest signature.
    """
    return [FillModel(half_spread=float(v)) for v in sorted(values)]


# Module-level default instance: the realistic execution every backtest uses.
REALISTIC: FillModel = FillModel()

__all__ = ["FillModel", "cost_sweep", "REALISTIC",
           "PM_FLAT_TAKER_RATE", "PM_CURVE_FEE_RATE_BPS",
           "SHORT_SIDES", "is_short_side"]
