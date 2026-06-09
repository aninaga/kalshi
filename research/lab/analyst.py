"""research.lab.analyst — the autonomous analyst runner (the harness, not the brain).

This replaces ``agents/usage/edge_hunt_loop.py``'s hard-coded ``DIRECTIONS``
menu with a registry-driven loop. Where the old loop enumerated a fixed
markets x mechanisms matrix, this one PULLS OPEN hypotheses from the dynamic
registry (``lab.hypothesis``) and hands each one to a deployed agent as a
self-contained "assignment" (toolkit handle + the open hypothesis + the brief).

``run_analyst`` is deliberately the *harness*: it prepares assignments, claims
the hypothesis, dispatches the actual idea-generation/backtesting to an injected
``executor`` (a real deployment wires in a codex-exec / agent spawn here), and
records the trial result back to the registry via ``lab.hypothesis.update``. The
analytical work itself is the agent's job; this module is the deterministic,
testable loop around it.

It is budget-aware: when ``budget_aware`` it polls ``agents.usage.limits`` and
lets the governor decide how many ideas it is safe to work this run (0 => the
gpt-5.5 windows are tapped, so we stop early rather than spawn).

Sibling lab modules (``lab.hypothesis``) are imported LAZILY inside functions so
this module imports cleanly in an isolated worktree where they have not merged
yet; tests monkeypatch them.

CLI::

    python -m research.lab.analyst --max-ideas 3 --market total
    python -m research.lab.analyst --dry-run        # plan only; executor is a no-op
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

# agents.usage.limits IS present on the base branch — import it directly.
from research.agents.usage import limits as L

AGENT = "lab.analyst"

# An executor turns one assignment into a trial result dict. The default is a
# no-op stub: a real deployment injects a callable that spawns the agent (e.g.
# codex exec) with the assignment and parses its result.md.
Executor = Callable[[dict], dict]


@dataclass
class Assignment:
    """A self-contained research assignment handed to the deployed agent.

    Carries the open hypothesis it should work, the analyst brief, and a
    description of the toolkit handle (the agent imports ``research.lab`` itself;
    this names the entrypoints so the assignment is self-documenting).
    """
    hypothesis_id: str
    market: Optional[str]
    mechanism: str
    signal_desc: str
    direction: str
    brief: Optional[str]
    toolkit: dict

    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "market": self.market,
            "mechanism": self.mechanism,
            "signal_desc": self.signal_desc,
            "direction": self.direction,
            "brief": self.brief,
            "toolkit": self.toolkit,
        }


# The toolkit handle every assignment advertises — the composable lab surface an
# analyst-agent imports and freely composes (data -> signals -> strategy ->
# realistic execution -> gate, plus the hypothesis registry).
_TOOLKIT = {
    "import": "research.lab",
    "session": "research.lab.session.lab()",
    "modules": [
        "lab.data (load_panels)",
        "lab.signals (pace_projection, anchoring_gap, ...)",
        "lab.strategy (Strategy)",
        "lab.execution (FillModel, REALISTIC)",
        "lab.evaluate (evaluate -> GateResult)",
        "lab.hypothesis (register/claim/update)",
    ],
    "contract": "research/lab/CONTRACT.md",
    "rules": [
        "realistic execution is the default (never assume a 0.50 fill)",
        "never read the test split; never weaken the gate",
    ],
}

# Valid verdicts the registry understands (mirrors Hypothesis docstring).
_VERDICTS = ("PROMOTE", "PROMISING", "NEEDS_DATA", "DEAD")


def _noop_executor(assignment: dict) -> dict:
    """Default executor: records that the assignment was prepared but not run.

    A real deployment replaces this with an agent/codex-exec dispatcher. Keeping
    a no-op default makes ``run_analyst`` import-safe and deterministic.
    """
    return {
        "hypothesis_id": assignment.get("hypothesis_id"),
        "executed": False,
        "verdict": None,
        "results": {"note": "no-op executor; assignment prepared but not run"},
        "status": "open",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _budget_for(max_ideas: int, budget_aware: bool) -> tuple[int, str]:
    """How many ideas may we work this run? Returns (n, reason).

    UNKNOWN/unavailable usage => optimistic (work up to ``max_ideas``, matching
    the governor's philosophy). KNOWN-and-tapped (workers==0) => 0.
    """
    if not budget_aware:
        return max_ideas, "budget_aware=False -> run up to max_ideas"
    snap = L.poll()
    decision = L.decide_workers(snap, max_ideas)
    return decision.gpt55_workers, decision.reason


def run_analyst(
    brief: Optional[str] = None,
    *,
    max_ideas: int = 3,
    market: Optional[str] = None,
    budget_aware: bool = True,
    executor: Optional[Executor] = None,
    path: Optional[str] = None,
) -> dict:
    """Run one pass of the autonomous analyst harness.

    Pulls OPEN hypotheses from the registry (seeding defaults if it is empty),
    and for each idea up to the budget-allowed cap: claims it, builds an
    assignment, dispatches it to ``executor``, and records the trial result back
    via ``lab.hypothesis.update``. The actual idea-generation/backtesting is the
    agent's job (inside ``executor``); this is the loop around it.

    Args:
        brief: optional analyst brief threaded into every assignment.
        max_ideas: max OPEN hypotheses to work this run (budget may lower it).
        market: if set, only work hypotheses for this market.
        budget_aware: poll ``agents.usage.limits`` and let the governor cap work.
        executor: callable(assignment_dict) -> result_dict. Defaults to a no-op.
        path: optional registry path override (forwarded to ``lab.hypothesis``).

    Returns a summary dict::

        {"processed": int, "claimed": [...ids], "results": [...],
         "verdicts": {id: verdict}, "budget": {"allowed": n, "reason": str},
         "open_remaining": int, "brief": brief, "started": iso, "finished": iso}
    """
    # Lazy import: sibling module may be absent/unmerged in an isolated worktree.
    from research.lab import hypothesis as H

    started = _now()
    executor = executor or _noop_executor

    allowed, budget_reason = _budget_for(max_ideas, budget_aware)
    allowed = max(0, min(allowed, max_ideas))

    summary: dict = {
        "processed": 0,
        "claimed": [],
        "results": [],
        "verdicts": {},
        "budget": {"allowed": allowed, "reason": budget_reason},
        "open_remaining": 0,
        "brief": brief,
        "started": started,
        "finished": started,
    }

    if allowed <= 0:
        summary["finished"] = _now()
        return summary

    # Pull OPEN hypotheses; seed starter ideas if the registry is empty.
    open_hyps = H.open_hypotheses(path=path)
    if not open_hyps and hasattr(H, "seed_defaults"):
        H.seed_defaults(path=path)
        open_hyps = H.open_hypotheses(path=path)

    if market is not None:
        open_hyps = [h for h in open_hyps if getattr(h, "market", None) == market]

    for hyp in open_hyps[:allowed]:
        hyp_id = getattr(hyp, "id", "") or hyp.hash()
        # Claim it (status -> running) so parallel analysts don't double-work it.
        claimed = H.claim(hyp_id, AGENT, path=path)
        ref = claimed if claimed is not None else hyp
        summary["claimed"].append(hyp_id)

        assignment = Assignment(
            hypothesis_id=hyp_id,
            market=getattr(ref, "market", None),
            mechanism=getattr(ref, "mechanism", ""),
            signal_desc=getattr(ref, "signal_desc", ""),
            direction=getattr(ref, "direction", ""),
            brief=brief,
            toolkit=_TOOLKIT,
        ).to_dict()

        # Dispatch the real work to the agent (default: no-op stub).
        result = executor(assignment) or {}
        result.setdefault("hypothesis_id", hyp_id)

        verdict = result.get("verdict")
        results_payload = result.get("results")
        # Settle status: a verdict means the trial finished; otherwise leave it
        # claimed (running) for a later pass unless the executor said otherwise.
        status = result.get("status")
        if status is None:
            status = "done" if verdict in _VERDICTS else "running"

        update_kwargs: dict = {"status": status, "path": path}
        if verdict is not None:
            update_kwargs["verdict"] = verdict
        if results_payload is not None:
            update_kwargs["results"] = results_payload
        H.update(hyp_id, **update_kwargs)

        summary["processed"] += 1
        summary["results"].append(result)
        summary["verdicts"][hyp_id] = verdict

    # How many OPEN ideas remain after this pass (for the caller / next wave).
    remaining = H.open_hypotheses(path=path)
    if market is not None:
        remaining = [h for h in remaining if getattr(h, "market", None) == market]
    summary["open_remaining"] = len(remaining)
    summary["finished"] = _now()
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brief", default=None, help="analyst brief threaded into assignments")
    ap.add_argument("--max-ideas", type=int, default=3, help="max OPEN hypotheses to work")
    ap.add_argument("--market", default=None, help="restrict to one market (winner/total/spread)")
    ap.add_argument("--no-budget", action="store_true",
                    help="skip the budget governor (work up to --max-ideas)")
    ap.add_argument("--dry-run", action="store_true",
                    help="use the no-op executor (assignments prepared, not run)")
    a = ap.parse_args(argv)

    executor = _noop_executor if a.dry_run else None
    summary = run_analyst(
        a.brief,
        max_ideas=a.max_ideas,
        market=a.market,
        budget_aware=not a.no_budget,
        executor=executor,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
