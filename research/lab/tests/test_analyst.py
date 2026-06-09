"""Tests for the autonomous analyst harness.

Standalone: mocks the sibling ``lab.hypothesis`` module and ``agents.usage.limits``,
and injects a fake executor — no real agent spawn, no real limits binary, no
real data. Asserts the harness claims N open hypotheses, calls the executor, and
records verdicts back to the registry.
"""
from __future__ import annotations

import sys
import types as _pytypes

import pytest

from research.lab import analyst
from research.lab.types import TOTAL, SPREAD, Hypothesis


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeRegistry:
    """In-memory stand-in for ``research.lab.hypothesis``."""

    def __init__(self, hyps):
        self.hyps = {h.id or h.hash(): h for h in hyps}
        for k, h in self.hyps.items():
            h.id = k
        self.claims: list[tuple[str, str]] = []
        self.updates: list[dict] = []
        self.seeded = 0

    def open_hypotheses(self, path=None):
        return [h for h in self.hyps.values() if h.status == "open"]

    def claim(self, hyp_id, agent, path=None):
        self.claims.append((hyp_id, agent))
        h = self.hyps[hyp_id]
        h.status = "running"
        return h

    def update(self, hyp_id, *, verdict=None, results=None, status=None, path=None):
        self.updates.append(
            {"id": hyp_id, "verdict": verdict, "results": results, "status": status}
        )
        h = self.hyps[hyp_id]
        if status is not None:
            h.status = status
        if verdict is not None:
            h.verdict = verdict
        if results is not None:
            h.results = results
        return h

    def seed_defaults(self, path=None):
        self.seeded += 1
        if not self.hyps:
            h = _hyp("seeded mechanism", TOTAL)
            h.id = h.hash()
            self.hyps[h.id] = h


def _hyp(mechanism, market=TOTAL, direction="over", status="open"):
    return Hypothesis(
        market=market,
        mechanism=mechanism,
        signal_desc="some observable signal",
        direction=direction,
        status=status,
    )


@pytest.fixture
def patch_registry(monkeypatch):
    """Install a FakeRegistry as ``research.lab.hypothesis`` (lazily imported)."""

    def _install(hyps):
        reg = FakeRegistry(hyps)
        mod = _pytypes.ModuleType("research.lab.hypothesis")
        mod.open_hypotheses = reg.open_hypotheses
        mod.claim = reg.claim
        mod.update = reg.update
        mod.seed_defaults = reg.seed_defaults
        monkeypatch.setitem(sys.modules, "research.lab.hypothesis", mod)
        return reg

    return _install


@pytest.fixture(autouse=True)
def patch_limits(monkeypatch):
    """Default: limits available and ample so budget never caps in most tests."""
    monkeypatch.setattr(analyst.L, "poll", lambda *a, **k: object())

    def _decide(snap, base, *a, **k):
        d = _pytypes.SimpleNamespace()
        d.gpt55_workers = base
        d.reason = f"fake-ample base={base}"
        d.sleep_until_epoch = None
        return d

    monkeypatch.setattr(analyst.L, "decide_workers", _decide)


def _verdict_executor(verdict="PROMOTE", real_edge=2.1):
    def _exec(assignment):
        return {
            "hypothesis_id": assignment["hypothesis_id"],
            "executed": True,
            "verdict": verdict,
            "results": {"real_edge_cents": real_edge, "gate_passed": verdict == "PROMOTE"},
        }

    return _exec


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_claims_calls_executor_and_updates(patch_registry):
    reg = patch_registry([_hyp("m1"), _hyp("m2", market=SPREAD), _hyp("m3")])
    calls = []

    def fake_exec(assignment):
        calls.append(assignment)
        return _verdict_executor("PROMISING")(assignment)

    summary = analyst.run_analyst(
        "be an analyst", max_ideas=3, budget_aware=False, executor=fake_exec
    )

    assert summary["processed"] == 3
    assert len(calls) == 3
    assert len(reg.claims) == 3
    # every claimed id got an update with a verdict
    assert len(reg.updates) == 3
    assert all(u["verdict"] == "PROMISING" for u in reg.updates)
    assert all(v == "PROMISING" for v in summary["verdicts"].values())
    # assignment carries the toolkit handle + the brief + hypothesis fields
    a0 = calls[0]
    assert a0["brief"] == "be an analyst"
    assert a0["toolkit"]["import"] == "research.lab"
    assert a0["mechanism"] in {"m1", "m2", "m3"}


def test_respects_max_ideas_cap(patch_registry):
    reg = patch_registry([_hyp(f"m{i}") for i in range(5)])
    summary = analyst.run_analyst(
        max_ideas=2, budget_aware=False, executor=_verdict_executor("DEAD")
    )
    assert summary["processed"] == 2
    assert len(reg.claims) == 2
    assert summary["open_remaining"] == 3  # 5 open - 2 claimed/done


def test_smoke_two_processed(patch_registry):
    """E2E synthetic smoke from the recipe."""
    patch_registry([_hyp("a"), _hyp("b"), _hyp("c")])
    fake = _verdict_executor("PROMOTE")
    summary = analyst.run_analyst(max_ideas=2, budget_aware=False, executor=fake)
    assert summary["processed"] == 2
    assert len(summary["results"]) == 2
    assert all(r["executed"] for r in summary["results"])


def test_seeds_when_registry_empty(patch_registry):
    reg = patch_registry([])
    summary = analyst.run_analyst(
        max_ideas=3, budget_aware=False, executor=_verdict_executor()
    )
    assert reg.seeded == 1
    assert summary["processed"] == 1  # one seeded idea got worked


def test_market_filter(patch_registry):
    reg = patch_registry([_hyp("a", market=TOTAL), _hyp("b", market=SPREAD), _hyp("c", market=TOTAL)])
    summary = analyst.run_analyst(
        max_ideas=5, market=SPREAD, budget_aware=False, executor=_verdict_executor()
    )
    assert summary["processed"] == 1
    assert reg.claims == [(list(reg.hyps.values())[1].id, analyst.AGENT)]


def test_budget_caps_processing(patch_registry, monkeypatch):
    reg = patch_registry([_hyp(f"m{i}") for i in range(4)])

    def _decide(snap, base, *a, **k):
        d = _pytypes.SimpleNamespace()
        d.gpt55_workers = 1  # governor throttles hard
        d.reason = "throttled"
        d.sleep_until_epoch = None
        return d

    monkeypatch.setattr(analyst.L, "decide_workers", _decide)
    summary = analyst.run_analyst(max_ideas=4, budget_aware=True, executor=_verdict_executor())
    assert summary["budget"]["allowed"] == 1
    assert summary["processed"] == 1


def test_budget_tapped_stops_early(patch_registry, monkeypatch):
    reg = patch_registry([_hyp("m1"), _hyp("m2")])

    def _decide(snap, base, *a, **k):
        d = _pytypes.SimpleNamespace()
        d.gpt55_workers = 0  # tapped
        d.reason = "tapped, pause until reset"
        d.sleep_until_epoch = 123.0
        return d

    monkeypatch.setattr(analyst.L, "decide_workers", _decide)
    summary = analyst.run_analyst(max_ideas=2, budget_aware=True, executor=_verdict_executor())
    assert summary["processed"] == 0
    assert reg.claims == []  # never even touched the registry


def test_no_verdict_leaves_running(patch_registry):
    reg = patch_registry([_hyp("a")])

    def _exec(assignment):
        return {"executed": True}  # no verdict

    analyst.run_analyst(max_ideas=1, budget_aware=False, executor=_exec)
    assert reg.updates[0]["status"] == "running"
    assert reg.updates[0]["verdict"] is None


def test_default_noop_executor(patch_registry):
    reg = patch_registry([_hyp("a")])
    summary = analyst.run_analyst(max_ideas=1, budget_aware=False)  # no executor
    assert summary["processed"] == 1
    assert summary["results"][0]["executed"] is False
    # no-op executor reports status open -> registry update preserves that
    assert reg.updates[0]["status"] == "open"


def test_budget_aware_polls_limits(patch_registry, monkeypatch):
    patch_registry([_hyp("a")])
    polled = {"n": 0}

    monkeypatch.setattr(analyst.L, "poll", lambda *a, **k: polled.__setitem__("n", polled["n"] + 1) or object())
    summary = analyst.run_analyst(max_ideas=1, budget_aware=True, executor=_verdict_executor())
    assert polled["n"] == 1
    assert "fake-ample" in summary["budget"]["reason"]
