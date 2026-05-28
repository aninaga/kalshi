"""Run all 5 manual strategies and tabulate.

Usage
-----
    PYTHONDONTWRITEBYTECODE=1 ~/code/kvenv/bin/python3 \\
        -m research.experiments.manual.run_all

Each row is a dict summarising one strategy's outcome. The leakage canary's
``validate_rejected`` row is load-bearing: if it doesn't appear, the spec
validator is broken.
"""

from research.experiments.manual import (
    c_score_run_buy_hot,
    p4_halftime_1to3,
    p2_underdog,
    deliberate_overfit,
    leakage_winprob,
)

STRATEGIES = [
    c_score_run_buy_hot,
    p4_halftime_1to3,
    p2_underdog,
    deliberate_overfit,
    leakage_winprob,
]


def _name_of(mod) -> str:
    """Best-effort spec name lookup for error rows where SPEC may be absent."""
    spec = getattr(mod, "SPEC", None)
    if spec is not None:
        return spec.name
    return mod.__name__


def main():
    rows = []
    for mod in STRATEGIES:
        try:
            r = mod.run()
            if isinstance(r, dict) and r.get("validate_raised"):
                rows.append({
                    "name": _name_of(mod),
                    "status": "validate_rejected",
                    "reason": r["reason"][:80],
                })
            else:
                rows.append({
                    "name": mod.SPEC.name,
                    "n_trades": r.n_trades_total,
                    "pnl_net": r.pnl_net,
                    "sharpe_net": r.sharpe_per_trade_net,
                    "win_rate": r.win_rate,
                })
        except Exception as e:
            rows.append({
                "name": _name_of(mod),
                "status": f"error: {type(e).__name__}: {e}",
            })
    for r in rows:
        print(r)


if __name__ == "__main__":
    main()
