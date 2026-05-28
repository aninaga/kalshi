"""THE LEAKAGE CANARY — references ``espn_home_winprob`` (is_live_safe=False).

``StrategySpec.validate()`` MUST raise ``ValueError`` mentioning
``espn_home_winprob``. The ``run()`` function catches that error and returns
a sentinel dict; tests assert this and assert that no row is written into
``trials.db`` for this spec hash.

If this trial ever gets recorded, the validator is broken.
"""

from research.harness.strategy_spec import StrategySpec, Condition, SizingRule

SPEC = StrategySpec(
    name="leakage_canary_v1",
    description=(
        "Canary: references espn_home_winprob which is is_live_safe=False. "
        "StrategySpec.validate() must raise. If this trial gets recorded in "
        "trials.db, the validator is broken."
    ),
    features=["margin", "espn_home_winprob"],
    entry_condition=Condition(
        expr="espn_home_winprob > 0.75 and pos_side is None",
        description=(
            "Would trigger when ESPN's leaked winprob is favourable -- but "
            "validation should reject before we ever get here."
        ),
    ),
    exit_conditions=[
        Condition(expr="True", description="never reached"),
    ],
    sizing=SizingRule(mode="fixed_contracts", value=5, max_position_contracts=100),
    time_stop_sec=180,
    side="long_home",
    venue="polymarket",
)


def run(split: str = "val"):
    """Validate and expect rejection.

    The leakage canary's purpose is to be REJECTED by the live-safety gate.
    A return value indicates expected failure; raising would mean the
    validator did not catch the leakage feature.
    """
    try:
        SPEC.validate()
    except ValueError as e:
        # Expected. The leakage canary's purpose is to be REJECTED.
        return {"validate_raised": True, "reason": str(e)}
    raise AssertionError("leakage canary unexpectedly passed validate()")


if __name__ == "__main__":
    import json
    r = run()
    print(json.dumps(r, indent=2))
