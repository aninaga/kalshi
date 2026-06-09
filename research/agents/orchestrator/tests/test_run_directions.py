"""Tests for registry-backed direction selection in the orchestrator (Unit 11).

Covers both paths of ``_select_directions``:
  * registry-backed — ``research.lab.hypothesis.open_hypotheses()`` returns OPEN
    hypotheses, mapped onto the ``{name, blurb}`` rotation entries.
  * legacy fallback — registry module absent or empty, so the hard-coded
    ``DIRECTION_ROTATION`` is used.

Also verifies ``_pick_direction`` rotates over the registry-backed rotation and
that mapped entries still compose a valid ``direction.md``.
"""
from __future__ import annotations

import sys
import types as pytypes

from research.agents.orchestrator import run as orun
from research.lab.types import TOTAL, Hypothesis


def _install_fake_hypothesis(monkeypatch, open_hyps):
    fake = pytypes.ModuleType("research.lab.hypothesis")
    fake.open_hypotheses = lambda *a, **k: list(open_hyps)
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", fake)
    return fake


def test_select_directions_falls_back_when_registry_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", None)
    rot = orun._select_directions()
    assert rot == list(orun.DIRECTION_ROTATION)
    assert rot is not orun.DIRECTION_ROTATION


def test_select_directions_falls_back_when_registry_empty(monkeypatch):
    _install_fake_hypothesis(monkeypatch, [])
    assert orun._select_directions() == list(orun.DIRECTION_ROTATION)


def test_select_directions_falls_back_when_registry_raises(monkeypatch):
    fake = pytypes.ModuleType("research.lab.hypothesis")

    def _boom(*a, **k):
        raise RuntimeError("registry I/O failed")

    fake.open_hypotheses = _boom
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", fake)
    assert orun._select_directions() == list(orun.DIRECTION_ROTATION)


def test_select_directions_uses_registry_when_present(monkeypatch):
    h = Hypothesis(
        market=TOTAL,
        mechanism="The total anchors on the pregame line and under-reacts to live pace.",
        signal_desc="anchoring_gap = pace_projection - implied_level",
        direction="over",
        id="abc123",
        status="open",
    )
    _install_fake_hypothesis(monkeypatch, [h])
    rot = orun._select_directions()
    assert len(rot) == 1
    d = rot[0]
    assert d["name"] == "abc123"
    assert d["market"] == TOTAL
    assert "anchors on the pregame line" in d["blurb"]
    assert "Signal: anchoring_gap" in d["blurb"]
    assert "Pre-registered direction: over" in d["blurb"]


def test_pick_direction_rotates_over_registry(monkeypatch):
    hyps = [
        Hypothesis(market=TOTAL, mechanism="m1", signal_desc="s1",
                   direction="over", id="h1", status="open"),
        Hypothesis(market=TOTAL, mechanism="m2", signal_desc="s2",
                   direction="under", id="h2", status="open"),
    ]
    _install_fake_hypothesis(monkeypatch, hyps)
    rotation = orun._select_directions()
    state: dict = {}
    first = orun._pick_direction(state, rotation)
    second = orun._pick_direction(state, rotation)
    third = orun._pick_direction(state, rotation)
    assert [first["name"], second["name"], third["name"]] == ["h1", "h2", "h1"]
    assert state["direction_rotation_index"] == 1


def test_pick_direction_defaults_to_select_directions(monkeypatch):
    # When no rotation passed, it falls back to _select_directions (legacy here).
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", None)
    state: dict = {}
    d = orun._pick_direction(state)
    assert d == orun.DIRECTION_ROTATION[0]


def test_registry_direction_composes_direction_md(monkeypatch):
    h = Hypothesis(
        market=TOTAL,
        mechanism="Registry mechanism text.",
        signal_desc="some signal",
        direction="over",
        id="hyp_md",
        status="open",
    )
    d = orun._hypothesis_to_direction(h)
    md = orun._compose_direction_md(d, {"total_trials": 0, "top_specs": []})
    assert "hyp_md" in md
    assert "Registry mechanism text." in md
