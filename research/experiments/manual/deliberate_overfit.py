"""THE OVERFIT CANARY — designed to pass DSR but fail block-bootstrap-by-game.

Purpose
-------
The promotion gate must reject specs whose sample mean is positive only
because the trades are concentrated in a handful of games. This canary is
intentionally over-narrow: the entry conditions are stacked enough that
only a small number of games qualify, and within those games the picture
favors home, so the sample mean is likely > 0. But the block-bootstrap-
by-game CI lower bound (computed by the scorer agent later) should be
<= 0 because the effective sample size at the GAME level is tiny.

If this spec ever passes the promotion gate, the gate is broken.

Caveat
------
We can't guarantee the realized PnL is positive without seeing the data.
If by chance the realized sample mean is non-positive, this canary loses
its overfit-by-luck character but is still informative -- it just means
we couldn't construct one without peeking.
"""

from research.harness.strategy_spec import StrategySpec, Condition, SizingRule

SPEC = StrategySpec(
    name="deliberate_overfit_v1",
    description=(
        "Canary: should pass DSR (positive sample mean) but fail "
        "block-bootstrap-by-game CI lower bound and/or season-split "
        "stability. If this spec passes the promotion gate, the gate is "
        "broken."
    ),
    features=["margin", "pm_implied_wp", "home_stars_on", "away_stars_on"],
    entry_condition=Condition(
        expr=(
            "margin >= 5 and margin <= 7 "
            "and home_stars_on >= 2 and away_stars_on == 0 "
            "and elapsed_game_sec >= 1800 and elapsed_game_sec <= 2100 "
            "and pos_side is None"
        ),
        description=(
            "Hyper-narrow entry: home up 5-7, home has >=2 stars on while "
            "away has 0, between 30:00 and 35:00 elapsed."
        ),
    ),
    exit_conditions=[
        Condition(expr="True", description="time-stop 6 game-min"),
    ],
    sizing=SizingRule(mode="fixed_contracts", value=5, max_position_contracts=100),
    time_stop_sec=360,
    side="long_home",
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
