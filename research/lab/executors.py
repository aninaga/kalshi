"""research.lab.executors — the real executor for the autonomous analyst loop.

``research.lab.analyst.run_analyst`` is the deterministic *harness*: it
originates ideas (scout), prioritizes them (director), claims a hypothesis, and
records the verdict back to the registry. The one thing it deliberately leaves
abstract is the actual research work — that is the ``executor`` seam, a
``Callable[[assignment_dict], result_dict]``. The harness ships a ``_noop_executor``
stub so it imports and tests without spawning anything; THIS module is the
"real deployment" the stub's docstring promises.

``make_codex_executor`` returns an executor that hands one assignment to a live
agent via ``research.agents.usage.probe_gpt55.run_codex_exec`` (the same
``codex exec`` subprocess primitive the GPT-5.5 edge-hunt loop uses), then
harvests the agent's ``result.md`` JSON contract and maps it onto the result
dict the harness records. The agentic boundary is exactly here: swap the
``runner`` for any subprocess-agent (codex, a headless ``claude -p``, a mock)
and the no-fabrication contract still holds.

Honesty contract (mirrors the rest of the lab):
  * codex unavailable / errored / no parseable verdict  -> ``executed=False``,
    ``verdict=None``, ``status="open"`` so the idea is re-tried later. We NEVER
    fabricate a verdict, and a failed spawn never silently becomes ``DEAD``.
  * a parseable verdict outside the registry's vocabulary is dropped to
    ``None`` (the harness then leaves the idea ``running``) — the gate's
    vocabulary is authoritative, not the agent's free text.

Sibling modules are imported LAZILY inside the executor so this module imports
cleanly in an isolated worktree (and so tests can monkeypatch the runner).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

# The registry's verdict vocabulary (kept in sync with analyst._VERDICTS /
# the Hypothesis docstring). Anything else from an agent is not a verdict.
_VERDICTS = ("PROMOTE", "PROMISING", "NEEDS_DATA", "DEAD")

# Control keys we strip when folding a result.md JSON block into ``results`` so
# the recorded payload is the evidence, not the contract scaffolding.
_CONTROL_KEYS = frozenset({"verdict", "hypothesis_id", "status", "executed"})

_ROOT = Path(__file__).resolve().parents[2]
_ANALYST_PROMPT = _ROOT / "research/agents/workers/analyst_prompt.md"

_FALLBACK_ANALYST_PROMPT = (
    "You are a quant research ANALYST for NBA prediction markets, working ONE "
    "pre-registered hypothesis to a verdict. Use the research.lab toolkit "
    "(import research.lab): compose lab.signals into a lab.strategy, backtest "
    "with REALISTIC execution (never assume a 0.50 fill), and let lab.evaluate's "
    "promotion gate judge. Pass ledger_path so the Deflated-Sharpe correction "
    "deflates against the real trial count. Pre-register your direction before "
    "you score; hunt the bug on any too-good result; NEVER read the test split; "
    "NEVER weaken the gate. Honest negatives (verdict=DEAD) are valuable.\n\n"
    "Write result.md ending in a fenced ```json block with the contract:\n"
    '{"market","gate_passed","real_edge","verdict","results"} where verdict is '
    "one of PROMOTE | PROMISING | NEEDS_DATA | DEAD."
)


def _result_json(stdout: str, files: dict[str, str]) -> Optional[dict]:
    """Extract the trailing ```json object from result.md (or stdout).

    Mirrors ``probe_gpt55._result_json`` / ``edge_hunt_loop._harvest`` so the
    whole org parses the agent contract the same way.
    """
    blob = (files.get("result.md", "") if files else "") + "\n" + (stdout or "")
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", blob, re.DOTALL)
    for b in reversed(blocks):
        try:
            obj = json.loads(b)
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001 — try the next block up
            continue
    return None


def _assignment_prompt(assignment: dict, prompt_text: str) -> str:
    """The full prompt handed to the agent: the analyst brief + this assignment."""
    return (
        f"{prompt_text}\n\n---\n\n## Your assignment (one pre-registered hypothesis)\n"
        f"```json\n{json.dumps(assignment, indent=2, default=str)}\n```\n\n"
        "Work THIS hypothesis only. When done, write result.md ending in the "
        "```json verdict block described above."
    )


def make_codex_executor(
    *,
    model: str = "gpt-5.5",
    effort: str = "high",
    timeout_sec: int = 900,
    workdir_base: Optional[str] = None,
    runner: Optional[Callable[..., Any]] = None,
    prompt_path: Optional[str] = None,
) -> Callable[[dict], dict]:
    """Build a real ``run_analyst`` executor backed by a subprocess agent.

    Args:
        model / effort / timeout_sec: forwarded to ``run_codex_exec``.
        workdir_base: parent dir for per-assignment workdirs (default: a temp
            dir per call). Each assignment gets its own ``<base>/<hyp_id>`` dir.
        runner: the subprocess-agent primitive ``(prompt, workdir, *, model,
            effort, timeout_sec) -> CodexResult``. Defaults (lazily) to
            ``research.agents.usage.probe_gpt55.run_codex_exec``. Injectable so
            tests — and alternative agents — plug in here.
        prompt_path: override the analyst prompt file (default:
            ``agents/workers/analyst_prompt.md``, with a built-in fallback).

    Returns an ``executor(assignment) -> result`` callable. The result dict
    carries ``hypothesis_id``, ``executed``, ``verdict`` (validated or ``None``),
    ``results`` (the agent's evidence) and ``status``.
    """
    prompt_file = Path(prompt_path) if prompt_path else _ANALYST_PROMPT

    def _runner(prompt: str, workdir: Path) -> Any:
        run = runner
        if run is None:
            from research.agents.usage.probe_gpt55 import run_codex_exec  # lazy
            run = run_codex_exec
        return run(prompt, workdir, model=model, effort=effort, timeout_sec=timeout_sec)

    def _executor(assignment: dict) -> dict:
        hyp_id = assignment.get("hypothesis_id")
        prompt_text = (
            prompt_file.read_text() if prompt_file.exists() else _FALLBACK_ANALYST_PROMPT
        )
        full = _assignment_prompt(assignment, prompt_text)

        if workdir_base:
            wd = Path(workdir_base) / str(hyp_id or "assignment")
            wd.mkdir(parents=True, exist_ok=True)
            res = _runner(full, wd)
        else:
            import tempfile

            with tempfile.TemporaryDirectory(prefix="analyst_") as d:
                res = _runner(full, Path(d))

        stdout = getattr(res, "stdout", "") or ""
        files = getattr(res, "files", {}) or {}
        err = getattr(res, "error", None)
        returncode = getattr(res, "returncode", 0)

        # Spawn failed (codex missing / timeout / crash): never fabricate a
        # verdict; leave the idea OPEN so a later pass re-tries it.
        if err or returncode not in (0, None):
            return {
                "hypothesis_id": hyp_id,
                "executed": False,
                "verdict": None,
                "results": {"note": f"agent spawn failed: {err or f'rc={returncode}'}"},
                "status": "open",
            }

        parsed = _result_json(stdout, files)
        if not parsed:
            # Ran, but produced no parseable verdict contract — not a verdict.
            return {
                "hypothesis_id": hyp_id,
                "executed": False,
                "verdict": None,
                "results": {"note": "no parseable result.md verdict block"},
                "status": "open",
            }

        verdict = str(parsed.get("verdict", "")).upper() or None
        if verdict not in _VERDICTS:
            verdict = None  # agent vocabulary is not authoritative; gate's is

        # ``results``: prefer an explicit nested block, else the evidence fields.
        if isinstance(parsed.get("results"), dict):
            results_payload = dict(parsed["results"])
        else:
            results_payload = {k: v for k, v in parsed.items() if k not in _CONTROL_KEYS}

        return {
            "hypothesis_id": hyp_id,
            "executed": True,
            "verdict": verdict,
            "results": results_payload,
            # verdict present -> harness marks done; absent -> leaves running.
            "status": "done" if verdict in _VERDICTS else "running",
        }

    return _executor


# A ready-to-use default (gpt-5.5 / high). ``run_analyst`` / the heartbeat wire
# this in when not in --dry-run; tests use ``make_codex_executor(runner=...)``.
codex_executor = make_codex_executor()
