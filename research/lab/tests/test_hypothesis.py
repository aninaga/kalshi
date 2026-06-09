"""Tests for research.lab.hypothesis — the append-only JSONL registry.

All tests use a TEMP path (``tmp_path``) — never the default store.
"""
from __future__ import annotations

import json

import pytest

from research.lab import hypothesis as H
from research.lab.types import SPREAD, TOTAL, WINNER, Hypothesis


def _store(tmp_path):
    return str(tmp_path / "hypotheses.jsonl")


def _hyp(market=TOTAL, mechanism="m", direction="d", **kw):
    return Hypothesis(market=market, mechanism=mechanism,
                      signal_desc="s", direction=direction, **kw)


# --- register / dedupe ------------------------------------------------------

def test_register_assigns_hash_id(tmp_path):
    path = _store(tmp_path)
    h = _hyp()
    out = H.register(h, path=path)
    assert out.id == h.hash()
    assert out.status == "open"
    assert out.created and out.updated


def test_register_dedupes_by_hash(tmp_path):
    path = _store(tmp_path)
    first = H.register(_hyp(mechanism="Pace anchoring"), path=path)
    # Same idea identity (market|mechanism|direction), different signal_desc/case.
    dup = H.register(
        _hyp(mechanism="  pace ANCHORING ", direction="d"), path=path
    )
    assert dup.id == first.id
    # Returned the EXISTING row (same timestamps), no second append.
    assert dup.created == first.created
    assert len(H.query(path=path)) == 1


def test_register_distinct_ideas_coexist(tmp_path):
    path = _store(tmp_path)
    H.register(_hyp(market=TOTAL, mechanism="a"), path=path)
    H.register(_hyp(market=SPREAD, mechanism="b"), path=path)
    H.register(_hyp(market=WINNER, mechanism="c"), path=path)
    assert len(H.query(path=path)) == 3


def test_register_is_append_only(tmp_path):
    path = _store(tmp_path)
    H.register(_hyp(mechanism="a"), path=path)
    H.register(_hyp(mechanism="b"), path=path)
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    assert len(lines) == 2
    for ln in lines:
        json.loads(ln)  # every line is valid JSON


# --- claim / update transitions --------------------------------------------

def test_claim_sets_running_and_records_agent(tmp_path):
    path = _store(tmp_path)
    h = H.register(_hyp(), path=path)
    claimed = H.claim(h.id, agent="analyst-7", path=path)
    assert claimed.status == "running"
    assert claimed.results["claimed_by"] == "analyst-7"
    # last-write-wins: re-reading reflects running state, still one logical row.
    assert len(H.query(path=path)) == 1
    assert H.query(path=path)[0].status == "running"


def test_claim_unknown_id_raises(tmp_path):
    path = _store(tmp_path)
    with pytest.raises(KeyError):
        H.claim("deadbeef", agent="x", path=path)


def test_update_sets_verdict_results_status(tmp_path):
    path = _store(tmp_path)
    h = H.register(_hyp(), path=path)
    H.claim(h.id, agent="a", path=path)
    out = H.update(h.id, verdict="DEAD", results={"cents": -1.0},
                   status="done", path=path)
    assert out.verdict == "DEAD"
    assert out.status == "done"
    assert out.results["cents"] == -1.0
    # claim metadata preserved (results merged, not replaced).
    assert out.results["claimed_by"] == "a"


def test_update_merges_results(tmp_path):
    path = _store(tmp_path)
    h = H.register(_hyp(), path=path)
    H.update(h.id, results={"a": 1}, path=path)
    out = H.update(h.id, results={"b": 2}, path=path)
    assert out.results == {"a": 1, "b": 2}


def test_update_none_args_leave_fields_unchanged(tmp_path):
    path = _store(tmp_path)
    h = H.register(_hyp(), path=path)
    H.update(h.id, verdict="PROMISING", status="running", path=path)
    out = H.update(h.id, results={"x": 1}, path=path)
    assert out.verdict == "PROMISING"
    assert out.status == "running"


def test_update_unknown_id_raises(tmp_path):
    path = _store(tmp_path)
    with pytest.raises(KeyError):
        H.update("nope", verdict="DEAD", path=path)


# --- query / open_hypotheses filters ---------------------------------------

def test_query_filters_by_status_and_market(tmp_path):
    path = _store(tmp_path)
    a = H.register(_hyp(market=TOTAL, mechanism="a"), path=path)
    H.register(_hyp(market=SPREAD, mechanism="b"), path=path)
    H.claim(a.id, agent="x", path=path)

    assert {h.id for h in H.query(status="running", path=path)} == {a.id}
    assert {h.market for h in H.query(status="open", path=path)} == {SPREAD}
    assert len(H.query(market=TOTAL, path=path)) == 1


def test_open_hypotheses_excludes_claimed(tmp_path):
    path = _store(tmp_path)
    a = H.register(_hyp(mechanism="a"), path=path)
    H.register(_hyp(mechanism="b"), path=path)
    assert len(H.open_hypotheses(path=path)) == 2
    H.claim(a.id, agent="x", path=path)
    open_ids = {h.id for h in H.open_hypotheses(path=path)}
    assert a.id not in open_ids
    assert len(open_ids) == 1


def test_query_empty_store(tmp_path):
    path = _store(tmp_path)
    assert H.query(path=path) == []
    assert H.open_hypotheses(path=path) == []


# --- seed_defaults ----------------------------------------------------------

def test_seed_defaults_populates_empty_store(tmp_path):
    path = _store(tmp_path)
    H.seed_defaults(path=path)
    seeded = H.query(path=path)
    assert len(seeded) >= 3
    # spans multiple families/markets and starts all open.
    assert {h.market for h in seeded} >= {TOTAL, SPREAD, WINNER}
    assert all(h.status == "open" for h in seeded)
    assert all(h.id for h in seeded)


def test_seed_defaults_idempotent(tmp_path):
    path = _store(tmp_path)
    H.seed_defaults(path=path)
    n1 = len(H.query(path=path))
    H.seed_defaults(path=path)
    H.seed_defaults(path=path)
    assert len(H.query(path=path)) == n1


def test_seed_defaults_noop_when_nonempty(tmp_path):
    path = _store(tmp_path)
    H.register(_hyp(mechanism="custom"), path=path)
    H.seed_defaults(path=path)
    assert len(H.query(path=path)) == 1


# --- E2E smoke (synthetic) --------------------------------------------------

def test_e2e_smoke_dedupe_and_open_count(tmp_path):
    path = _store(tmp_path)
    h1 = H.register(_hyp(market=TOTAL, mechanism="pace anchoring"), path=path)
    h2 = H.register(_hyp(market=SPREAD, mechanism="margin reversion"), path=path)
    dup = H.register(_hyp(market=TOTAL, mechanism="pace anchoring"), path=path)

    assert dup.id == h1.id
    assert h1.id != h2.id
    assert len(H.open_hypotheses(path=path)) == 2
