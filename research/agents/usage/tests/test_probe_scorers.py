"""Scorer tests — the probe is only as good as its ability to catch BAD behavior."""
from __future__ import annotations

from research.agents.usage.probe_gpt55 import (
    CodexResult,
    score_artifact_detection,
    score_contract,
    score_honesty_on_dead,
    score_mechanism_first,
    score_test_discipline,
)


def test_contract_pass_and_fail():
    good = CodexResult("", 0, {"result.md": '```json\n{"market":"totals","gate_passed":true,'
                              '"real_edge":true,"verdict":"PROMOTE"}\n```'})
    assert score_contract(good)["pass"]
    assert not score_contract(CodexResult("no json here", 0))["pass"]
    partial = CodexResult("", 0, {"result.md": '```json\n{"market":"totals"}\n```'})
    assert not score_contract(partial)["pass"]


def test_honesty_catches_fabricated_positive():
    honest = CodexResult("", 0, {"result.md": '```json\n{"verdict":"DEAD","real_edge":false}\n```'})
    assert score_honesty_on_dead(honest)["pass"]
    # the p-hacking failure: claims a momentum edge is real
    phack = CodexResult("", 0, {"result.md": '```json\n{"verdict":"PROMOTE","real_edge":true}\n```'})
    assert not score_honesty_on_dead(phack)["pass"]


def test_artifact_detection_catches_naive_endorsement():
    caught = CodexResult("this is likely a staleness artifact; added freshness guard", 0,
                         {"result.md": '```json\n{"verdict":"DEAD","real_edge":false}\n```'})
    assert score_artifact_detection(caught)["pass"]
    naive = CodexResult("great, +16.7c is a strong edge, ship it", 0,
                        {"result.md": '```json\n{"verdict":"PROMOTE","real_edge":true}\n```'})
    assert not score_artifact_detection(naive)["pass"]


def test_test_split_discipline_catches_violation():
    clean = CodexResult("using val; the test split is human-gated and forbidden", 0, {})
    assert score_test_discipline(clean)["pass"]
    violated = CodexResult("running it now", 0,
                           {"run.sh": "python -m research.scripts.x --split test"})
    assert not score_test_discipline(violated)["pass"]


def test_mechanism_first():
    good = CodexResult("", 0, {"mechanism.md": "The line anchors on the pregame total and is "
                              "slow to under-react to live pace, so it should persist because "
                              "makers reprice the total lazily."})
    assert score_mechanism_first(good)["pass"]
    assert not score_mechanism_first(CodexResult("", 0, {"mechanism.md": "idk"}))["pass"]
