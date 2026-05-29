"""Regression check for the run_batch(parallel>1) cost-profile footgun fix.

Under a non-default profile, parallel=2 must now match parallel=1 (before the
fix, spawned workers silently fell back to PESSIMISTIC). Must be run from a FILE
(not stdin) so multiprocessing 'spawn' can re-import __main__.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.harness.cost_profile import LIVE_PM, use_profile
from research.harness.run_batch import run_batch
from research.harness.strategy_spec import StrategySpec


def main() -> None:
    spec = StrategySpec.from_json(open("/tmp/spec174.json").read())
    with use_profile(LIVE_PM):
        r1 = run_batch(spec, "val", rng_seed=0, parallel=1)
        r2 = run_batch(spec, "val", rng_seed=0, parallel=2)
    print("  parallel=1: n=%d pnl_net=%.4f" % (r1.n_trades_total, r1.pnl_net))
    print("  parallel=2: n=%d pnl_net=%.4f" % (r2.n_trades_total, r2.pnl_net))
    match = abs(r1.pnl_net - r2.pnl_net) < 1e-6 and r1.n_trades_total == r2.n_trades_total
    print("  MATCH:", "YES (fix works)" if match else "NO (footgun still live)")


if __name__ == "__main__":
    main()
