"""Phenomenon A's positive cell — halftime 1-3 point deficit, hold-to-settle.

Phase -1 EDA noted: "1-3 deficit bucket, hold to settlement, 0c cost: mean PnL
+0.038 with CI [-0.023, +0.099]". The cell didn't pass the gate (CI lo < 0)
but it's the closest-to-positive in P4 and a useful sanity check for the
harness end-to-end.
"""

from research.harness.strategy_spec import StrategySpec, Condition, SizingRule

SPEC = StrategySpec(
    name="p4_halftime_small_deficit_v1",
    description=(
        "Buy the team trailing by 1-3 points at or after halftime; hold to "
        "settlement (capped by time_stop)."
    ),
    features=["margin", "pm_implied_wp"],
    entry_condition=Condition(
        expr=(
            "elapsed_game_sec >= 1440 and abs(margin) >= 1 "
            "and abs(margin) <= 3 and pos_side is None"
        ),
        description=(
            "After half (24:00 elapsed), small absolute margin in [1, 3], "
            "no existing position."
        ),
    ),
    exit_conditions=[
        Condition(expr="True", description="hold to settlement via time_stop"),
    ],
    sizing=SizingRule(mode="fixed_contracts", value=5, max_position_contracts=100),
    time_stop_sec=7200,
    side="long_trailing",
    venue="polymarket",
)


def run(split: str = "val"):
    """Run this spec through ``run_batch`` on the specified split.

    Returns the ``BatchResult``. The orchestrator and tests call this.
    """
    from research.harness.run_batch import run_batch
    SPEC.validate()
    return run_batch(SPEC, split)


if __name__ == "__main__":
    import json
    r = run()
    print(json.dumps({
        "spec_hash": SPEC.spec_hash(),
        "n_trades": r.n_trades_total,
        "n_games_with_trades": r.n_games_with_trades,
        "pnl_net": r.pnl_net,
        "sharpe_net": r.sharpe_per_trade_net,
        "win_rate": r.win_rate,
    }, indent=2))
