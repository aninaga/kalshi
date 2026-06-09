"""research.lab.cli — the agent-facing command line for the lab.

A thin argparse entrypoint so an agent (or ``codex exec``) drives the research
lab without writing bespoke scripts::

    python -m research.lab.cli backtest mymod:STRATEGY --market total --split nontest
    python -m research.lab.cli hypotheses list
    python -m research.lab.cli hypotheses new --market total --mechanism "..." \
        --signal "..." --direction over
    python -m research.lab.cli hypotheses claim <id> --agent me
    python -m research.lab.cli demo

Sibling lab modules (``session``, ``hypothesis``) are imported LAZILY inside the
subcommand handlers so this module imports cleanly even in an isolated worktree
where those siblings are not yet present. The ``demo`` subcommand is fully
self-contained: it uses only ``research.lab.types`` synthetic fixtures and a
trivial inline strategy/eval, so it runs anywhere with zero real data.
"""
from __future__ import annotations

import argparse
import importlib
from typing import Optional, Sequence

import numpy as np

from research.lab.types import TOTAL, synthetic_panels


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #
def _load_strategy(spec: str):
    """Resolve a ``module.path:ATTR`` spec to the named object."""
    mod_path, sep, attr = spec.partition(":")
    if not sep or not mod_path or not attr:
        raise ValueError(
            f"strategy spec must be 'module.path:ATTR', got {spec!r}"
        )
    module = importlib.import_module(mod_path)
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise ValueError(
            f"module {mod_path!r} has no attribute {attr!r}"
        ) from exc


def _cmd_backtest(args: argparse.Namespace) -> int:
    # Lazy: lab.session may not be merged in this worktree.
    from research.lab import session as lab_session

    strategy = _load_strategy(args.strategy)
    lab = lab_session.lab()
    result = lab.backtest(strategy, args.market, split=args.split)
    _print_gate(result)
    return 0 if getattr(result, "passed", False) else 1


# --------------------------------------------------------------------------- #
# hypotheses
# --------------------------------------------------------------------------- #
def _cmd_hypotheses(args: argparse.Namespace) -> int:
    # Lazy: lab.hypothesis may not be merged in this worktree.
    from research.lab import hypothesis as lab_hyp

    action = args.action or "list"
    if action == "list":
        rows = lab_hyp.query(status=args.status, market=args.market)
        if not rows:
            print("(no hypotheses)")
            return 0
        for h in rows:
            print(f"{h.id}  [{h.status:<7}] {h.market:<7} {h.signal_desc}")
        return 0

    if action == "new":
        from research.lab.types import Hypothesis

        missing = [name for name in ("market", "mechanism", "signal", "direction")
                   if getattr(args, name) is None]
        if missing:
            raise ValueError(
                "hypotheses new requires --" + ", --".join(missing))
        h = Hypothesis(
            market=args.market,
            mechanism=args.mechanism,
            signal_desc=args.signal,
            direction=args.direction,
        )
        registered = lab_hyp.register(h)
        print(f"registered {registered.id}  [{registered.status}] {registered.market}")
        return 0

    if action == "claim":
        if not args.id:
            raise ValueError("hypotheses claim requires an <id>")
        claimed = lab_hyp.claim(args.id, args.agent)
        print(f"claimed {claimed.id}  [{claimed.status}] by {args.agent}")
        return 0

    raise ValueError(f"unknown hypotheses action: {action!r}")


# --------------------------------------------------------------------------- #
# demo (self-contained — only research.lab.types)
# --------------------------------------------------------------------------- #
def _cmd_demo(_args: argparse.Namespace) -> int:
    """Run a synthetic-fixture backtest end to end with NO sibling imports.

    Builds synthetic TOTAL panels and runs a trivial anchoring-style strategy
    directly over the Panel arrays: enter at the first bar inside a sane elapsed
    window with a single pre-registered direction (``over`` the implied level),
    then settle each trade against the final total. Wins and losses are decided
    purely by settlement (no peeking at the outcome to pick the side), so the
    cents-per-contract distribution is genuine. It prints a simple gate-shaped
    summary. This is a self-contained smoke of the CLI wiring; it imports no
    sibling lab modules.
    """
    panels = synthetic_panels(n_games=40, market=TOTAL, seed=0, signal=0.05)

    cost_c = 4.0   # toy round-trip execution cost, in cents per contract
    pnls: list[float] = []
    for p in panels:
        if p.n == 0 or p.final_total is None:
            continue
        # Trivial anchoring entry: first bar inside a sane elapsed window.
        idx = _first_in_window(p.elapsed_sec, lo=600.0, hi=2520.0)
        if idx is None:
            continue
        level = float(p.mid[idx])
        # Pre-registered direction: always bet "over" the implied level.
        won = p.final_total > level
        # Toy payoff in cents: settle to 100/0, net a flat round-trip cost.
        pnls.append((100.0 if won else 0.0) - cost_c)

    summary = _toy_gate(pnls)
    print("lab demo — synthetic anchoring backtest (TOTAL, signal=0.05)")
    _print_gate_dict(summary)
    return 0


def _first_in_window(elapsed: np.ndarray, lo: float, hi: float) -> Optional[int]:
    where = np.flatnonzero((elapsed >= lo) & (elapsed <= hi))
    return int(where[0]) if where.size else None


def _toy_gate(pnls: list[float]) -> dict:
    """A simple, gate-shaped summary dict (mirrors GateResult fields)."""
    n = len(pnls)
    if n == 0:
        return {"passed": False, "cents_per_contract": 0.0, "ci_lo": 0.0,
                "ci_hi": 0.0, "n": 0, "n_games": 0}
    arr = np.asarray(pnls, float)
    mean = float(arr.mean())
    se = float(arr.std(ddof=1) / np.sqrt(n)) if n > 1 else 0.0
    ci_lo, ci_hi = mean - 1.96 * se, mean + 1.96 * se
    return {
        "passed": ci_lo > 0.0,
        "cents_per_contract": mean,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "n": n,
        "n_games": n,
    }


# --------------------------------------------------------------------------- #
# printing helpers
# --------------------------------------------------------------------------- #
def _print_gate(result) -> None:
    _print_gate_dict({
        "passed": getattr(result, "passed", None),
        "cents_per_contract": getattr(result, "cents_per_contract", None),
        "ci_lo": getattr(result, "ci_lo", None),
        "ci_hi": getattr(result, "ci_hi", None),
        "n": getattr(result, "n", None),
        "n_games": getattr(result, "n_games", None),
    })
    reasons = getattr(result, "reasons", None)
    if reasons:
        for r in reasons:
            print(f"  - {r}")


def _print_gate_dict(d: dict) -> None:
    verdict = "PASS" if d.get("passed") else "FAIL"
    print(f"verdict: {verdict}")
    print(f"  cents_per_contract: {d.get('cents_per_contract')}")
    print(f"  ci: [{d.get('ci_lo')}, {d.get('ci_hi')}]")
    print(f"  n: {d.get('n')}  n_games: {d.get('n_games')}")


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research.lab.cli",
        description="Agent-facing CLI for the NBA research lab.",
    )
    sub = parser.add_subparsers(dest="command")

    p_bt = sub.add_parser(
        "backtest", help="load + run + evaluate a Strategy (module:ATTR).")
    p_bt.add_argument("strategy", help="strategy spec 'module.path:ATTR'")
    p_bt.add_argument("--market", required=True, help="winner|total|spread")
    p_bt.add_argument("--split", default="nontest",
                      help="train|val|nontest|None (NEVER test)")
    p_bt.set_defaults(func=_cmd_backtest)

    p_hyp = sub.add_parser(
        "hypotheses", help="inspect / register / claim hypotheses.")
    p_hyp.add_argument("action", nargs="?", default="list",
                       choices=["list", "new", "claim"])
    p_hyp.add_argument("id", nargs="?", default=None,
                       help="hypothesis id (for claim)")
    p_hyp.add_argument("--status", default=None, help="filter: open|running|done")
    p_hyp.add_argument("--market", default=None, help="market (filter or for new)")
    p_hyp.add_argument("--mechanism", default=None, help="why it persists (new)")
    p_hyp.add_argument("--signal", default=None, help="observable signal (new)")
    p_hyp.add_argument("--direction", default=None, help="pre-registered dir (new)")
    p_hyp.add_argument("--agent", default="cli", help="claiming agent (claim)")
    p_hyp.set_defaults(func=_cmd_hypotheses)

    p_demo = sub.add_parser(
        "demo", help="run a self-contained synthetic backtest end to end.")
    p_demo.set_defaults(func=_cmd_demo)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
