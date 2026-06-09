"""research.lab.director — agent-driven PRIORITIZATION + feedback (no hardcoded picks).

The director replaces round-robin/FIFO selection. It hands the agent the OPEN
hypotheses plus the recorded research ledger (what's been tried and how it
turned out) and asks it to RANK which to pursue next by expected value × novelty
× evidence — and to avoid re-running dead families. The ranking is the agent's;
the director is only the machinery. ``incorporate_results`` is the feedback
arm: it folds recorded verdicts back into the registry so the pool evolves.

Nothing here encodes a preference for any particular strategy, and no model is
baked in. The default ``ranker`` is a model-agnostic stable novelty order (most
recent first); a smarter ranking is INJECTED at the ``ranker`` seam — in
practice a Claude Opus agent the operator spawns from chat with
``agents/workers/director_prompt.md``, the OPEN pool, and the ledger.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
# The prompt handed to an injected ranking agent (e.g. a spawned Opus agent).
DIRECTOR_PROMPT = _ROOT / "research/agents/workers/director_prompt.md"
_DEAD = {"DEAD"}


def _read_ledger(ledger_path: str | None) -> list[dict]:
    if not ledger_path or not Path(ledger_path).exists():
        return []
    out = []
    for line in Path(ledger_path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def default_ranker(open_hyps: list, ledger: list, brief: str | None) -> list[str]:
    """Model-agnostic default: a stable novelty order (most-recent first).

    Bakes in no model and no strategy preference — just orders the
    agent-originated pool by recency so the loop is deterministic without an
    injected ranker. A real prioritization (EV × novelty × evidence,
    down-ranking DEAD families) is INJECTED at the ``ranker`` seam: a Claude
    Opus agent the operator spawns from chat with ``DIRECTOR_PROMPT``, the pool,
    and ``ledger``.
    """
    if not open_hyps:
        return []
    return [h.id or h.hash() for h in sorted(open_hyps, key=lambda h: h.created, reverse=True)]


def select(k: int = 3, market: str | None = None, *, ranker=default_ranker,
           registry_path: str | None = None, ledger_path: str | None = None,
           brief: str | None = None) -> list:
    """Return the top-``k`` OPEN hypotheses to pursue next, agent-ranked.

    If the open pool is empty this returns ``[]`` — the caller is expected to
    run ``lab.scout`` (agent origination) first. Never invents ideas.
    """
    from research.lab import hypothesis as H

    path = registry_path or H.DEFAULT_PATH
    open_hyps = H.open_hypotheses(path=path)
    if market is not None:
        open_hyps = [h for h in open_hyps if h.market == market]
    if not open_hyps:
        return []
    order = ranker(open_hyps, _read_ledger(ledger_path), brief)
    by_id = {h.id or h.hash(): h for h in open_hyps}
    return [by_id[i] for i in order if i in by_id][:k]


def incorporate_results(ledger_path: str | None, registry_path: str | None = None) -> dict:
    """Feedback: fold recorded ledger verdicts back into the registry.

    Each ledger row with a ``hypothesis_id``/``key`` + ``verdict`` updates that
    registry row (status -> done, verdict recorded). Driven entirely by recorded
    RESULTS — no hardcoded judgement about which strategies matter.
    """
    from research.lab import hypothesis as H

    path = registry_path or H.DEFAULT_PATH
    updated = {"applied": 0, "by_verdict": {}}
    for row in _read_ledger(ledger_path):
        hyp_id = row.get("hypothesis_id") or row.get("key")
        verdict = row.get("verdict")
        if not hyp_id or not verdict:
            continue
        status = "done" if str(verdict).upper() in _DEAD | {"PROMOTE", "NEEDS_DATA"} else "open"
        try:
            H.update(hyp_id, verdict=verdict, status=status, path=path)
        except Exception:  # noqa: BLE001 — unknown id is a no-op
            continue
        updated["applied"] += 1
        updated["by_verdict"][verdict] = updated["by_verdict"].get(verdict, 0) + 1
    return updated
