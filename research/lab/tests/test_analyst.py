"""Tests for the autonomous analyst harness (agent-driven selection layer).

Standalone: stubs the sibling ``lab.hypothesis`` registry, the ``lab.director``
(prioritizer) and ``lab.scout`` (originator), and injects a fake executor — no
real agent spawn, no real data. The harness must: ORIGINATE via the scout when
the registry is empty, PRIORITIZE via the director (never FIFO/hardcoded), then
claim/execute/update each chosen idea.
"""
from __future__ import annotations

import sys
import types as _pytypes

import pytest

from research.lab import analyst
from research.lab.types import SPREAD, TOTAL, Hypothesis


class FakeRegistry:
    """In-memory stand-in for ``research.lab.hypothesis``."""

    DEFAULT_PATH = "memory"

    def __init__(self, hyps):
        self.hyps = {h.id or h.hash(): h for h in hyps}
        for k, h in self.hyps.items():
            h.id = k
        self.claims: list[tuple[str, str]] = []
        self.updates: list[dict] = []

    def open_hypotheses(self, path=None):
        return [h for h in self.hyps.values() if h.status == "open"]

    def query(self, status=None, market=None, path=None):
        return list(self.hyps.values())

    def register(self, h, path=None):
        h.id = h.id or h.hash()
        self.hyps[h.id] = h
        return h

    def claim(self, hyp_id, agent, path=None):
        self.claims.append((hyp_id, agent))
        h = self.hyps[hyp_id]; h.status = "running"; return h

    def update(self, hyp_id, *, verdict=None, results=None, status=None, path=None):
        self.updates.append({"id": hyp_id, "verdict": verdict, "results": results, "status": status})
        h = self.hyps[hyp_id]
        if status is not None: h.status = status
        if verdict is not None: h.verdict = verdict
        if results is not None: h.results = results
        return h


def _hyp(mechanism, market=TOTAL, direction="over", status="open"):
    return Hypothesis(market=market, mechanism=mechanism,
                      signal_desc="some observable signal", direction=direction, status=status)


@pytest.fixture
def patch_layer(monkeypatch):
    """Install fake hypothesis registry + director (rank=identity over open,
    market-filtered, k-capped) + scout (registers one originated idea)."""

    # Import the package so the fakes can be bound as ATTRIBUTES on it, not just
    # in sys.modules. ``run_analyst`` does ``from research.lab import director``,
    # which resolves the package attribute first — if an earlier test bound the
    # REAL submodule there, a sys.modules-only override is silently bypassed (the
    # real scout/director then run on real data). Patching both wins deterministically.
    import importlib

    _lab_pkg = importlib.import_module("research.lab")

    def _install(hyps):
        reg = FakeRegistry(hyps)
        hmod = _pytypes.ModuleType("research.lab.hypothesis")
        for name in ("DEFAULT_PATH", "open_hypotheses", "query", "register", "claim", "update"):
            setattr(hmod, name, getattr(reg, name))
        monkeypatch.setitem(sys.modules, "research.lab.hypothesis", hmod)
        monkeypatch.setattr(_lab_pkg, "hypothesis", hmod, raising=False)

        # director.select: agent stand-in — returns open hyps (market-filtered), capped at k.
        dmod = _pytypes.ModuleType("research.lab.director")

        def _select(k=3, market=None, registry_path=None, ledger_path=None, brief=None):
            rows = reg.open_hypotheses()
            if market is not None:
                rows = [h for h in rows if h.market == market]
            return rows[:k]

        dmod.select = _select
        dmod.incorporate_results = lambda *a, **k: {"applied": 0}
        monkeypatch.setitem(sys.modules, "research.lab.director", dmod)
        monkeypatch.setattr(_lab_pkg, "director", dmod, raising=False)

        # scout.propose: agent stand-in — originates one idea into the empty store.
        smod = _pytypes.ModuleType("research.lab.scout")
        reg.scouted = 0

        def _propose(market=None, brief=None, registry_path=None, **kw):
            reg.scouted += 1
            return [reg.register(_hyp("originated-by-scout", market or TOTAL))]

        smod.propose = _propose
        monkeypatch.setitem(sys.modules, "research.lab.scout", smod)
        monkeypatch.setattr(_lab_pkg, "scout", smod, raising=False)
        return reg

    return _install


def _verdict_executor(verdict="PROMOTE"):
    def _exec(assignment):
        return {"hypothesis_id": assignment["hypothesis_id"], "executed": True,
                "verdict": verdict, "results": {"gate_passed": verdict == "PROMOTE"}}
    return _exec


def test_director_picks_executor_runs_updates(patch_layer):
    reg = patch_layer([_hyp("m1"), _hyp("m2", market=SPREAD), _hyp("m3")])
    calls = []

    def fake_exec(assignment):
        calls.append(assignment)
        return _verdict_executor("PROMISING")(assignment)

    summary = analyst.run_analyst("be an analyst", max_ideas=3, budget_aware=False, executor=fake_exec)
    assert summary["processed"] == 3 and len(calls) == 3 and len(reg.claims) == 3
    assert all(u["verdict"] == "PROMISING" for u in reg.updates)
    a0 = calls[0]
    assert a0["brief"] == "be an analyst" and a0["toolkit"]["import"] == "research.lab"


def test_respects_max_ideas_cap(patch_layer):
    reg = patch_layer([_hyp(f"m{i}") for i in range(5)])
    summary = analyst.run_analyst(max_ideas=2, budget_aware=False, executor=_verdict_executor("DEAD"))
    assert summary["processed"] == 2 and len(reg.claims) == 2


def test_scouts_when_registry_empty(patch_layer):
    reg = patch_layer([])
    summary = analyst.run_analyst(max_ideas=3, budget_aware=False, executor=_verdict_executor())
    assert reg.scouted == 1                      # originated via the scout, not canned
    assert summary["processed"] == 1             # the one originated idea got worked


def test_market_filter(patch_layer):
    reg = patch_layer([_hyp("a", TOTAL), _hyp("b", SPREAD), _hyp("c", TOTAL)])
    summary = analyst.run_analyst(max_ideas=5, market=SPREAD, budget_aware=False,
                                  executor=_verdict_executor())
    assert summary["processed"] == 1
    assert reg.claims and reg.hyps[reg.claims[0][0]].market == SPREAD


def test_no_verdict_leaves_running(patch_layer):
    reg = patch_layer([_hyp("a")])
    analyst.run_analyst(max_ideas=1, budget_aware=False, executor=lambda a: {"executed": True})
    assert reg.updates[0]["status"] == "running" and reg.updates[0]["verdict"] is None


def test_default_noop_executor(patch_layer):
    reg = patch_layer([_hyp("a")])
    summary = analyst.run_analyst(max_ideas=1, budget_aware=False)
    assert summary["processed"] == 1 and summary["results"][0]["executed"] is False
