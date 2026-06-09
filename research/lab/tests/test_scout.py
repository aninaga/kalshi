"""Tests for research.lab.scout — agent ORIGINATION (no hardcoded ideas).

The scout's ideas come from the proposer (a live agent by default); here we
inject a stub proposer + stub EDA so the test is deterministic and offline.
"""
from __future__ import annotations

import os
import tempfile

from research.lab import scout
from research.lab.types import TOTAL


def _store():
    return os.path.join(tempfile.mkdtemp(), "h.jsonl")


def _stub_eda(market, **kw):
    return {"market": market, "n_games": 10, "notes": ["calibration bias +0.04 in [0.10,0.20)"]}


def test_propose_registers_agent_ideas():
    path = _store()
    proposed = [
        {"market": TOTAL, "mechanism": "agent idea one", "signal_desc": "sig1", "direction": "over"},
        {"market": TOTAL, "mechanism": "agent idea two", "signal_desc": "sig2", "direction": "under"},
    ]
    new = scout.propose(market=TOTAL, proposer=lambda *a, **k: proposed,
                        registry_path=path, eda_fn=_stub_eda)
    assert len(new) == 2
    from research.lab import hypothesis as H
    assert len(H.query(path=path)) == 2
    assert {h.mechanism for h in H.query(path=path)} == {"agent idea one", "agent idea two"}


def test_propose_respects_max_new():
    path = _store()
    proposed = [{"market": TOTAL, "mechanism": f"idea {i}", "signal_desc": "s",
                 "direction": "over"} for i in range(10)]
    new = scout.propose(market=TOTAL, proposer=lambda *a, **k: proposed, max_new=3,
                        registry_path=path, eda_fn=_stub_eda)
    assert len(new) == 3


def test_propose_dedupes_against_registry():
    path = _store()
    p = [{"market": TOTAL, "mechanism": "same idea", "signal_desc": "s", "direction": "over"}]
    scout.propose(market=TOTAL, proposer=lambda *a, **k: p, registry_path=path, eda_fn=_stub_eda)
    scout.propose(market=TOTAL, proposer=lambda *a, **k: p, registry_path=path, eda_fn=_stub_eda)
    from research.lab import hypothesis as H
    assert len(H.query(path=path)) == 1   # same hash -> deduped


def test_propose_empty_when_agent_returns_nothing():
    path = _store()
    new = scout.propose(market=TOTAL, proposer=lambda *a, **k: [],
                        registry_path=path, eda_fn=_stub_eda)
    assert new == []   # no agent ideas -> nothing canned is substituted


def test_parse_proposals_extracts_json_block():
    text = 'blah\n```json\n[{"market":"total","mechanism":"m"}]\n```\n'
    out = scout._parse_proposals(text)
    assert out == [{"market": "total", "mechanism": "m"}]
