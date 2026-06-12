"""Tests for research.lab.data_agent — the on-demand data-request seam.

The default provider is deterministic and offline; the injectable ``provider``
is the agentic seam (mirrors scout's ``proposer``). These tests pin: the JSONL
log round-trip + dedupe, fulfillment of a derivable spec (values must MATCH a
direct signals call — reuse, not recompute), the honest NEEDS_DATA path for an
external spec (no fabrication), provider injection, and summon_for_hypothesis.
"""
from __future__ import annotations

import numpy as np

from research.lab import data_agent as DA
from research.lab import signals
from research.lab.types import TOTAL, Hypothesis, synthetic_panel


def _store(tmp_path):
    return str(tmp_path / "data_requests.jsonl")


def test_request_query_update_roundtrip_and_dedupe(tmp_path):
    path = _store(tmp_path)
    req = DA.DataRequest(market=TOTAL, field_name="mid_z5", spec="zscore(mid,5)",
                         rationale="need a normalized mid")
    filed = DA.request(req, path=path)
    assert filed.id and filed.status == DA.OPEN and filed.created

    # round-trip through the log
    got = DA.query_requests(path=path)
    assert len(got) == 1 and got[0].field_name == "mid_z5"

    # dedupe: same identity -> no duplicate row, returns existing
    again = DA.request(DA.DataRequest(market=TOTAL, field_name="mid_z5",
                                      spec="zscore(mid,5)"), path=path)
    assert again.id == filed.id
    assert len(DA.query_requests(path=path)) == 1

    # update round-trips and merges result
    DA.update_request(filed.id, status=DA.FULFILLED, result={"k": 1}, path=path)
    upd = DA.query_requests(status=DA.FULFILLED, path=path)
    assert len(upd) == 1 and upd[0].result.get("k") == 1


def test_fulfill_derivable_spec_matches_signals(tmp_path):
    path = _store(tmp_path)
    panel = synthetic_panel(market=TOTAL, n=24, seed=3)
    req = DA.request(DA.DataRequest(market=TOTAL, field_name="mid_z5",
                                    spec="zscore(mid,5)"), path=path)

    new_panel, updated = DA.fulfill(req, panel, path=path)

    assert updated.status == DA.FULFILLED
    arr = new_panel.features["mid_z5"]
    assert arr.shape[0] == panel.n
    # values MUST match the direct signals call (reuse, don't recompute)
    expected = signals.zscore(signals._series(panel, "mid"), 5)
    np.testing.assert_array_equal(np.asarray(arr), np.asarray(expected))
    # original panel untouched (fulfill returns a copy)
    assert "mid_z5" not in panel.features


def test_fulfill_binop_spec(tmp_path):
    path = _store(tmp_path)
    panel = synthetic_panel(market=TOTAL, n=20, seed=1)
    req = DA.request(DA.DataRequest(market=TOTAL, field_name="margin_resid",
                                    spec="rolling(margin,3) - margin"), path=path)
    new_panel, updated = DA.fulfill(req, panel, path=path)
    assert updated.status == DA.FULFILLED
    expected = signals.rolling(panel, "margin", 3) - signals._series(panel, "margin")
    np.testing.assert_array_equal(np.asarray(new_panel.features["margin_resid"]),
                                  np.asarray(expected))


def test_fulfill_external_spec_marks_needs_data_no_fabrication(tmp_path):
    path = _store(tmp_path)
    panel = synthetic_panel(market=TOTAL, n=16, seed=2)
    req = DA.request(DA.DataRequest(market=TOTAL, field_name="injuries",
                                    spec="injury report feed"), path=path)

    out_panel, updated = DA.fulfill(req, panel, path=path)

    assert updated.status == DA.NEEDS_DATA
    # nothing fabricated: panel unchanged, no new field
    assert out_panel is panel
    assert "injuries" not in out_panel.features
    # default_provider itself refuses
    assert DA.default_provider("injury report feed", panel) is None
    # and it persisted to the log
    assert DA.query_requests(status=DA.NEEDS_DATA, path=path)[0].field_name == "injuries"


def test_injected_provider_overrides_default(tmp_path):
    path = _store(tmp_path)
    panel = synthetic_panel(market=TOTAL, n=12, seed=4)
    calls = {"n": 0}

    def custom_provider(spec, p):
        calls["n"] += 1
        # a provider the DEFAULT could never satisfy (external-looking spec),
        # proving the injected seam is what ran
        return np.arange(p.n, dtype=float)

    req = DA.request(DA.DataRequest(market=TOTAL, field_name="custom",
                                    spec="some external feed"), path=path)
    new_panel, updated = DA.fulfill(req, panel, provider=custom_provider, path=path)

    assert calls["n"] == 1                      # injected provider was called
    assert updated.status == DA.FULFILLED        # default would have said NEEDS_DATA
    np.testing.assert_array_equal(np.asarray(new_panel.features["custom"]),
                                  np.arange(panel.n, dtype=float))


def test_summon_for_hypothesis_files_request(tmp_path):
    path = _store(tmp_path)
    h = Hypothesis(market=TOTAL, mechanism="pace mispriced after injuries",
                   signal_desc="injury-adjusted pace", direction="over",
                   verdict=DA.NEEDS_DATA)
    h = h.__class__(**{**h.__dict__, "id": h.hash()})

    req = DA.summon_for_hypothesis(h, path=path)
    assert req.status == DA.OPEN
    assert req.market == TOTAL
    assert h.hash() in req.rationale
    filed = DA.query_requests(path=path)
    assert len(filed) == 1 and filed[0].spec == "injury-adjusted pace"
