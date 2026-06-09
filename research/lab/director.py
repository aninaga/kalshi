"""research.lab.director — agent-driven PRIORITIZATION + feedback (no hardcoded picks).

The director replaces round-robin/FIFO selection. It hands the agent the OPEN
hypotheses plus the recorded research ledger (what's been tried and how it
turned out) and asks it to RANK which to pursue next by expected value × novelty
× evidence — and to avoid re-running dead families. The ranking is the agent's;
the director is only the machinery. ``incorporate_results`` is the feedback
arm: it folds recorded verdicts back into the registry so the pool evolves.

Nothing here encodes a preference for any particular strategy. If no agent is
available, selection degrades to a stable novelty order over the AGENT-ORIGINATED
pool (it never injects canned ideas).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
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


def default_ranker(open_hyps: list, ledger: list, brief: str | None,
                   *, model: str = "gpt-5.5", effort: str = "high") -> list[str]:
    """Ask the live agent (codex) to rank OPEN hypotheses; return ordered ids.

    Degrades to a stable novelty order (most-recent first) over the
    agent-originated pool when codex is unavailable — never a hardcoded
    preference for a particular strategy.
    """
    import tempfile

    from research.agents.usage.probe_gpt55 import run_codex_exec  # lazy

    if not open_hyps:
        return []
    prompt = DIRECTOR_PROMPT.read_text() if DIRECTOR_PROMPT.exists() else _FALLBACK_DIRECTOR_PROMPT
    pool = [{"id": h.id or h.hash(), "market": h.market, "mechanism": h.mechanism,
             "signal_desc": h.signal_desc, "direction": h.direction} for h in open_hyps]
    payload = (
        f"{prompt}\n\n---\n\n## OPEN hypotheses (rank these)\n"
        f"```json\n{json.dumps(pool, indent=2)}\n```\n\n"
        f"## Research ledger (past verdicts — avoid re-running DEAD families)\n"
        f"```json\n{json.dumps(ledger[-50:], indent=2)}\n```\n\n"
        f"## Coordinator brief\n{brief or '(none)'}\n\n"
        'Output ONLY a fenced ```json block: an ordered list of hypothesis ids '
        '(most worth pursuing first), e.g. ["id1","id2",...].')
    with tempfile.TemporaryDirectory(prefix="director_") as d:
        res = run_codex_exec(payload, Path(d), model=model, effort=effort)
    order = _parse_order(res.stdout)
    valid = {h.id or h.hash() for h in open_hyps}
    ranked = [i for i in order if i in valid]
    if not ranked:                                  # degrade: novelty order
        return [h.id or h.hash() for h in sorted(open_hyps, key=lambda h: h.created, reverse=True)]
    # append any open ids the agent omitted, preserving its priority first
    return ranked + [i for i in valid if i not in set(ranked)]


def _parse_order(text: str) -> list[str]:
    blocks = re.findall(r"```json\s*(\[.*?\])\s*```", text or "", re.DOTALL)
    for b in reversed(blocks):
        try:
            data = json.loads(b)
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:  # noqa: BLE001
            continue
    return []


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


_FALLBACK_DIRECTOR_PROMPT = (
    "You are the RESEARCH DIRECTOR for NBA prediction-market research. Given the "
    "OPEN hypotheses and the ledger of past verdicts, rank which to pursue next by "
    "expected value × novelty × evidence. Down-rank ideas similar to families "
    "already found DEAD; up-rank PROMISING leads worth deepening and genuinely "
    "novel mechanisms. Return an ordered list of hypothesis ids."
)
