"""Cost profile abstraction — the dial that says how pessimistic to be.

Every cost knob the fill model uses (synthesized spread, PM curve fee rate,
PM flat taker rate, Kalshi taker multiplier) is gathered into a single
:class:`CostProfile`. The replay engine + ``realistic_fills`` read from the
currently-active profile via :func:`get_active_profile`. Tests and research
scripts switch profiles via :func:`use_profile` (context manager) or
:func:`set_active_profile`.

Three pre-defined profiles are exposed:

  - ``PESSIMISTIC`` — current pre-2026-05-28 defaults (1¢ half-spread, full PM
    fee curve + 2% flat taker). Mirrors what every prior trial was charged.
  - ``LIVE_PM``     — empirically more realistic PM moneyline costs (0.5¢
    half-spread, 0.5% flat + smaller curve). Best-guess of what live PM
    actually charges for liquid NBA moneyline binaries.
  - ``ZERO_COST``   — no spread, no fees. The edge-discovery diagnostic; if a
    strategy is negative even here, it has negative *underlying* expected
    return, not just a cost problem.

The named profiles are intentionally minimal — a researcher who wants
something in between (e.g. half-flat-rate, full-curve) constructs a custom
``CostProfile`` and passes it directly.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class CostProfile:
    """All cost knobs in one place.

    Attributes
    ----------
    name : str
        Human-readable label used in audit logs / reports.
    spread_bps : int
        Half-spread in basis points used by ``synthesize_orderbook_from_mid``
        (100 = 1¢ half-spread = 2¢ round-trip). Applies on top of the mid
        when synthesizing a book from a single mid price.
    pm_curve_fee_rate_bps : int
        ``fee_rate_bps`` parameter to the PM curve fee piece.
    pm_flat_taker_rate : float
        Flat PM taker rate on notional (e.g. 0.02 = 2%). Applies on EVERY fill.
    kalshi_taker_multiplier : float
        Multiplier on the Kalshi taker fee formula (1.0 = full fee, 0.0 = no
        fee). Useful for paper-trading what-ifs without rewriting the formula.
    """

    name: str
    spread_bps: int
    pm_curve_fee_rate_bps: int
    pm_flat_taker_rate: float
    kalshi_taker_multiplier: float = 1.0


# ---------------------------------------------------------------------------
# Pre-defined profiles
# ---------------------------------------------------------------------------

PESSIMISTIC = CostProfile(
    name="pessimistic",
    spread_bps=100,                  # 1¢ half-spread (2¢ round-trip)
    pm_curve_fee_rate_bps=1000,      # the pre-2026-05-28 hardcoded default
    pm_flat_taker_rate=0.02,         # 2% of notional, taker side
    kalshi_taker_multiplier=1.0,
)

LIVE_PM = CostProfile(
    name="live_pm",
    spread_bps=50,                   # 0.5¢ half-spread (1¢ round-trip)
    pm_curve_fee_rate_bps=200,       # smaller curve piece
    pm_flat_taker_rate=0.005,        # 0.5% flat (realistic for liquid NBA binaries)
    kalshi_taker_multiplier=1.0,
)

ZERO_COST = CostProfile(
    name="zero",
    spread_bps=0,
    pm_curve_fee_rate_bps=0,
    pm_flat_taker_rate=0.0,
    kalshi_taker_multiplier=0.0,
)

_BUILTIN: dict[str, CostProfile] = {
    "pessimistic": PESSIMISTIC,
    "live_pm": LIVE_PM,
    "zero": ZERO_COST,
}


# ---------------------------------------------------------------------------
# Active-profile state (module-level; not thread-safe by design — the harness
# is single-threaded within one process, and parallel runs each have their own
# Python process).
# ---------------------------------------------------------------------------

_active_profile: CostProfile = PESSIMISTIC


def get_active_profile() -> CostProfile:
    """Return the currently-active cost profile (default: PESSIMISTIC)."""
    return _active_profile


def set_active_profile(profile: "CostProfile | str") -> CostProfile:
    """Set the active cost profile; return the previous one.

    Accepts a :class:`CostProfile` instance or one of the built-in name
    strings (``"pessimistic"`` | ``"live_pm"`` | ``"zero"``).
    """
    global _active_profile
    previous = _active_profile
    if isinstance(profile, str):
        try:
            _active_profile = _BUILTIN[profile]
        except KeyError as exc:
            raise ValueError(
                f"unknown built-in cost profile {profile!r}; "
                f"options: {sorted(_BUILTIN)}"
            ) from exc
    elif isinstance(profile, CostProfile):
        _active_profile = profile
    else:
        raise TypeError(
            f"profile must be str or CostProfile, got {type(profile).__name__}"
        )
    return previous


@contextmanager
def use_profile(profile: "CostProfile | str"):
    """Temporarily set the active cost profile for the ``with`` block.

    Restores the prior profile on exit (even on exception).
    """
    previous = set_active_profile(profile)
    try:
        yield get_active_profile()
    finally:
        set_active_profile(previous)


__all__ = [
    "CostProfile",
    "PESSIMISTIC",
    "LIVE_PM",
    "ZERO_COST",
    "get_active_profile",
    "set_active_profile",
    "use_profile",
]
