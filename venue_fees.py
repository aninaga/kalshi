"""venue_fees — THE canonical venue fee schedules (single source of truth).

Every other fee implementation in this repo must delegate here:
``kalshi_arbitrage/mock_execution.py`` (FeeModel), ``research/lab/execution.py``
(FillModel), ``research/harness/realistic_fills.py`` (profile wrappers), and
``research/scripts/{maker_fill_study,totals_realistic,spread_realistic}.py``.
Top-level stdlib-only module BY DESIGN: research code must be importable
without aiohttp (see realistic_fills), and the installed package excludes
``research*`` (pyproject), so neither side can host the canonical copy.

History: until 2026-06-12 the repo carried 5+ divergent fee models. The
research lab charged a flat 2%-of-notional Polymarket taker fee that DOES NOT
EXIST in the official schedule (plus a legacy curve), over-charging ATM NBA
taker fills ~2.4x and tail fills ~14x, and charged makers either 0 or the FULL
Kalshi taker fee. The arb side fixed PM takers on 2026-06-09; this module
unifies both sides and adds the per-series Kalshi maker schedule.

Sources (all verified 2026-06-12 unless noted):
- Polymarket taker: ``shares x (bps/10000) x p x (1-p)`` with PER-CATEGORY
  rates — Geopolitics 0, Sports 300, Politics/Finance/Tech/Mentions 400,
  Economics/Culture/Weather/Other 500, Crypto 700 bps. Makers pay $0 and earn
  a 20-25% rebate of counterparty fees (not credited here). Fees round
  HALF_UP to 5 decimals with a 0.00001 USDC minimum when nonzero.
  docs.polymarket.com/trading/fees (first verified 2026-06-09),
  help.polymarket.com/en/articles/13364478-trading-fees.
  NOTE: per-token live rates exist (clob.polymarket.com/fee-rate) — pass
  ``fee_rate_bps`` when you have one; categories are the static fallback.
  Fee rollout is by market-creation date (e.g. Sports markets created on/after
  2026-03-30); historical-period markets may have been fee-free.
- Kalshi taker: ``ceil_cents(0.07 x C x P x (1-P))`` — kalshi.com/fee-schedule,
  help.kalshi.com/en/articles/13823805-fees. Batch-level ceil to the next cent.
- Kalshi maker: ~quarter of taker, ``ceil_cents(0.0175 x C x P x (1-P))``,
  charged ONLY on series whose ``fee_type`` is "quadratic_with_maker_fees"
  (charged on execution of the resting order). Per-series semantics from the
  venue API ``/trade-api/v2/series/<ticker>`` (fetched 2026-06-12):
  KXNBA + KXNBASERIES = quadratic_with_maker_fees x1.0;
  KXBTCD / KXETHD / KXHIGHNY = quadratic (taker-only) x1.0;
  KXINX (Financials) = quadratic x0.5. ``fee_multiplier`` scales the factor.
- Legacy (research memos before 2026-06-12): flat 2% of notional + curve
  ``tv x (1000bps/4000) x (p(1-p))^2``. Kept ONLY for reproducing old memos
  behind explicit ``legacy_pm_taker_fee`` / ``taker_fee(..., legacy=True)``.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
from typing import Optional

__all__ = [
    "KALSHI_TAKER_FACTOR", "KALSHI_MAKER_FACTOR",
    "KALSHI_FEE_TYPE_QUADRATIC", "KALSHI_FEE_TYPE_WITH_MAKER",
    "KALSHI_SERIES_FEE_SNAPSHOT", "kalshi_series_fee_params",
    "PM_CATEGORY_FEE_RATE_BPS", "PM_DEFAULT_FEE_RATE_BPS",
    "PM_MAKER_FEE_RATE", "PM_MAKER_REBATE_RANGE",
    "LEGACY_PM_FLAT_TAKER_RATE", "LEGACY_PM_CURVE_FEE_RATE_BPS",
    "kalshi_taker_fee", "kalshi_maker_fee",
    "pm_taker_fee", "pm_maker_fee", "legacy_pm_taker_fee",
    "taker_fee", "maker_fee",
]

# --------------------------------------------------------------------------
# Kalshi schedule
# --------------------------------------------------------------------------
KALSHI_TAKER_FACTOR = Decimal("0.07")
KALSHI_MAKER_FACTOR = Decimal("0.0175")   # ~= quarter of taker, per published schedule

KALSHI_FEE_TYPE_QUADRATIC = "quadratic"                    # taker-only series
KALSHI_FEE_TYPE_WITH_MAKER = "quadratic_with_maker_fees"   # taker + maker series

# Snapshot of per-series fee semantics from the venue API (fetched 2026-06-12).
# Format: series_ticker -> (fee_type, fee_multiplier). The live API is the
# source of truth for series not pinned here.
KALSHI_SERIES_FEE_SNAPSHOT: dict[str, tuple[str, float]] = {
    "KXNBA":        (KALSHI_FEE_TYPE_WITH_MAKER, 1.0),
    "KXNBASERIES":  (KALSHI_FEE_TYPE_WITH_MAKER, 1.0),
    "KXBTCD":       (KALSHI_FEE_TYPE_QUADRATIC, 1.0),
    "KXETHD":       (KALSHI_FEE_TYPE_QUADRATIC, 1.0),
    "KXHIGHNY":     (KALSHI_FEE_TYPE_QUADRATIC, 1.0),
    "KXINX":        (KALSHI_FEE_TYPE_QUADRATIC, 0.5),
}


def kalshi_series_fee_params(series_ticker: str) -> tuple[str, float]:
    """(fee_type, fee_multiplier) for a series; conservative default if unknown.

    Unknown series default to (quadratic_with_maker_fees, 1.0) — the
    over-charging side — so an unpinned series can only make research verdicts
    MORE pessimistic, never inflate an edge.
    """
    return KALSHI_SERIES_FEE_SNAPSHOT.get(
        series_ticker.upper(), (KALSHI_FEE_TYPE_WITH_MAKER, 1.0))


def _kalshi_quadratic(price: float, size: float, factor: Decimal,
                      fee_multiplier: float) -> float:
    """Kalshi's quadratic fee with batch-level ceil to the next cent."""
    p = Decimal(str(price))
    c = Decimal(str(size))
    raw = factor * Decimal(str(fee_multiplier)) * c * p * (Decimal("1") - p)
    fee = (raw * 100).to_integral_value(rounding=ROUND_UP) / Decimal("100")
    return float(fee)


def kalshi_taker_fee(price: float, size: float = 1.0, *,
                     fee_multiplier: float = 1.0) -> float:
    """Kalshi taker fee in dollars: ``ceil_cents(0.07 x mult x C x P x (1-P))``."""
    return _kalshi_quadratic(price, size, KALSHI_TAKER_FACTOR, fee_multiplier)


def kalshi_maker_fee(price: float, size: float = 1.0, *,
                     fee_type: str = KALSHI_FEE_TYPE_WITH_MAKER,
                     fee_multiplier: float = 1.0) -> float:
    """Kalshi maker fee in dollars; $0 on plain-quadratic (taker-only) series.

    ``ceil_cents(0.0175 x mult x C x P x (1-P))`` on series whose fee_type is
    "quadratic_with_maker_fees" (e.g. KXNBA); 0.0 otherwise (e.g. KXBTCD,
    KXETHD, KXHIGHNY). Charged only when the resting order executes.
    """
    if fee_type != KALSHI_FEE_TYPE_WITH_MAKER:
        return 0.0
    return _kalshi_quadratic(price, size, KALSHI_MAKER_FACTOR, fee_multiplier)


# --------------------------------------------------------------------------
# Polymarket schedule (official, 2026)
# --------------------------------------------------------------------------
PM_CATEGORY_FEE_RATE_BPS: dict[str, int] = {
    "geopolitics": 0,
    "sports": 300,
    "politics": 400,
    "finance": 400,
    "tech": 400,
    "mentions": 400,
    "economics": 500,
    "culture": 500,
    "weather": 500,
    "other": 500,
    "crypto": 700,
}
# Conservative static fallback when neither a live per-token rate nor a
# category is known (= highest non-crypto category).
PM_DEFAULT_FEE_RATE_BPS = 500

PM_MAKER_FEE_RATE = 0.0
# Makers additionally EARN a daily rebate of 20-25% of counterparty taker fees.
# Documented for sizing studies; never auto-credited in fee functions.
PM_MAKER_REBATE_RANGE = (0.20, 0.25)


def pm_taker_fee(price: float, size: float = 1.0, *,
                 fee_rate_bps: Optional[int] = None,
                 category: Optional[str] = None) -> float:
    """Official Polymarket taker fee in USDC: ``C x (bps/10000) x P x (1-P)``.

    Precedence: explicit ``fee_rate_bps`` (e.g. from the live per-token
    endpoint) > ``category`` lookup > ``PM_DEFAULT_FEE_RATE_BPS``. Rate 0
    (e.g. Geopolitics, or pre-rollout markets) is a genuine zero fee.
    Rounds HALF_UP to 5 decimals with a 0.00001 minimum when nonzero.
    """
    if fee_rate_bps is None:
        if category is not None:
            try:
                fee_rate_bps = PM_CATEGORY_FEE_RATE_BPS[category.strip().lower()]
            except KeyError:
                raise ValueError(
                    f"Unknown Polymarket category: {category!r} "
                    f"(known: {sorted(PM_CATEGORY_FEE_RATE_BPS)})") from None
        else:
            fee_rate_bps = PM_DEFAULT_FEE_RATE_BPS
    if not fee_rate_bps or fee_rate_bps <= 0:
        return 0.0
    p = Decimal(str(price))
    c = Decimal(str(size))
    rate = Decimal(int(fee_rate_bps)) / Decimal("10000")
    raw = c * rate * p * (Decimal("1") - p)
    fee = raw.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
    if raw > 0 and fee < Decimal("0.00001"):
        fee = Decimal("0.00001")   # minimum charged per the fee schedule
    return float(fee)


def pm_maker_fee(price: float, size: float = 1.0) -> float:
    """Polymarket maker fee: $0 (makers are never charged; rebate not credited)."""
    return 0.0


# --------------------------------------------------------------------------
# Legacy research fee (pre-2026-06-12 memos ONLY)
# --------------------------------------------------------------------------
LEGACY_PM_FLAT_TAKER_RATE = 0.02
LEGACY_PM_CURVE_FEE_RATE_BPS = 1000


def legacy_pm_taker_fee(price: float, size: float = 1.0) -> float:
    """The retired research-lab PM taker fee: flat 2% of notional + old curve.

    Does NOT exist in any official schedule. Reproduces the fee charged by
    research memos/verdicts recorded before 2026-06-12 (and arb trials before
    2026-06-09). Use only to replicate historical numbers.
    """
    p = Decimal(str(price))
    c = Decimal(str(size))
    trade_value = p * c
    fee_rate = Decimal(LEGACY_PM_CURVE_FEE_RATE_BPS) / Decimal("4000")
    curve = trade_value * fee_rate * (p * (Decimal("1") - p)) ** Decimal("2")
    flat = trade_value * Decimal(str(LEGACY_PM_FLAT_TAKER_RATE))
    fee = (curve + flat).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(fee)


# --------------------------------------------------------------------------
# Venue dispatchers
# --------------------------------------------------------------------------
def taker_fee(venue: str, price: float, size: float = 1.0, *,
              category: Optional[str] = None,
              fee_rate_bps: Optional[int] = None,
              fee_multiplier: float = 1.0,
              legacy: bool = False) -> float:
    """Taker fee in dollars for ``venue`` ("kalshi" | "polymarket")."""
    v = venue.strip().lower()
    if v == "kalshi":
        return kalshi_taker_fee(price, size, fee_multiplier=fee_multiplier)
    if v == "polymarket":
        if legacy:
            return legacy_pm_taker_fee(price, size)
        return pm_taker_fee(price, size, fee_rate_bps=fee_rate_bps,
                            category=category)
    raise ValueError(f"Unknown venue: {venue!r} (kalshi|polymarket)")


def maker_fee(venue: str, price: float, size: float = 1.0, *,
              fee_type: Optional[str] = None,
              fee_multiplier: float = 1.0) -> float:
    """Maker fee in dollars for ``venue`` ("kalshi" | "polymarket").

    For Kalshi, pass the series' ``fee_type`` (see
    :func:`kalshi_series_fee_params`); defaults to the with-maker-fees type,
    the conservative (over-charging) side.
    """
    v = venue.strip().lower()
    if v == "kalshi":
        ft = KALSHI_FEE_TYPE_WITH_MAKER if fee_type is None else fee_type
        return kalshi_maker_fee(price, size, fee_type=ft,
                                fee_multiplier=fee_multiplier)
    if v == "polymarket":
        return pm_maker_fee(price, size)
    raise ValueError(f"Unknown venue: {venue!r} (kalshi|polymarket)")
