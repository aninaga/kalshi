"""Tests for registry-backed direction selection in the edge-hunt loop (Unit 11).

These cover both paths of ``_select_directions``:
  * registry-backed — ``research.lab.hypothesis.open_hypotheses()`` returns
    OPEN hypotheses, which are mapped onto ``Direction`` rows.
  * legacy fallback — the registry module is absent (not merged in this
    worktree) or returns nothing, so the hard-coded ``DIRECTIONS`` list is used.

The registry module is mocked via ``sys.modules`` so the suite passes standalone
(``research.lab.hypothesis`` is genuinely absent here).
"""
from __future__ import annotations

import sys
import types as pytypes

from research.agents.usage import edge_hunt_loop as ehl
from research.lab.types import TOTAL, Hypothesis


def _install_fake_hypothesis(monkeypatch, open_hyps):
    """Install a fake ``research.lab.hypothesis`` module returning ``open_hyps``."""
    fake = pytypes.ModuleType("research.lab.hypothesis")
    fake.open_hypotheses = lambda *a, **k: list(open_hyps)
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", fake)
    return fake


def test_select_directions_falls_back_when_registry_absent(monkeypatch):
    # Ensure no real/fake registry module is importable.
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", None)
    dirs = ehl._select_directions()
    assert dirs == list(ehl.DIRECTIONS)
    # Legacy entries are returned as a copy, not the module-level list itself.
    assert dirs is not ehl.DIRECTIONS


def test_select_directions_falls_back_when_registry_empty(monkeypatch):
    _install_fake_hypothesis(monkeypatch, [])
    assert ehl._select_directions() == list(ehl.DIRECTIONS)


def test_select_directions_falls_back_when_registry_raises(monkeypatch):
    fake = pytypes.ModuleType("research.lab.hypothesis")

    def _boom(*a, **k):
        raise RuntimeError("registry I/O failed")

    fake.open_hypotheses = _boom
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", fake)
    assert ehl._select_directions() == list(ehl.DIRECTIONS)


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
    dirs = ehl._select_directions()
    assert len(dirs) == 1
    d = dirs[0]
    assert isinstance(d, ehl.Direction)
    assert d.key == "abc123"
    assert d.market == TOTAL
    # The blurb flattens mechanism + signal + pre-registered direction.
    assert "anchors on the pregame line" in d.hypothesis
    assert "Signal: anchoring_gap" in d.hypothesis
    assert "Pre-registered direction: over" in d.hypothesis


def test_hypothesis_without_id_uses_hash(monkeypatch):
    h = Hypothesis(
        market=TOTAL,
        mechanism="Some mechanism.",
        signal_desc="",
        direction="under",
        id="",  # no id assigned yet
        status="open",
    )
    d = ehl._hypothesis_to_direction(h)
    assert d.key == h.hash()
    assert d.key  # non-empty stable hash


def test_dry_run_uses_selected_directions(monkeypatch, tmp_path, capsys):
    """End-to-end-ish: --dry-run plans waves off the registry directions."""
    h = Hypothesis(
        market=TOTAL,
        mechanism="Registry-driven test mechanism.",
        signal_desc="z",
        direction="over",
        id="hyp_e2e",
        status="open",
    )
    _install_fake_hypothesis(monkeypatch, [h])
    # Redirect state/ledger writes into tmp so we don't touch repo data paths.
    monkeypatch.setattr(ehl, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ehl, "LEDGER", tmp_path / "ledger.jsonl")
    # Stub run_wave so we don't depend on the worker prompt file (absent in the
    # worktree); we only assert the loop drew its direction from the registry.
    seen: list[ehl.Direction] = []

    def _fake_run_wave(direction, n_workers, base, model, effort, dry_run):
        seen.append(direction)
        return [{"workdir": "w0", "dry_run": True, "direction": direction.key}]

    monkeypatch.setattr(ehl, "run_wave", _fake_run_wave)
    rc = ehl.main(["--waves", "1", "--workers", "1", "--dry-run"])
    assert rc == 0
    assert len(seen) == 1
    assert seen[0].key == "hyp_e2e"
    out = capsys.readouterr().out
    assert "dir=hyp_e2e" in out
