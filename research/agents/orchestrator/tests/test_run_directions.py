"""Orchestrator direction selection is AGENT-DRIVEN, not hardcoded.

`_select_directions` originates via `research.lab.scout` (empty pool) and ranks
via `research.lab.director`; there is no hardcoded `DIRECTION_ROTATION` to fall
back to (it is empty by design). The layer is stubbed via ``sys.modules``.
"""
from __future__ import annotations

import sys
import types as pytypes

import pytest

from research.agents.orchestrator import run as orun
from research.lab.types import TOTAL, Hypothesis


def _install(monkeypatch, *, open_hyps=None, selected=None):
    hmod = pytypes.ModuleType("research.lab.hypothesis")
    hmod.open_hypotheses = lambda *a, **k: list(open_hyps or [])
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", hmod)

    dmod = pytypes.ModuleType("research.lab.director")
    dmod.select = lambda k=8, **kw: list(selected or [])
    monkeypatch.setitem(sys.modules, "research.lab.director", dmod)

    smod = pytypes.ModuleType("research.lab.scout")
    calls = {"propose": 0}
    smod.propose = lambda *a, **k: calls.__setitem__("propose", calls["propose"] + 1) or []
    monkeypatch.setitem(sys.modules, "research.lab.scout", smod)
    return calls


def test_no_hardcoded_rotation():
    assert orun.DIRECTION_ROTATION == []


def test_select_uses_director(monkeypatch):
    h = Hypothesis(market=TOTAL, mechanism="anchors on the pregame line.",
                   signal_desc="anchoring_gap", direction="over", id="abc123", status="open")
    _install(monkeypatch, open_hyps=[h], selected=[h])
    rot = orun._select_directions()
    assert len(rot) == 1 and rot[0]["name"] == "abc123" and rot[0]["market"] == TOTAL
    assert "anchors on the pregame line" in rot[0]["blurb"]
    assert "Signal: anchoring_gap" in rot[0]["blurb"]


def test_select_scouts_when_pool_empty(monkeypatch):
    calls = _install(monkeypatch, open_hyps=[], selected=[])
    orun._select_directions(originate=True)
    assert calls["propose"] == 1


def test_select_empty_when_layer_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "research.lab.director", None)  # ImportError
    assert orun._select_directions() == []   # no hardcoded fallback


def test_pick_direction_rotates_over_director(monkeypatch):
    hyps = [Hypothesis(market=TOTAL, mechanism="m1", signal_desc="s1", direction="over",
                       id="h1", status="open"),
            Hypothesis(market=TOTAL, mechanism="m2", signal_desc="s2", direction="under",
                       id="h2", status="open")]
    _install(monkeypatch, open_hyps=hyps, selected=hyps)
    rotation = orun._select_directions()
    state: dict = {}
    names = [orun._pick_direction(state, rotation)["name"] for _ in range(3)]
    assert names == ["h1", "h2", "h1"]


def test_pick_direction_raises_when_empty(monkeypatch):
    _install(monkeypatch, open_hyps=[], selected=[])
    with pytest.raises(RuntimeError):
        orun._pick_direction({}, [])


def test_registry_direction_composes_direction_md():
    h = Hypothesis(market=TOTAL, mechanism="Registry mechanism text.", signal_desc="some signal",
                   direction="over", id="hyp_md", status="open")
    d = orun._hypothesis_to_direction(h)
    md = orun._compose_direction_md(d, {"total_trials": 0, "top_specs": []})
    assert "hyp_md" in md and "Registry mechanism text." in md
