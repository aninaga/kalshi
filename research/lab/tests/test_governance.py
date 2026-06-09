"""Tests for research.lab.governance — the N-aware DSR governance layer.

All tests write tiny temp JSONL files via ``tmp_path`` — never the default
store. They assert the three public functions (``trial_count``,
``sharpe_variance``, ``governance_params``) behave as documented, and an
end-to-end check that a higher N tightens the DSR hurdle
(``research.scorer.dsr.expected_max_sharpe``).
"""
from __future__ import annotations

import json

from research.lab import governance as G
from research.lab import hypothesis as H
from research.lab.types import TOTAL, Hypothesis
from research.scorer.dsr import expected_max_sharpe


# --- helpers ---------------------------------------------------------------


def _write_ledger(tmp_path, rows) -> str:
    path = str(tmp_path / "ledger.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def _row(hid, *, net=4.0, ci_lo=0.5, ci_hi=7.5, verdict="PROMISING", **extra):
    results = {"net_cents_0c": net, "ci_lo": ci_lo, "ci_hi": ci_hi}
    results.update(extra)
    return {"hypothesis_id": hid, "verdict": verdict, "results": results}


def _empty_registry(tmp_path) -> str:
    """A registry path that exists but has no verdicted hypotheses."""
    return str(tmp_path / "hypotheses.jsonl")


# --- trial_count -----------------------------------------------------------


def test_trial_count_counts_ledger_rows(tmp_path):
    led = _write_ledger(tmp_path, [_row("a"), _row("b"), _row("c")])
    assert G.trial_count(led, registry_path=_empty_registry(tmp_path)) == 3


def test_trial_count_dedupes_reruns_by_id(tmp_path):
    # Same hypothesis re-run three times -> one dart.
    led = _write_ledger(tmp_path, [_row("a"), _row("a"), _row("a"), _row("b")])
    assert G.trial_count(led, registry_path=_empty_registry(tmp_path)) == 2


def test_trial_count_includes_verdicted_registry_hypotheses(tmp_path):
    led = _write_ledger(tmp_path, [_row("a")])
    reg = str(tmp_path / "hyp.jsonl")
    # Two registry ideas: one verdicted (counts), one still open (does not).
    judged = H.register(Hypothesis(market=TOTAL, mechanism="m1", signal_desc="s",
                                   direction="d"), path=reg)
    H.update(judged.id, verdict="DEAD", path=reg)
    H.register(Hypothesis(market=TOTAL, mechanism="m2", signal_desc="s",
                          direction="d"), path=reg)  # stays open
    # ledger "a" + judged registry hypothesis = 2 (open one excluded).
    assert G.trial_count(led, registry_path=reg) == 2


def test_trial_count_dedupes_registry_against_ledger(tmp_path):
    reg = str(tmp_path / "hyp.jsonl")
    judged = H.register(Hypothesis(market=TOTAL, mechanism="m1", signal_desc="s",
                                   direction="d"), path=reg)
    H.update(judged.id, verdict="PROMOTE", path=reg)
    # The SAME id also appears in the ledger -> still one trial.
    led = _write_ledger(tmp_path, [_row(judged.id)])
    assert G.trial_count(led, registry_path=reg) == 1


def test_trial_count_anonymous_rows_count(tmp_path):
    # Rows without an id cannot be proven duplicates -> each is its own trial.
    led = _write_ledger(tmp_path, [
        {"verdict": "PROMISING", "results": {"net_cents_0c": 1.0}},
        {"verdict": "PROMISING", "results": {"net_cents_0c": 2.0}},
    ])
    assert G.trial_count(led, registry_path=_empty_registry(tmp_path)) == 2


def test_trial_count_min_one_on_empty(tmp_path):
    missing = str(tmp_path / "nope.jsonl")
    assert G.trial_count(missing, registry_path=_empty_registry(tmp_path)) == 1


def test_trial_count_skips_malformed_lines(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_row("a")) + "\n")
        fh.write("{not json at all\n")
        fh.write("\n")
        fh.write(json.dumps(_row("b")) + "\n")
    assert G.trial_count(path, registry_path=_empty_registry(tmp_path)) == 2


# --- sharpe_variance -------------------------------------------------------


def test_sharpe_variance_placeholder_when_too_few_trials(tmp_path):
    led = _write_ledger(tmp_path, [_row("a")])
    assert G.sharpe_variance(led, registry_path=_empty_registry(tmp_path)) == 1.0


def test_sharpe_variance_empty_is_placeholder(tmp_path):
    missing = str(tmp_path / "nope.jsonl")
    assert G.sharpe_variance(missing, registry_path=_empty_registry(tmp_path)) == 1.0


def test_sharpe_variance_real_estimate_nonnegative(tmp_path):
    # Distinct trials with differing strength -> a real, finite, >=0 variance.
    led = _write_ledger(tmp_path, [
        _row("a", net=1.0, ci_lo=0.2, ci_hi=1.8),
        _row("b", net=5.0, ci_lo=1.0, ci_hi=9.0),
        _row("c", net=3.0, ci_lo=-1.0, ci_hi=7.0),
    ])
    v = G.sharpe_variance(led, registry_path=_empty_registry(tmp_path))
    assert v >= 0.0
    import math
    assert math.isfinite(v)


def test_sharpe_variance_uses_explicit_sharpe_when_present(tmp_path):
    led = _write_ledger(tmp_path, [
        _row("a", sharpe=0.0),
        _row("b", sharpe=2.0),
    ])
    v = G.sharpe_variance(led, registry_path=_empty_registry(tmp_path))
    # Variance of {0.0, 2.0} with ddof=1 == 2.0.
    assert abs(v - 2.0) < 1e-9


def test_sharpe_variance_nan_safe(tmp_path):
    # One row has no usable numbers (no mean, no CI) -> dropped, leaving <2.
    led = _write_ledger(tmp_path, [
        {"hypothesis_id": "a", "results": {"foo": "bar"}},
        _row("b", net=3.0, ci_lo=1.0, ci_hi=5.0),
    ])
    v = G.sharpe_variance(led, registry_path=_empty_registry(tmp_path))
    # Only one usable trial -> placeholder.
    assert v == 1.0


def test_sharpe_variance_dedupes_reruns(tmp_path):
    # 'a' re-run twice should not double-weight the variance; last write wins.
    led = _write_ledger(tmp_path, [
        _row("a", sharpe=0.0),
        _row("a", sharpe=10.0),   # rerun overrides
        _row("b", sharpe=10.0),
    ])
    v = G.sharpe_variance(led, registry_path=_empty_registry(tmp_path))
    # Effective values {a:10.0, b:10.0} -> variance 0.0.
    assert abs(v - 0.0) < 1e-9


# --- governance_params -----------------------------------------------------


def test_governance_params_cold_start_keys_and_sources(tmp_path):
    missing = str(tmp_path / "nope.jsonl")
    p = G.governance_params(missing, registry_path=_empty_registry(tmp_path))
    assert set(p) == {
        "n_trials", "sharpe_variance",
        "n_trials_source", "sharpe_variance_source",
    }
    assert p["n_trials"] == 1
    assert p["sharpe_variance"] == 1.0
    assert p["n_trials_source"] == "cold-start placeholder"
    assert p["sharpe_variance_source"] == "cold-start placeholder"


def test_governance_params_populated_sources_real(tmp_path):
    led = _write_ledger(tmp_path, [
        _row("a", net=1.0, ci_lo=0.2, ci_hi=1.8),
        _row("b", net=5.0, ci_lo=1.0, ci_hi=9.0),
        _row("c", net=3.0, ci_lo=-1.0, ci_hi=7.0),
    ])
    p = G.governance_params(led, registry_path=_empty_registry(tmp_path))
    assert p["n_trials"] == 3
    assert p["n_trials_source"] == "real-ledger"
    assert p["sharpe_variance_source"] == "real-ledger"
    assert p["sharpe_variance"] >= 0.0


def test_governance_params_single_trial_is_placeholder_n(tmp_path):
    led = _write_ledger(tmp_path, [_row("a")])
    p = G.governance_params(led, registry_path=_empty_registry(tmp_path))
    assert p["n_trials"] == 1
    assert p["n_trials_source"] == "cold-start placeholder"
    # Only one usable Sharpe -> variance placeholder too.
    assert p["sharpe_variance_source"] == "cold-start placeholder"


# --- end-to-end: more darts => higher hurdle -------------------------------


def test_more_trials_raise_dsr_hurdle(tmp_path):
    """Governance must actually tighten the gate as more darts are thrown.

    Build a small ledger and a larger one (same Sharpe dispersion), feed each
    one's governance params into ``expected_max_sharpe``, and assert the larger
    N yields a strictly higher best-of-N hurdle.
    """
    small = _write_ledger(tmp_path, [
        _row(f"s{i}", net=2.0 + (i % 3), ci_lo=(2.0 + (i % 3)) - 2.0,
             ci_hi=(2.0 + (i % 3)) + 2.0)
        for i in range(3)
    ])
    p_small = G.governance_params(small, registry_path=_empty_registry(tmp_path))

    big = _write_ledger(tmp_path, [
        _row(f"b{i}", net=2.0 + (i % 3), ci_lo=(2.0 + (i % 3)) - 2.0,
             ci_hi=(2.0 + (i % 3)) + 2.0)
        for i in range(30)
    ])
    p_big = G.governance_params(big, registry_path=_empty_registry(tmp_path))

    assert p_big["n_trials"] > p_small["n_trials"]

    # Hold V[SR] fixed across both so the comparison isolates N's effect.
    v = 1.0
    hurdle_small = expected_max_sharpe(p_small["n_trials"], v)
    hurdle_big = expected_max_sharpe(p_big["n_trials"], v)
    assert hurdle_big > hurdle_small > 0.0
