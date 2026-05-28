"""Feature registry.

The registry is the project's single source of truth for which features exist,
where they come from, when they are first knowable, and whether a live
strategy is allowed to reference them. Every ``FeatureSpec`` carries a
live-availability contract:

* ``available_at_offset_sec`` — the game-clock offset at which the value is
  first knowable. The replay engine asserts on every bar that no feature is
  read before this offset for that bar's game clock.
* ``is_live_safe`` — if False, ``StrategySpec.validate()`` refuses the
  feature outright. This is the canary that blocks leakage specs (e.g.
  ESPN's model winprob) from ever entering the harness.

``compute_fn_path`` is stored as a dotted string (not a callable) so that
``FeatureSpec`` is JSON-serialisable and hashable. The registry resolves it
lazily via ``importlib`` at compute time.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable, Dict, List, Literal, Optional


Source = Literal[
    "kalshi_ws",
    "kalshi_rest",
    "polymarket_clob",
    "polymarket_gamma",
    "espn_pbp",
    "espn_winprob",
    "derived",
]


@dataclass(frozen=True)
class FeatureSpec:
    """A registered feature and its live-availability contract."""

    name: str
    compute_fn_path: str          # dotted path, e.g. "research.features.computers.compute_margin"
    source: Source
    latency_ms: int               # typical wall-clock event-to-value delay
    available_at_offset_sec: int  # game-clock offset at which value is first knowable
    is_live_safe: bool            # False => historical-only; refused by StrategySpec.validate()
    revisable: bool               # True if the source corrects past values
    description: str              # human-readable one-liner


class FeatureRegistry:
    """In-memory feature registry. Module-level singleton in this file."""

    def __init__(self) -> None:
        self._specs: Dict[str, FeatureSpec] = {}
        # Cache resolved callables to avoid re-importing on every compute call.
        self._fn_cache: Dict[str, Callable[[dict, float], Optional[float]]] = {}

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register(self, spec: FeatureSpec) -> None:
        """Add a feature. Refuses to overwrite an existing name."""
        if spec.name in self._specs:
            raise ValueError(
                f"FeatureSpec name {spec.name!r} is already registered; "
                "registry refuses to overwrite (append-only invariant)."
            )
        self._specs[spec.name] = spec

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def get(self, name: str) -> FeatureSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"No feature registered with name {name!r}") from exc

    def list_all(self) -> List[str]:
        """All registered feature names (sorted for deterministic output)."""
        return sorted(self._specs.keys())

    def list_live_safe(self) -> List[str]:
        """Names of features with ``is_live_safe=True`` (sorted)."""
        return sorted(name for name, s in self._specs.items() if s.is_live_safe)

    # ------------------------------------------------------------------ #
    # Validation (the live-safety gate that StrategySpec.validate() leans on)
    # ------------------------------------------------------------------ #
    def validate_strategy_features(self, feature_names: List[str]) -> None:
        """Verify every name is registered AND ``is_live_safe=True``.

        Raises a single ``ValueError`` listing all violations so a strategy
        author sees everything wrong at once rather than fixing-and-rerunning.
        """
        unregistered: List[str] = []
        not_live_safe: List[str] = []
        for name in feature_names:
            spec = self._specs.get(name)
            if spec is None:
                unregistered.append(name)
            elif not spec.is_live_safe:
                not_live_safe.append(name)

        if not unregistered and not_live_safe == []:
            return

        parts: List[str] = []
        if unregistered:
            parts.append(f"unregistered features: {unregistered}")
        if not_live_safe:
            parts.append(f"features not live-safe (is_live_safe=False): {not_live_safe}")
        raise ValueError("StrategySpec feature validation failed — " + "; ".join(parts))

    # ------------------------------------------------------------------ #
    # Compute (lazy importlib resolution)
    # ------------------------------------------------------------------ #
    def _resolve(self, spec: FeatureSpec) -> Callable[[dict, float], Optional[float]]:
        cached = self._fn_cache.get(spec.name)
        if cached is not None:
            return cached
        module_path, _, attr = spec.compute_fn_path.rpartition(".")
        if not module_path or not attr:
            raise RuntimeError(
                f"FeatureSpec {spec.name!r} has malformed compute_fn_path "
                f"{spec.compute_fn_path!r}; expected dotted path"
            )
        module = importlib.import_module(module_path)
        try:
            fn = getattr(module, attr)
        except AttributeError as exc:
            raise RuntimeError(
                f"FeatureSpec {spec.name!r}: {module_path} has no attribute {attr!r}"
            ) from exc
        if not callable(fn):
            raise RuntimeError(
                f"FeatureSpec {spec.name!r}: {spec.compute_fn_path} is not callable"
            )
        self._fn_cache[spec.name] = fn
        return fn

    def compute(
        self, name: str, bar: dict, game_clock_sec: float
    ) -> Optional[float]:
        """Compute one feature value for the given bar.

        Raises ``RuntimeError`` if the game clock has not yet reached the
        feature's ``available_at_offset_sec``. This is the per-bar live-safety
        assertion that mirrors what the replay engine will enforce — keeping
        it here too means even ad-hoc callers can't read a feature too early.
        """
        spec = self.get(name)
        if game_clock_sec < spec.available_at_offset_sec:
            raise RuntimeError(
                f"{name} not yet available at game_clock={game_clock_sec}"
            )
        return self._resolve(spec)(bar, game_clock_sec)


# ---------------------------------------------------------------------- #
# Module-level singleton + seed registration
# ---------------------------------------------------------------------- #

REGISTRY = FeatureRegistry()


def _register_seed_features() -> None:
    """Register the Wave-0 seed features.

    Per plan v2 §0.6: exactly 10 live-safe features plus one explicitly
    excluded leakage canary (``espn_home_winprob``). The canary exists so
    Wave-2's leakage-strategy test has something concrete to reference.
    """
    seed: List[FeatureSpec] = [
        FeatureSpec(
            name="margin",
            compute_fn_path="research.features.computers.compute_margin",
            source="espn_pbp",
            latency_ms=5000,
            available_at_offset_sec=0,
            is_live_safe=True,
            revisable=False,
            description="Home margin (home_score - away_score) at the current bar.",
        ),
        FeatureSpec(
            name="time_remaining",
            compute_fn_path="research.features.computers.compute_time_remaining",
            source="derived",
            latency_ms=0,
            available_at_offset_sec=0,
            is_live_safe=True,
            revisable=False,
            description="Seconds of regulation remaining (clamped to 0 in OT).",
        ),
        FeatureSpec(
            name="pace_ppm",
            compute_fn_path="research.features.computers.compute_pace_ppm",
            source="derived",
            latency_ms=5000,
            available_at_offset_sec=120,
            is_live_safe=True,
            revisable=False,
            description="Points per minute extrapolated from running total; "
                        "needs ~2 minutes of game for a stable estimate.",
        ),
        FeatureSpec(
            name="recent_run_signed",
            compute_fn_path="research.features.computers.compute_recent_run_signed",
            source="espn_pbp",
            latency_ms=5000,
            available_at_offset_sec=240,
            is_live_safe=True,
            revisable=False,
            description="Signed scoring-run delta over the last ~4 minutes "
                        "(positive = home); needs a 4-min lookback.",
        ),
        FeatureSpec(
            name="lineup_hash",
            compute_fn_path="research.features.computers.compute_lineup_hash",
            source="espn_pbp",
            latency_ms=5000,
            available_at_offset_sec=60,
            is_live_safe=True,
            revisable=False,
            description="Stable hash of the on-court lineup; confident after "
                        "the first substitution wave.",
        ),
        FeatureSpec(
            name="home_stars_on",
            compute_fn_path="research.features.computers.compute_home_stars_on",
            source="espn_pbp",
            latency_ms=5000,
            available_at_offset_sec=60,
            is_live_safe=True,
            revisable=False,
            description="Count of home-team stars currently on the floor.",
        ),
        FeatureSpec(
            name="away_stars_on",
            compute_fn_path="research.features.computers.compute_away_stars_on",
            source="espn_pbp",
            latency_ms=5000,
            available_at_offset_sec=60,
            is_live_safe=True,
            revisable=False,
            description="Count of away-team stars currently on the floor.",
        ),
        FeatureSpec(
            name="kalshi_implied_wp",
            compute_fn_path="research.features.computers.compute_kalshi_implied_wp",
            source="kalshi_rest",
            latency_ms=1000,
            available_at_offset_sec=0,
            is_live_safe=True,
            revisable=True,
            description="Kalshi-implied home win probability from mid-quote.",
        ),
        FeatureSpec(
            name="pm_implied_wp",
            compute_fn_path="research.features.computers.compute_pm_implied_wp",
            source="polymarket_clob",
            latency_ms=1000,
            available_at_offset_sec=0,
            is_live_safe=True,
            revisable=True,
            description="Polymarket-implied home win probability from mid-quote.",
        ),
        FeatureSpec(
            name="lead_changes_cum",
            compute_fn_path="research.features.computers.compute_lead_changes_cum",
            source="espn_pbp",
            latency_ms=5000,
            available_at_offset_sec=0,
            is_live_safe=True,
            revisable=False,
            description="Cumulative lead changes from tip-off through the current bar.",
        ),
        # ---------------- Explicitly excluded leakage canary ---------------- #
        FeatureSpec(
            name="espn_home_winprob",
            compute_fn_path="research.features.computers.compute_espn_home_winprob",
            source="espn_winprob",
            latency_ms=5000,
            available_at_offset_sec=0,
            is_live_safe=False,  # ← the canary; StrategySpec.validate() must raise
            revisable=True,
            description="ESPN's model winprob — LEAKAGE. is_live_safe=False so "
                        "any spec referencing it is rejected at validate().",
        ),
    ]
    for spec in seed:
        REGISTRY.register(spec)


_register_seed_features()
