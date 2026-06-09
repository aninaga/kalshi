"""research.lab.scout — agent-driven hypothesis ORIGINATION (no hardcoded ideas).

The scout is machinery; the IDEAS come from the agent. It scans a market with
``lab.eda`` (idea-agnostic diagnostics), hands those findings + the existing
research record to a ``proposer`` (an LLM/codex call by default), and registers
whatever distinct, mechanism-grounded hypotheses the agent originates. There is
no hand-written strategy list anywhere in this path — if no agent is available
and the registry is empty, the pool stays empty (and the loop says so) rather
than falling back to canned ideas.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from research.lab.types import MARKETS, Hypothesis

_ROOT = Path(__file__).resolve().parents[2]
SCOUT_PROMPT = _ROOT / "research/agents/workers/scout_prompt.md"


def default_proposer(eda_report: dict, existing: list, brief: str | None,
                     *, model: str = "gpt-5.5", effort: str = "high") -> list[dict]:
    """Ask the live agent (codex) to originate hypotheses from the EDA.

    Returns a list of ``{market, mechanism, signal_desc, direction}`` dicts.
    Degrades to ``[]`` when codex is unavailable — it NEVER substitutes canned
    ideas. The actual creativity is the model's, grounded in ``eda_report``.
    """
    import tempfile

    from research.agents.usage.probe_gpt55 import run_codex_exec  # lazy

    prompt = SCOUT_PROMPT.read_text() if SCOUT_PROMPT.exists() else _FALLBACK_SCOUT_PROMPT
    existing_brief = [
        {"market": h.market, "mechanism": h.mechanism, "status": h.status,
         "verdict": h.verdict} for h in existing]
    payload = (
        f"{prompt}\n\n---\n\n## EDA findings (idea-agnostic diagnostics)\n"
        f"```json\n{json.dumps(eda_report, indent=2)}\n```\n\n"
        f"## Existing research record (do NOT duplicate these)\n"
        f"```json\n{json.dumps(existing_brief, indent=2)}\n```\n\n"
        f"## Coordinator brief\n{brief or '(none)'}\n\n"
        "Output ONLY a fenced ```json block: a list of "
        '{"market","mechanism","signal_desc","direction"} objects.')
    with tempfile.TemporaryDirectory(prefix="scout_") as d:
        res = run_codex_exec(payload, Path(d), model=model, effort=effort)
    return _parse_proposals(res.stdout)


def _parse_proposals(text: str) -> list[dict]:
    blocks = re.findall(r"```json\s*(\[.*?\])\s*```", text or "", re.DOTALL)
    for b in reversed(blocks):
        try:
            data = json.loads(b)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except Exception:  # noqa: BLE001
            continue
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


_FALLBACK_SCOUT_PROMPT = (
    "You are a quant research SCOUT for NBA prediction markets. From the EDA "
    "diagnostics below, ORIGINATE new, distinct, mechanism-grounded mispricing "
    "hypotheses the data actually suggests. Do not propose timing/momentum "
    "(honest one-bar entry latency kills it). Ground every hypothesis in a "
    "specific EDA observation. Do not duplicate the existing research record."
)
