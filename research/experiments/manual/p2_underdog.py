"""Phenomenon 2 — cheap underdog in Q4, hold to settlement.

Phase -1 result: FAILED. This baseline is intentionally included as a
"we already know this doesn't work" negative control. If the harness gives
this a passing block-bootstrap CI lower bound, something has drifted.

Entry uses the side-agnostic underdog band: ``pm_implied_wp < 0.20`` or
``pm_implied_wp > 0.80``. The ``side="long_trailing"`` resolution picks the
underdog regardless of home/away orientation -- at Q4 with the market deep
on one side, the trailing team IS the underdog.
"""

from research.harness.strategy_spec import StrategySpec, Condition, SizingRule

SPEC = StrategySpec(
    name="p2_q4_underdog_v1",
    description=(
        "Buy the trailing team in Q4 when the market puts the underdog in "
        "the 5-20% win-prob band; hold to settlement."
    ),
    features=["margin", "pm_implied_wp"],
    entry_condition=Condition(
        expr=(
            "elapsed_game_sec >= 2160 "
            "and (pm_implied_wp < 0.20 or pm_implied_wp > 0.80) "
            "and pos_side is None"
        ),
        description=(
            "In Q4 (36:00+ elapsed) with one side priced as a heavy "
            "underdog; no existing position."
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
