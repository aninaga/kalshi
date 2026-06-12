"""Tests for research.lab.director — agent PRIORITIZATION + feedback (no hardcoded picks)."""
from __future__ import annotations

import json
import os
import tempfile

from research.lab import director
from research.lab import hypothesis as H
from research.lab.types import TOTAL, Hypothesis


def _store():
    return os.path.join(tempfile.mkdtemp(), "h.jsonl")


def _reg(path, n=3):
    ids = []
    for i in range(n):
        h = H.register(Hypothesis(market=TOTAL, mechanism=f"idea {i}",
                                  signal_desc="s", direction="over"), path=path)
        ids.append(h.id)
    return ids


def test_select_uses_ranker_order():
    path = _store()
    _reg(path, 3)
    open_ids = [h.id for h in H.open_hypotheses(path=path)]  # registry's open order
    # stub ranker reverses the pool order
    rank = lambda open_hyps, ledger, brief: [h.id for h in reversed(open_hyps)]
    chosen = director.select(k=2, ranker=rank, registry_path=path)
    assert [h.id for h in chosen] == list(reversed(open_ids))[:2]


def test_select_empty_pool_returns_empty():
    # no canned fallback — empty registry yields no picks
    assert director.select(k=3, registry_path=_store()) == []


def test_default_ranker_degrades_to_novelty_without_codex():
    path = _store()
    _reg(path, 3)
    # codex absent in CI -> default_ranker returns a stable order over the
    # AGENT-ORIGINATED pool (never a hardcoded preference)
    chosen = director.select(k=3, registry_path=path)
    assert len(chosen) == 3
    assert {h.market for h in chosen} == {TOTAL}


def test_incorporate_results_folds_verdicts_into_registry():
    path = _store()
    ids = _reg(path, 2)
    ledger = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")
    with open(ledger, "w") as fh:
        fh.write(json.dumps({"hypothesis_id": ids[0], "verdict": "DEAD"}) + "\n")
        fh.write(json.dumps({"hypothesis_id": ids[1], "verdict": "PROMISING"}) + "\n")
    out = director.incorporate_results(ledger, registry_path=path)
    assert out["applied"] == 2
    rows = {h.id: h for h in H.query(path=path)}
    assert rows[ids[0]].verdict == "DEAD" and rows[ids[0]].status == "done"
    # PROMISING stays open for deepening
    assert rows[ids[1]].verdict == "PROMISING" and rows[ids[1]].status == "open"


def test_incorporate_results_ignores_unknown_ids():
    path = _store()
    _reg(path, 1)
    ledger = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")
    with open(ledger, "w") as fh:
        fh.write(json.dumps({"hypothesis_id": "nonexistent", "verdict": "DEAD"}) + "\n")
    out = director.incorporate_results(ledger, registry_path=path)
    assert out["applied"] == 0


def test_default_ranker_is_novelty_order():
    # model-agnostic default: stable novelty order (most-recent first), no model
    from research.lab.types import Hypothesis
    a = Hypothesis(market="total", mechanism="a", signal_desc="", direction="over",
                   id="a", created="2025-01-01")
    b = Hypothesis(market="total", mechanism="b", signal_desc="", direction="over",
                   id="b", created="2025-02-01")
    assert director.default_ranker([a, b], [], None) == ["b", "a"]
