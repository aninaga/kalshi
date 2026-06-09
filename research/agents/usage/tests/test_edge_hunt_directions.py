"""Direction selection in the edge-hunt loop is AGENT-DRIVEN, not hardcoded.

`_next_directions` originates via `research.lab.scout` (when the open pool is
empty) and prioritizes via `research.lab.director` — there is no hardcoded menu
to fall back to (``DIRECTIONS`` is empty by design). These tests stub the layer
via ``sys.modules`` so they pass standalone.
"""
from __future__ import annotations

import sys
import types as pytypes

from research.agents.usage import edge_hunt_loop as ehl
from research.lab.types import TOTAL, Hypothesis


def _install(monkeypatch, *, open_hyps=None, selected=None, raise_select=False):
    hmod = pytypes.ModuleType("research.lab.hypothesis")
    hmod.open_hypotheses = lambda *a, **k: list(open_hyps or [])
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", hmod)

    dmod = pytypes.ModuleType("research.lab.director")
    if raise_select:
        def _sel(*a, **k):
            raise RuntimeError("boom")
    else:
        def _sel(k=3, market=None, registry_path=None, ledger_path=None, brief=None):
            return list(selected or [])
    dmod.select = _sel
    monkeypatch.setitem(sys.modules, "research.lab.director", dmod)

    smod = pytypes.ModuleType("research.lab.scout")
    calls = {"propose": 0}
    smod.propose = lambda *a, **k: calls.__setitem__("propose", calls["propose"] + 1) or []
    monkeypatch.setitem(sys.modules, "research.lab.scout", smod)
    return calls


def test_no_hardcoded_directions():
    # The menu is intentionally empty — strategies come from the agent layer.
    assert ehl.DIRECTIONS == []


def test_next_directions_uses_director(monkeypatch):
    h = Hypothesis(market=TOTAL, mechanism="line anchors on pregame pace.",
                   signal_desc="anchoring_gap", direction="over", id="abc123", status="open")
    _install(monkeypatch, open_hyps=[h], selected=[h])
    dirs = ehl._next_directions(1)
    assert len(dirs) == 1 and isinstance(dirs[0], ehl.Direction)
    assert dirs[0].key == "abc123" and dirs[0].market == TOTAL
    assert "anchors on pregame" in dirs[0].hypothesis and "Signal: anchoring_gap" in dirs[0].hypothesis


def test_next_directions_scouts_when_pool_empty(monkeypatch):
    calls = _install(monkeypatch, open_hyps=[], selected=[])
    ehl._next_directions(1, originate=True)
    assert calls["propose"] == 1            # cold start originates via the scout


def test_next_directions_no_scout_when_originate_false(monkeypatch):
    calls = _install(monkeypatch, open_hyps=[], selected=[])
    ehl._next_directions(1, originate=False)
    assert calls["propose"] == 0            # dry-run never invokes the agent


def test_next_directions_empty_when_layer_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "research.lab.director", None)  # ImportError on import
    assert ehl._next_directions(1) == []     # no hardcoded fallback


def test_next_directions_empty_when_select_raises(monkeypatch):
    _install(monkeypatch, open_hyps=[], selected=[], raise_select=True)
    assert ehl._next_directions(1) == []


def test_hypothesis_to_direction_uses_hash_without_id():
    h = Hypothesis(market=TOTAL, mechanism="m.", signal_desc="", direction="under", id="")
    d = ehl._hypothesis_to_direction(h)
    assert d.key == h.hash() and d.key


def test_dry_run_draws_from_director(monkeypatch, tmp_path, capsys):
    h = Hypothesis(market=TOTAL, mechanism="registry-driven mechanism.", signal_desc="z",
                   direction="over", id="hyp_e2e", status="open")
    _install(monkeypatch, open_hyps=[h], selected=[h])
    monkeypatch.setattr(ehl, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(ehl, "LEDGER", tmp_path / "ledger.jsonl")
    seen = []

    def _fake_run_wave(direction, n_workers, base, model, effort, dry_run):
        seen.append(direction)
        return [{"workdir": "w0", "dry_run": True, "direction": direction.key}]

    monkeypatch.setattr(ehl, "run_wave", _fake_run_wave)
    rc = ehl.main(["--waves", "1", "--workers", "1", "--dry-run"])
    assert rc == 0 and len(seen) == 1 and seen[0].key == "hyp_e2e"
    assert "dir=hyp_e2e" in capsys.readouterr().out
