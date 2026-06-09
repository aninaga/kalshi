"""research.lab.execution — realistic fills, THE binding capital-risk primitive.

The artifact that falsely certified the NBA totals edge was an at-the-money fill
assumed at exactly 0.50 against a *fabricated* interpolated strike where
P(over)=0.5 by construction (see ``research/TOTALS_REFINE_FINDINGS.md`` /
``research/scripts/totals_realistic.py``). Realistic execution is the WHOLE POINT
of this module: every fill snaps to a *listed* strike on the real ladder, fills at
that strike's REAL quoted probability (never 0.50), crosses a half-spread to take
liquidity, and pays the venue taker fee.

The fill math is ported from ``research/scripts/totals_realistic.py`` and mirrors
``research/harness/realistic_fills.py`` + ``kalshi_arbitrage/mock_execution.py``
``FeeModel``:

    all_in_price = fill_mid + half_spread + fee

where

  * ``fill_mid`` is the real quoted P(side) of the nearest listed strike,
    interpolated to the entry timestamp (for "under"/"short" sides it is
    ``1 - P(over/cover)``), NOT 0.50;
  * ``half_spread`` is the cost of crossing to take liquidity (default 1.5c);
  * ``fee`` is the Polymarket taker fee = flat 2% of notional + a small curve
    piece (or the Kalshi non-linear formula).

``FillModel.fill(panel, ts, side) -> FillResult`` is the public surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP

import numpy as np

from research.lab.types import FillResult, Panel, WINNER

# ---------------------------------------------------------------------------
# Fee constants (mirror kalshi_arbitrage.mock_execution.FeeModel /
# research.harness.realistic_fills). The flat 2% taker on notional applies to
# EVERY Polymarket taker fill; the per-token curve piece is tiny near ATM but
# included for honesty. Omitting the flat piece understates PM cost by ~an order
# of magnitude and is exactly the error that falsely certified totals.
# ---------------------------------------------------------------------------
PM_FLAT_TAKER_RATE = 0.02
PM_CURVE_FEE_RATE_BPS = 1000

# Sides that bet the COMPLEMENT of the ladder's quoted P(over/cover) — i.e. the
# line is too HIGH (final outcome < strike). Must stay in sync with
# strategy._OVER_SIDES (its complement): cover_away bets below, cover_home above.
_SHORT_SIDES = frozenset({"under", "short", "short_home", "short_away", "no", "sell", "cover_away"})


def _interp_at(ts: float, xp: np.ndarray, fp: np.ndarray) -> float:
    """Interpolate ``fp`` at ``ts`` over ``xp``, skipping NaNs.

    ``np.interp`` propagates NaN through any bracketing NaN; the ported
    ``totals_realistic._load`` instead used only the finite quotes (ffill /
    last-known). Mirror that: interpolate over the finite samples only. Returns
    NaN only when *every* sample is NaN.
    """
    fp = np.asarray(fp, dtype=float)
    mask = np.isfinite(fp) & np.isfinite(xp)
    if not mask.any():
        return float("nan")
    return float(np.interp(ts, xp[mask], fp[mask]))


def _pm_taker_fee(price: float, size: float = 1.0) -> float:
    """Polymarket taker fee in dollars per contract (curve + flat 2% notional).

    Identical formula to ``mock_execution.FeeModel.polymarket_taker_fee`` and
    ``realistic_fills._polymarket_taker_fee``.
    """
    p = Decimal(str(price))
    c = Decimal(str(size))
    trade_value = p * c
    fee_rate = Decimal(PM_CURVE_FEE_RATE_BPS) / Decimal("4000")
    curve = trade_value * fee_rate * (p * (Decimal("1") - p)) ** Decimal("2")
    flat = trade_value * Decimal(str(PM_FLAT_TAKER_RATE))
    fee = (curve + flat).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(fee)


def _kalshi_taker_fee(price: float, size: float = 1.0) -> float:
    """Kalshi non-linear taker fee: ``ceil(0.07 * size * p * (1-p) * 100) / 100``.

    Identical formula to ``mock_execution.FeeModel.kalshi_taker_fee``.
    """
    p = Decimal(str(price))
    c = Decimal(str(size))
    raw = Decimal("0.07") * c * p * (Decimal("1") - p)
    fee = (raw * 100).to_integral_value(rounding=ROUND_UP) / Decimal("100")
    return float(fee)


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
        Fee schedule: ``"polymarket"`` (2% flat + curve) or ``"kalshi"``.
    """

    half_spread: float = 0.015
    venue: str = "polymarket"

    def fee(self, price: float, size: float = 1.0) -> float:
        """Taker fee in dollars per ``size`` contracts at ``price`` (prob units)."""
        v = self.venue.lower()
        if v == "kalshi":
            return _kalshi_taker_fee(price, size)
        if v == "polymarket":
            return _pm_taker_fee(price, size)
        raise ValueError(f"Unknown venue: {self.venue!r} (kalshi|polymarket)")

    def fill(self, panel: Panel, ts: float, side: str) -> FillResult:
        """Realistic fill for taking ``side`` on ``panel`` at entry time ``ts``.

        Snaps to the nearest LISTED strike on ``panel.ladder`` (nearest to the
        implied level ``panel.mid`` interpolated at ``ts``), reads that strike's
        REAL quoted probability interpolated at ``ts`` (never 0.50), crosses the
        half-spread, and bills the taker fee.

        The winner market has no strike ladder; there the "strike" is the 0.5
        crossing and ``fill_mid`` is the interpolated win-prob (or its complement
        for a short side).
        """
        if panel.n == 0:
            raise ValueError("cannot fill on an empty panel")

        xp = np.asarray(panel.minute_ts, dtype=float)
        short = side.strip().lower() in _SHORT_SIDES

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

        # --- Ladder markets (total / spread): snap to the listed strike. ---
        strikes = np.array(sorted(panel.ladder.keys()), dtype=float)
        implied = _interp_at(ts, xp, panel.mid)
        if not np.isfinite(implied):
            raise ValueError("panel has no finite mid to snap a strike to")
        si = int(np.argmin(np.abs(strikes - implied)))
        strike = float(strikes[si])

        p_over = _interp_at(ts, xp, panel.ladder[strike])
        if not np.isfinite(p_over):
            raise ValueError(f"strike {strike} has no finite quote to fill against")
        p_over = min(max(p_over, 0.01), 0.99)

        fill_mid = (1.0 - p_over) if short else p_over
        all_in = min(fill_mid + self.half_spread, 0.999)
        return FillResult(strike=strike, fill_mid=fill_mid,
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
           "PM_FLAT_TAKER_RATE", "PM_CURVE_FEE_RATE_BPS"]
