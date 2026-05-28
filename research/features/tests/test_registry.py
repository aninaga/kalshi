"""Tests for the feature registry (Wave 0)."""

from __future__ import annotations

import unittest

from research.features.registry import REGISTRY, FeatureSpec


# The 11 names that must be registered after import — 10 live-safe plus the
# explicit leakage canary.
EXPECTED_LIVE_SAFE = {
    "margin",
    "time_remaining",
    "pace_ppm",
    "recent_run_signed",
    "lineup_hash",
    "home_stars_on",
    "away_stars_on",
    "kalshi_implied_wp",
    "pm_implied_wp",
    "lead_changes_cum",
}
EXPECTED_NON_LIVE_SAFE = {"espn_home_winprob"}
EXPECTED_ALL = EXPECTED_LIVE_SAFE | EXPECTED_NON_LIVE_SAFE


class TestSeedRegistration(unittest.TestCase):
    def test_seed_features_registered(self) -> None:
        registered = set(REGISTRY.list_all())
        self.assertEqual(
            registered,
            EXPECTED_ALL,
            f"registered names diverged from seed list: "
            f"missing={EXPECTED_ALL - registered}, extra={registered - EXPECTED_ALL}",
        )
        live_safe = set(REGISTRY.list_live_safe())
        self.assertEqual(live_safe, EXPECTED_LIVE_SAFE)
        # Cross-check: list_live_safe() length is exactly 10.
        self.assertEqual(len(REGISTRY.list_live_safe()), 10)


class TestValidateStrategyFeatures(unittest.TestCase):
    def test_validate_rejects_unregistered(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            REGISTRY.validate_strategy_features(["nonexistent_feature"])
        self.assertIn("nonexistent_feature", str(ctx.exception))

    def test_validate_rejects_non_live_safe(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            REGISTRY.validate_strategy_features(["margin", "espn_home_winprob"])
        self.assertIn("espn_home_winprob", str(ctx.exception))

    def test_validate_accepts_all_live_safe(self) -> None:
        # Should not raise.
        REGISTRY.validate_strategy_features(REGISTRY.list_live_safe())


class TestRegistrationInvariants(unittest.TestCase):
    def test_register_refuses_overwrite(self) -> None:
        dup = FeatureSpec(
            name="margin",  # already registered by _register_seed_features
            compute_fn_path="research.features.computers.compute_margin",
            source="espn_pbp",
            latency_ms=5000,
            available_at_offset_sec=0,
            is_live_safe=True,
            revisable=False,
            description="duplicate",
        )
        with self.assertRaises(ValueError):
            REGISTRY.register(dup)


class TestComputeAvailability(unittest.TestCase):
    def test_compute_before_available_raises(self) -> None:
        # recent_run_signed has available_at_offset_sec=240; gc=10 must raise.
        with self.assertRaises(RuntimeError) as ctx:
            REGISTRY.compute("recent_run_signed", bar={}, game_clock_sec=10.0)
        msg = str(ctx.exception)
        self.assertIn("recent_run_signed", msg)
        self.assertIn("10", msg)


if __name__ == "__main__":
    unittest.main()
