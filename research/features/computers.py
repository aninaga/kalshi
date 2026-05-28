"""Feature-computation functions.

Each function has signature ``(bar: dict, game_clock_sec: float) -> float | None``.
``bar`` is a single-minute row from ``research.lake.reader.load_game()['pbp']``
(or, in Wave 0, the equivalent dict from ``research/cache/minutes_v1.parquet``).

These stubs are intentionally thin. Wave 1's replay engine will pass richer
context (rolling windows, orderbook snapshots, etc.) and the corresponding
computers will grow. The Wave 0 contract is the ``FeatureSpec`` semantics in
``registry.py``, not the compute logic here.
"""

from __future__ import annotations

from typing import Optional


# Regulation NBA game = 4 quarters * 12 min = 48 min = 2880 sec.
_REGULATION_GAME_SEC = 2880.0


def _to_float(value) -> Optional[float]:
    """Coerce a bar value to float, returning None on missing/non-numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_margin(bar: dict, _game_clock_sec: float) -> Optional[float]:
    """Home margin (home_score - away_score) at the current bar."""
    return _to_float(bar.get("margin"))


def compute_time_remaining(bar: dict, game_clock_sec: float) -> Optional[float]:
    """Seconds of regulation remaining; clamps to 0 in OT."""
    if game_clock_sec <= _REGULATION_GAME_SEC:
        return _REGULATION_GAME_SEC - float(game_clock_sec)
    return 0.0


def compute_pace_ppm(bar: dict, game_clock_sec: float) -> Optional[float]:
    """Points per minute extrapolated from the running total."""
    total = bar.get("total")
    if total is None:
        return None
    try:
        total_f = float(total)
    except (TypeError, ValueError):
        return None
    # game_clock_sec is in seconds; ppm = points / minutes.
    # Use max(gc, 1) guard so we never divide by zero even if the registry's
    # availability check is bypassed; registry still enforces gc >= 120s.
    minutes = max(float(game_clock_sec), 1.0) / 60.0
    return total_f / minutes


def compute_recent_run_signed(bar: dict, _game_clock_sec: float) -> Optional[float]:
    """Signed scoring-run delta over the last ~4 minutes (positive = home).

    TODO(wave1): a single bar is insufficient to compute a windowed run.
    The replay engine will need to pass either a rolling window of bars or a
    pre-computed `recent_run_signed_4m` field. Returning None here keeps the
    contract honest until that plumbing exists.
    """
    return None


def compute_lineup_hash(bar: dict, _game_clock_sec: float) -> Optional[float]:
    """Stable hash of the current on-court lineup state.

    Wave 0 placeholder: derived from the home/away stars-on flags so that any
    sub event that flips a star produces a different hash. Wave 1 will replace
    this with a real lineup hash computed from the subs table.
    """
    home_stars = bar.get("home_stars_on", 0)
    away_stars = bar.get("away_stars_on", 0)
    try:
        key = (int(home_stars or 0), int(away_stars or 0))
    except (TypeError, ValueError):
        key = (0, 0)
    return float(hash(key) & 0xFFFFFFFF)


def compute_home_stars_on(bar: dict, _game_clock_sec: float) -> Optional[float]:
    """Count of home-team stars currently on the floor."""
    return _to_float(bar.get("home_stars_on"))


def compute_away_stars_on(bar: dict, _game_clock_sec: float) -> Optional[float]:
    """Count of away-team stars currently on the floor."""
    return _to_float(bar.get("away_stars_on"))


def compute_kalshi_implied_wp(bar: dict, _game_clock_sec: float) -> Optional[float]:
    """Kalshi-implied home win probability (mid of yes_bid/yes_ask)."""
    return _to_float(bar.get("kalshi_home_winprob"))


def compute_pm_implied_wp(bar: dict, _game_clock_sec: float) -> Optional[float]:
    """Polymarket-implied home win probability (mid of yes_bid/yes_ask)."""
    return _to_float(bar.get("pm_home_winprob"))


def compute_lead_changes_cum(bar: dict, _game_clock_sec: float) -> Optional[float]:
    """Cumulative lead changes from tip-off through the current bar."""
    return _to_float(bar.get("lead_changes"))


def compute_espn_home_winprob(bar: dict, _game_clock_sec: float) -> Optional[float]:
    """ESPN's model home win probability.

    LEAKAGE: this is a model output, not a market signal. The registry marks
    this feature ``is_live_safe=False``; ``StrategySpec.validate()`` will
    raise before any replay can reach this code path. The function exists
    only so the registry entry resolves cleanly via importlib.
    """
    return _to_float(bar.get("espn_home_winprob"))
