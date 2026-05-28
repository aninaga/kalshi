"""Tests for the Wave 1 StrategySpec contract.

These tests pin down four things:

1. JSON round-tripping is lossless and produces a stable hash.
2. Live-safety: unregistered or non-live-safe features are rejected.
3. Declared-deps: an expression that references an undeclared feature is rejected.
4. Expression-AST safety: attribute access, imports, and disallowed calls are rejected.

The tests use ONLY features registered as live-safe in
``research.features.registry``, so they remain valid even if the seed feature
list changes — assuming ``margin``, ``recent_run_signed``, and
``kalshi_implied_wp`` continue to exist.
"""

from __future__ import annotations

import unittest

from research.harness.strategy_spec import (
    Condition,
    SizingRule,
    StrategySpec,
    eval_condition,
)


def _make_valid_spec(**overrides) -> StrategySpec:
    """Return a minimal-but-valid StrategySpec, with optional overrides."""
    defaults = dict(
        name="c_buy_hot_10_2",
        description="C-phenomenon: long the hot team on a 10-2 run; "
                    "exit after 4 minutes of game clock.",
        features=["margin", "recent_run_signed", "kalshi_implied_wp"],
        entry_condition=Condition(
            expr="abs(recent_run_signed) >= 8 and margin < 0",
            description="hot team trailing on a >=8-point run",
        ),
        exit_conditions=[
            Condition(expr="True", description="time-stop only"),
        ],
        sizing=SizingRule(mode="fixed_contracts", value=5, max_position_contracts=100),
        time_stop_sec=240,
        side="long_hot",
        venue="polymarket",
    )
    defaults.update(overrides)
    return StrategySpec(**defaults)


class TestRoundTrip(unittest.TestCase):
    def test_valid_spec_roundtrip(self) -> None:
        spec = _make_valid_spec()
        spec.validate()

        as_json = spec.to_json()
        roundtripped = StrategySpec.from_json(as_json)

        self.assertEqual(spec, roundtripped)
        self.assertEqual(spec.spec_hash(), roundtripped.spec_hash())

    def test_spec_hash_stable(self) -> None:
        """Two specs built with the same content but with the features list
        in different order should NOT collapse to the same hash (feature
        order is semantically meaningful for dep declaration)."""
        spec_a = _make_valid_spec()
        spec_b = _make_valid_spec(
            features=["kalshi_implied_wp", "margin", "recent_run_signed"]
        )
        # Hashes differ because feature order is part of the canonical form.
        self.assertNotEqual(spec_a.spec_hash(), spec_b.spec_hash())

        # ...but the same spec hashed twice must collide.
        self.assertEqual(spec_a.spec_hash(), _make_valid_spec().spec_hash())

        # And JSON sort_keys=True guarantees field-declaration order does
        # not change the hash: build the dict from from_dict() with keys
        # in scrambled order (json.dumps will re-sort) and confirm parity.
        scrambled = {
            "venue": "polymarket",
            "side": "long_hot",
            "time_stop_sec": 240,
            "sizing": {
                "value": 5.0,
                "max_position_contracts": 100,
                "mode": "fixed_contracts",
            },
            "exit_conditions": [{"description": "time-stop only", "expr": "True"}],
            "entry_condition": {
                "description": "hot team trailing on a >=8-point run",
                "expr": "abs(recent_run_signed) >= 8 and margin < 0",
            },
            "features": ["margin", "recent_run_signed", "kalshi_implied_wp"],
            "description": "C-phenomenon: long the hot team on a 10-2 run; "
                           "exit after 4 minutes of game clock.",
            "name": "c_buy_hot_10_2",
            "cost_assumption_bps": None,
        }
        rebuilt = StrategySpec.from_dict(scrambled)
        self.assertEqual(rebuilt.spec_hash(), spec_a.spec_hash())


class TestValidation(unittest.TestCase):
    def test_validate_rejects_unregistered_feature(self) -> None:
        spec = _make_valid_spec(
            features=["nonexistent"],
            entry_condition=Condition(expr="True"),
        )
        with self.assertRaises(ValueError) as ctx:
            spec.validate()
        self.assertIn("nonexistent", str(ctx.exception))

    def test_validate_rejects_non_live_safe(self) -> None:
        spec = _make_valid_spec(
            features=["espn_home_winprob"],
            entry_condition=Condition(expr="espn_home_winprob > 0.5"),
        )
        with self.assertRaises(ValueError) as ctx:
            spec.validate()
        msg = str(ctx.exception)
        self.assertIn("espn_home_winprob", msg)
        self.assertIn("live_safe", msg)

    def test_validate_rejects_undeclared_feature_in_expr(self) -> None:
        # Declares margin only, but references recent_run_signed.
        spec = _make_valid_spec(
            features=["margin"],
            entry_condition=Condition(expr="recent_run_signed > 0"),
        )
        with self.assertRaises(ValueError) as ctx:
            spec.validate()
        msg = str(ctx.exception)
        self.assertIn("recent_run_signed", msg)
        self.assertIn("undeclared", msg)

    def test_validate_rejects_unsafe_ast(self) -> None:
        spec = _make_valid_spec(
            features=["margin"],
            entry_condition=Condition(
                expr="__import__('os').system('rm -rf /')"
            ),
        )
        with self.assertRaises(ValueError) as ctx:
            spec.validate()
        msg = str(ctx.exception).lower()
        # Call to a disallowed function name OR a disallowed-node error.
        self.assertTrue(
            "disallowed" in msg or "__import__" in msg,
            f"unexpected error msg: {ctx.exception}",
        )

    def test_validate_rejects_attribute_access(self) -> None:
        spec = _make_valid_spec(
            features=["margin"],
            entry_condition=Condition(expr="ctx.margin < 0"),
        )
        with self.assertRaises(ValueError) as ctx:
            spec.validate()
        self.assertIn("Attribute", str(ctx.exception))

    def test_time_stop_cap(self) -> None:
        spec = _make_valid_spec(time_stop_sec=8000)
        with self.assertRaises(ValueError) as ctx:
            spec.validate()
        self.assertIn("time_stop_sec", str(ctx.exception))

    def test_time_stop_negative_rejected(self) -> None:
        spec = _make_valid_spec(time_stop_sec=-1)
        with self.assertRaises(ValueError):
            spec.validate()

    def test_sizing_rule_zero_rejected(self) -> None:
        spec = _make_valid_spec(
            sizing=SizingRule(mode="fixed_contracts", value=0, max_position_contracts=100)
        )
        with self.assertRaises(ValueError) as ctx:
            spec.validate()
        self.assertIn("sizing.value", str(ctx.exception))

    def test_sizing_max_position_zero_rejected(self) -> None:
        spec = _make_valid_spec(
            sizing=SizingRule(
                mode="fixed_contracts", value=5, max_position_contracts=0
            )
        )
        with self.assertRaises(ValueError):
            spec.validate()

    def test_bad_side_rejected(self) -> None:
        spec = _make_valid_spec(side="long_sideways")  # type: ignore[arg-type]
        with self.assertRaises(ValueError) as ctx:
            spec.validate()
        self.assertIn("side", str(ctx.exception))

    def test_bad_venue_rejected(self) -> None:
        spec = _make_valid_spec(venue="ftx")  # type: ignore[arg-type]
        with self.assertRaises(ValueError) as ctx:
            spec.validate()
        self.assertIn("venue", str(ctx.exception))


class TestEvalCondition(unittest.TestCase):
    def test_eval_condition_works(self) -> None:
        cond = Condition(expr="margin < -5")
        self.assertTrue(eval_condition(cond, {"margin": -7}))
        self.assertFalse(eval_condition(cond, {"margin": -3}))

    def test_eval_condition_with_sign(self) -> None:
        cond = Condition(expr="sign(margin) == -1")
        self.assertTrue(eval_condition(cond, {"margin": -10}))
        self.assertFalse(eval_condition(cond, {"margin": 0}))
        self.assertFalse(eval_condition(cond, {"margin": 4}))

    def test_eval_condition_compound(self) -> None:
        cond = Condition(
            expr="margin < -7 and elapsed_game_sec >= 1440 "
                 "and recent_run_signed > 0"
        )
        self.assertTrue(
            eval_condition(
                cond,
                {"margin": -8, "elapsed_game_sec": 1500, "recent_run_signed": 4},
            )
        )
        self.assertFalse(
            eval_condition(
                cond,
                {"margin": -3, "elapsed_game_sec": 1500, "recent_run_signed": 4},
            )
        )

    def test_eval_condition_refuses_attribute_at_eval_time(self) -> None:
        """eval_condition must re-validate AST so unsafe specs can't slip
        past callers who construct Condition directly."""
        cond = Condition(expr="(1).real")  # Attribute access on a constant
        with self.assertRaises(ValueError):
            eval_condition(cond, {})


if __name__ == "__main__":
    unittest.main()
