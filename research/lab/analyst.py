"""research.lab.analyst — the autonomous analyst runner (the harness, not the brain).

A registry-driven loop: it PULLS OPEN hypotheses from the dynamic registry
(``lab.hypothesis``) and hands each one to an ``executor`` as a self-contained
"assignment" (toolkit handle + the open hypothesis + the brief).

``run_analyst`` is deliberately the *harness*, and it is model-agnostic: it
prepares assignments, claims the hypothesis, dispatches the actual
idea-generation/backtesting to an INJECTED ``executor``, and records the trial
result back to the registry via ``lab.hypothesis.update``. The analytical work
itself is the agent's job; this module is the deterministic, testable loop
around it. No model is baked in — the default ``executor`` is a no-op stub that
merely prepares the assignment. In this codebase the real executor is a Claude
Opus agent the operator spawns from chat (see ``OPERATING_MODEL.md``); inject it
as ``executor=...``.

Sibling lab modules (``lab.hypothesis``, ``lab.scout``, ``lab.director``) are
imported LAZILY inside functions so this module imports cleanly in an isolated
worktree; tests monkeypatch them.

CLI::

    python -m research.lab.analyst --max-ideas 3 --market total   # prepares assignments
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

AGENT = "lab.analyst"

# The shared per-trial ledger. Defaulting this ON is what makes governance
# N-aware in production: every trial the analyst settles is appended here, the
# director ranks against it, and ``lab.governance`` deflates the Deflated-Sharpe
# hurdle by the real trial count read from it (no more cold-start N=1 placeholder).
DEFAULT_LEDGER = "research/reports/alpha/ledger.jsonl"

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

    Model-agnostic by design — it bakes in no agent. The real executor is
    INJECTED (a Claude Opus agent the operator spawns from chat; see
    ``OPERATING_MODEL.md``). Keeping a no-op default makes ``run_analyst``
    import-safe and deterministic.
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


def _family_of(market) -> Optional[str]:
    """Best-effort event-class family for ``market`` via the provider registry.

    ``None`` when unresolvable — governance then counts the row toward every
    family (conservative). Never raises: telemetry, not the critical path.
    """
    if not isinstance(market, str) or not market:
        return None
    try:
        from research.lab import providers
        return providers.family_of(market)
    except Exception:  # noqa: BLE001
        return None


def _append_ledger(ledger_path: str, row: dict) -> None:
    """Append one settled-trial row to the shared JSONL ledger.

    The row shape (``hypothesis_id`` + ``verdict`` + ``results``) is exactly what
    ``lab.director`` reads back for prioritization and what ``lab.governance``
    dedupes by ``hypothesis_id`` to count distinct trials (the DSR ``N``). Best
    effort: a ledger write must never crash the research loop.
    """
    try:
        import os

        os.makedirs(os.path.dirname(ledger_path) or ".", exist_ok=True)
        with open(ledger_path, "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception:  # noqa: BLE001 — telemetry, not the critical path
        pass


def _budget_for(max_ideas: int, budget_aware: bool) -> tuple[int, str]:
    """How many ideas to work this run? Returns (n, reason).

    Model-agnostic: the cap is simply ``max_ideas``. The old GPT-5.5
    window governor was removed along with the codex spawn infra — spawn
    budgeting now lives with the operator who spins up the agents (and their
    own model's rate limits), not with a Python poller. ``budget_aware`` is
    accepted for backward compatibility and no longer changes behavior.
    """
    return max_ideas, "cap = max_ideas (no external budget governor)"


def run_analyst(
    brief: Optional[str] = None,
    *,
    max_ideas: int = 3,
    market: Optional[str] = None,
    budget_aware: bool = True,
    executor: Optional[Executor] = None,
    path: Optional[str] = None,
    ledger_path: Optional[str] = None,
    family: Optional[str] = None,
) -> dict:
    """Run one pass of the autonomous analyst harness.

    Originates ideas via the scout when the registry is empty, then lets the
    director (an agent) PRIORITIZE which open hypotheses to pursue,
    and for each idea up to the budget-allowed cap: claims it, builds an
    assignment, dispatches it to ``executor``, and records the trial result back
    via ``lab.hypothesis.update``. The actual idea-generation/backtesting is the
    agent's job (inside ``executor``); this is the loop around it.

    Args:
        brief: optional analyst brief threaded into every assignment.
        max_ideas: max OPEN hypotheses to work this run.
        market: if set, only work hypotheses for this market.
        budget_aware: accepted for backward compat; no longer changes behavior.
        executor: callable(assignment_dict) -> result_dict. Defaults to a no-op
            (the real, model-specific executor is injected; see OPERATING_MODEL.md).
        path: optional registry path override (forwarded to ``lab.hypothesis``).
        family: event-class family stamped on every ledger row (the per-family
            DSR partition key). When ``None``, derived per-hypothesis from its
            market via the provider registry (best effort; legacy rows without
            a family count toward every family's hurdle — never weaker).

    Returns a summary dict::

        {"processed": int, "claimed": [...ids], "results": [...],
         "verdicts": {id: verdict}, "budget": {"allowed": n, "reason": str},
         "open_remaining": int, "brief": brief, "started": iso, "finished": iso}
    """
    # Lazy import: sibling module may be absent/unmerged in an isolated worktree.
    from research.lab import hypothesis as H

    path = path or getattr(H, "DEFAULT_PATH", None)
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

    # Cold start: ORIGINATE ideas via the scout (an agent reasoning over EDA),
    # never canned defaults. Then PRIORITIZE via the director (an agent ranking
    # open ideas + the research ledger) — not FIFO. The only hardcoded thing
    # about which strategies get pursued is this scout->director machinery.
    if not H.open_hypotheses(path=path):
        from research.lab import scout
        scout.propose(market=market, brief=brief, registry_path=path)
    from research.lab import director
    chosen = director.select(k=allowed, market=market, registry_path=path,
                             ledger_path=ledger_path, brief=brief)

    for hyp in chosen:
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

        # Append the settled trial to the shared ledger so the next director
        # pass ranks against it and governance counts it toward the DSR N.
        if ledger_path:
            hyp_market = getattr(ref, "market", None)
            _append_ledger(ledger_path, {
                "hypothesis_id": hyp_id,
                "verdict": verdict,
                "results": results_payload if results_payload is not None else {},
                "agent": AGENT,
                "settled": _now(),
                # Per-family DSR partition key (+ the market it was derived
                # from, so governance can re-derive on legacy/foreign rows).
                "family": family if family is not None else _family_of(hyp_market),
                "market": hyp_market,
            })

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
    ap.add_argument("--ledger", default=DEFAULT_LEDGER,
                    help=f"shared trial ledger (default: {DEFAULT_LEDGER}); '' to disable")
    ap.add_argument("--family", default=None,
                    help="event-class family stamped on ledger rows (default: "
                         "derived from each hypothesis's market)")
    a = ap.parse_args(argv)

    # The CLI runs with the model-agnostic no-op executor: it PREPARES and prints
    # assignments. The analytical work is done by an injected executor — a Claude
    # Opus agent the operator spawns from chat (see OPERATING_MODEL.md) — or by
    # calling run_analyst(..., executor=...) programmatically.
    summary = run_analyst(
        a.brief,
        max_ideas=a.max_ideas,
        market=a.market,
        executor=_noop_executor,
        ledger_path=(a.ledger or None),
        family=a.family,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
