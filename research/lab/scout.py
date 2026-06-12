"""research.lab.scout — agent-driven hypothesis ORIGINATION (no hardcoded ideas).

The scout is machinery; the IDEAS come from the agent. It scans a market with
``lab.eda`` (idea-agnostic diagnostics), hands those findings + the existing
research record to a ``proposer``, and registers whatever distinct,
mechanism-grounded hypotheses the agent originates. There is no hand-written
strategy list anywhere in this path, and — by design — no built-in model: the
default ``proposer`` is a model-agnostic no-op that originates nothing. The
intelligence is INJECTED at the ``proposer`` seam.

In this codebase the injected proposer is a Claude Opus agent spawned from the
operator chat (see ``OPERATING_MODEL.md``): the operator hands the agent
``agents/workers/scout_prompt.md`` plus the EDA report below, and the agent
returns ``{market, mechanism, signal_desc, direction}`` dicts. With no proposer
injected the registry simply stays empty rather than falling back to canned
ideas.
"""
from __future__ import annotations

from pathlib import Path

from research.lab.types import MARKETS, Hypothesis

_ROOT = Path(__file__).resolve().parents[2]
# The prompt handed to an injected origination agent (e.g. a spawned Opus agent).
SCOUT_PROMPT = _ROOT / "research/agents/workers/scout_prompt.md"


def default_proposer(eda_report: dict, existing: list, brief: str | None) -> list[dict]:
    """Model-agnostic default: originate nothing.

    The scout NEVER invents ideas itself and bakes in no model. Origination is
    the job of an INJECTED proposer — a callable
    ``(eda_report, existing, brief) -> [{market, mechanism, signal_desc,
    direction}, ...]`` — which in practice is a Claude Opus agent the operator
    spawns from chat with ``SCOUT_PROMPT`` and ``eda_report``. With no proposer
    injected the open pool stays empty (and the loop says so).
    """
    return []


def propose(market: str | None = None, *, proposer=default_proposer,
            split: str = "train", max_new: int = 5, brief: str | None = None,
            registry_path: str | None = None, eda_fn=None) -> list:
    """Originate and register new hypotheses for ``market`` (or all markets).

    Returns the list of newly-registered ``Hypothesis`` rows (deduped by the
    registry). Pure agent origination — no canned ideas.
    """
    from research.lab import eda as _eda
    from research.lab import hypothesis as H

    eda_fn = eda_fn or _eda.scan_market
    path = registry_path or H.DEFAULT_PATH
    markets = [market] if market else list(MARKETS)
    existing = H.query(path=path)

    new_rows: list = []
    for mkt in markets:
        try:
            report = eda_fn(mkt, split=split)
        except Exception as exc:  # noqa: BLE001 — data/cache may be absent
            report = {"market": mkt, "error": str(exc)}
        proposals = proposer(report, existing, brief) or []
        for p in proposals[:max_new]:
            if not isinstance(p, dict) or "mechanism" not in p:
                continue
            h = Hypothesis(
                market=p.get("market", mkt),
                mechanism=str(p["mechanism"]).strip(),
                signal_desc=str(p.get("signal_desc", "")).strip(),
                direction=str(p.get("direction", "")).strip(),
            )
            registered = H.register(h, path=path)
            new_rows.append(registered)
            existing.append(registered)
    return new_rows
