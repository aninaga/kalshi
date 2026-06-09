"""Tests for the heartbeat scheduler (the cadence engine).

No spawning, no waiting: we monkeypatch ``run_analyst`` to a deterministic stub
and inject a no-op ``sleep``. We assert: --once fires exactly one pass; resident
mode honors --max-passes; the idle stop-condition stands the loop down; and
state persists across passes (so cron-driven --once accumulates).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.lab import heartbeat


@pytest.fixture
def patched(monkeypatch):
    """Replace run_analyst with a scripted sequence of pass-summaries."""

    def _install(processed_seq):
        calls = {"n": 0, "ledger_paths": [], "executors": []}
        seq = list(processed_seq)

        def fake_run_analyst(brief=None, *, max_ideas=3, market=None,
                             budget_aware=True, executor=None, ledger_path=None):
            i = calls["n"]
            calls["n"] += 1
            calls["ledger_paths"].append(ledger_path)
            calls["executors"].append(executor)
            processed = seq[i] if i < len(seq) else 0
            return {"processed": processed, "open_remaining": max(0, 3 - processed),
                    "budget": {"reason": "fake"}, "verdicts": {}}

        monkeypatch.setattr(heartbeat, "run_analyst", fake_run_analyst)
        return calls

    return _install


def test_once_fires_single_pass(patched, tmp_path):
    calls = patched([2])
    state = heartbeat.run_heartbeat(
        once=True, executor=lambda a: {}, state_path=str(tmp_path / "s.json"),
        ledger_path=str(tmp_path / "l.jsonl"), sleep=lambda s: None, log=lambda m: None)
    assert calls["n"] == 1
    assert state.passes_done == 1 and state.total_processed == 2


def test_max_passes_caps_resident_loop(patched, tmp_path):
    calls = patched([1, 1, 1, 1, 1])
    slept = []
    state = heartbeat.run_heartbeat(
        once=False, max_passes=3, stop_after_idle=0, executor=lambda a: {},
        state_path=str(tmp_path / "s.json"), sleep=lambda s: slept.append(s), log=lambda m: None)
    assert calls["n"] == 3 and state.passes_done == 3
    assert len(slept) == 2          # sleeps BETWEEN passes, not after the last


def test_idle_stop_condition(patched, tmp_path):
    # Two productive passes, then idle: should stand down after 2 idle passes.
    calls = patched([1, 1, 0, 0, 0, 0])
    state = heartbeat.run_heartbeat(
        once=False, max_passes=0, stop_after_idle=2, executor=lambda a: {},
        state_path=str(tmp_path / "s.json"), sleep=lambda s: None, log=lambda m: None)
    assert calls["n"] == 4          # 1,1,0,0 -> 2 consecutive idle -> stop
    assert state.consecutive_idle == 2
    assert state.total_processed == 2


def test_state_persists_across_once_calls(patched, tmp_path):
    sp = str(tmp_path / "s.json")
    patched([2, 3])
    heartbeat.run_heartbeat(once=True, executor=lambda a: {}, state_path=sp,
                            sleep=lambda s: None, log=lambda m: None)
    # second --once invocation must resume from persisted state
    patched([3])
    state = heartbeat.run_heartbeat(once=True, executor=lambda a: {}, state_path=sp,
                                    sleep=lambda s: None, log=lambda m: None)
    assert state.passes_done == 2
    assert state.total_processed == 5
    on_disk = json.loads(Path(sp).read_text())
    assert on_disk["passes_done"] == 2 and on_disk["total_processed"] == 5


def test_ledger_and_executor_threaded_through(patched, tmp_path):
    calls = patched([1])
    sentinel = lambda a: {"v": 1}
    heartbeat.run_heartbeat(once=True, executor=sentinel, ledger_path="L.jsonl",
                            state_path=str(tmp_path / "s.json"),
                            sleep=lambda s: None, log=lambda m: None)
    assert calls["ledger_paths"] == ["L.jsonl"]
    assert calls["executors"] == [sentinel]


def test_corrupt_state_starts_fresh(tmp_path):
    sp = tmp_path / "s.json"
    sp.write_text("{not valid json")
    st = heartbeat.HeartbeatState.load(str(sp))
    assert st.passes_done == 0 and st.total_processed == 0
