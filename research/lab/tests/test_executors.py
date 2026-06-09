"""Tests for the real analyst executor (the agent-spawn bridge).

No real ``codex`` and no real data: we inject a fake ``runner`` returning a
stand-in ``CodexResult`` and assert the executor (1) parses the result.md JSON
verdict contract, (2) maps verdicts onto the registry vocabulary, (3) NEVER
fabricates a verdict on a failed/empty spawn (leaves the idea OPEN), and (4)
honors the injectable agentic boundary.
"""
from __future__ import annotations

from types import SimpleNamespace

from research.lab.executors import make_codex_executor, _result_json


def _codex(stdout="", files=None, returncode=0, error=None):
    return SimpleNamespace(stdout=stdout, files=files or {}, returncode=returncode, error=error)


def _assignment(hid="h1"):
    return {"hypothesis_id": hid, "market": "total", "mechanism": "m",
            "signal_desc": "s", "direction": "over", "brief": None, "toolkit": {}}


def test_parses_result_md_verdict():
    captured = {}

    def runner(prompt, workdir, **kw):
        captured["prompt"] = prompt
        return _codex(files={"result.md": 'ran it.\n```json\n'
                             '{"market":"total","gate_passed":false,'
                             '"real_edge":false,"verdict":"DEAD","results":{"cents":-1.0}}\n```'})

    ex = make_codex_executor(runner=runner)
    out = ex(_assignment("h1"))
    assert out["executed"] is True
    assert out["verdict"] == "DEAD"
    assert out["status"] == "done"
    assert out["results"] == {"cents": -1.0}
    assert out["hypothesis_id"] == "h1"
    # the assignment is embedded in the prompt handed to the agent
    assert "h1" in captured["prompt"] and "assignment" in captured["prompt"].lower()


def test_verdict_uppercased_and_validated():
    def runner(p, w, **k):
        return _codex(stdout='```json\n{"verdict":"promote"}\n```')

    out = make_codex_executor(runner=runner)(_assignment())
    assert out["verdict"] == "PROMOTE" and out["status"] == "done"


def test_unknown_verdict_dropped_to_none():
    # An agent-invented verdict is NOT authoritative — gate's vocabulary wins.
    def runner(p, w, **k):
        return _codex(stdout='```json\n{"verdict":"LOOKS_GREAT","results":{"x":1}}\n```')

    out = make_codex_executor(runner=runner)(_assignment())
    assert out["verdict"] is None
    assert out["status"] == "running"          # no valid verdict -> stays running
    assert out["results"] == {"x": 1}          # evidence still captured
    assert out["executed"] is True


def test_spawn_failure_never_fabricates():
    def runner(p, w, **k):
        return _codex(returncode=127, error="codex binary not found on PATH")

    out = make_codex_executor(runner=runner)(_assignment("h9"))
    assert out["executed"] is False
    assert out["verdict"] is None
    assert out["status"] == "open"             # re-tryable, not silently DEAD
    assert "spawn failed" in out["results"]["note"]


def test_no_parseable_block_is_not_a_verdict():
    def runner(p, w, **k):
        return _codex(stdout="I thought about it but wrote no JSON block.")

    out = make_codex_executor(runner=runner)(_assignment())
    assert out["executed"] is False and out["verdict"] is None
    assert out["status"] == "open"


def test_nested_results_block_preferred():
    def runner(p, w, **k):
        return _codex(stdout='```json\n{"verdict":"PROMISING","results":'
                             '{"cents":2.1,"ci_lo":0.3},"market":"spread"}\n```')

    out = make_codex_executor(runner=runner)(_assignment())
    assert out["verdict"] == "PROMISING"
    assert out["results"] == {"cents": 2.1, "ci_lo": 0.3}   # control keys stripped


def test_result_json_takes_trailing_block():
    files = {"result.md": '```json\n{"verdict":"DEAD"}\n```\nmore\n'
                           '```json\n{"verdict":"PROMOTE"}\n```'}
    assert _result_json("", files)["verdict"] == "PROMOTE"


def test_runner_receives_model_and_effort():
    seen = {}

    def runner(prompt, workdir, *, model=None, effort=None, timeout_sec=None):
        seen.update(model=model, effort=effort, timeout_sec=timeout_sec)
        return _codex(stdout='```json\n{"verdict":"DEAD"}\n```')

    make_codex_executor(runner=runner, model="opus", effort="medium",
                        timeout_sec=42)(_assignment())
    assert seen == {"model": "opus", "effort": "medium", "timeout_sec": 42}
