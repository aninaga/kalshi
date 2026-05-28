"""Phenomenon C — long the hot team on an 8+/4-game-min scoring run.

THE REAL CANDIDATE. The Phase −1 EDA found this was the only phenomenon to
clear the gate at 2.5c (Kalshi) cost; it failed at 4c (Polymarket). We still
keep it on the polymarket venue here because the cache is PM-only.

TODO(Wave 2/3): ``recent_run_signed`` is stubbed to ``None`` in
``research/features/computers.py``. Until that feature gets a real windowed
computation (rolling lookback of bars), the entry condition is unreachable
and ``run()`` will produce a zero-trade BatchResult. The spec is wired up so
it starts trading the moment the feature lands.
"""

from research.harness.strategy_spec import StrategySpec, Condition, SizingRule

SPEC = StrategySpec(
    name="c_score_run_buy_hot_v1",
    description=(
        "Long the team on a 8+ in last 4 game-minutes scoring run, exit at "
        "+4 game-minutes."
    ),
    features=["margin", "pm_implied_wp", "recent_run_signed"],
    entry_condition=Condition(
        expr=(
            "abs(recent_run_signed) >= 8 and pos_side is None "
            "and elapsed_game_sec >= 240 and elapsed_game_sec <= 2640"
        ),
        description=(
            "Scoring-run magnitude >= 8 over the last 4 game-minutes; gated "
            "between 4:00 elapsed and the start of the final 4 minutes."
        ),
    ),
    exit_conditions=[
        Condition(expr="True", description="time-stop only -- 4 game-min"),
    ],
    sizing=SizingRule(mode="fixed_contracts", value=5, max_position_contracts=100),
    time_stop_sec=240,
    side="long_hot",
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
