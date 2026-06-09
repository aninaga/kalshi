"""Tests for the feature store — the durable half of the data-request loop.

The contract under test: a written field reaches panels ONLY as-of its
``known_from_ts`` (future rows are structurally unreachable), writes demand
provenance, and a broken store never breaks panel loading.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.lab.providers import feature_store as FS
from research.lab.types import TOTAL, synthetic_panel


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the store root to a temp dir (never the real market_data/)."""
    monkeypatch.setattr(FS, "_ROOT", tmp_path)
    FS._CACHE.clear()
    return tmp_path


_DEFAULT_PROV = object()


def _write(family="fam", field="fc", rows=None, prov=_DEFAULT_PROV):
    df = pd.DataFrame(rows or [], columns=["event_id", "known_from_ts", "value"])
    if prov is _DEFAULT_PROV:
        prov = {"source": "unit-test"}
    return FS.write_field(family, field, df, provenance=prov)


def test_write_requires_schema_and_provenance(store):
    with pytest.raises(ValueError, match="missing columns"):
        FS.write_field("fam", "x", pd.DataFrame({"event_id": ["a"]}),
                       provenance={"source": "s"})
    with pytest.raises(ValueError, match="provenance"):
        _write(prov={})


def test_asof_join_never_sees_the_future(store):
    p = synthetic_panel(market=TOTAL, n=10)
    t0 = float(p.minute_ts[0])
    # Three values becoming knowable at bars 0, 4, and AFTER the panel ends.
    _write(rows=[(p.game_id, int(t0), 1.0),
                 (p.game_id, int(p.minute_ts[4]), 2.0),
                 (p.game_id, int(p.minute_ts[-1]) + 10_000, 99.0)])
    FS.join_panel(p, "fam")
    arr = p.features["fc"]
    assert arr[0] == 1.0 and arr[3] == 1.0          # before bar 4: first value
    assert arr[4] == 2.0 and arr[-1] == 2.0          # from bar 4: second value
    assert not np.isin(99.0, arr)                    # the future row never leaks


def test_nan_before_first_knowable(store):
    p = synthetic_panel(market=TOTAL, n=8)
    _write(rows=[(p.game_id, int(p.minute_ts[5]), 7.0)])
    FS.join_panel(p, "fam")
    arr = p.features["fc"]
    assert np.isnan(arr[:5]).all() and (arr[5:] == 7.0).all()


def test_event_without_rows_gets_nan_column(store):
    p = synthetic_panel(market=TOTAL, n=6, game_id="other_event")
    _write(rows=[("some_event", 0, 1.0)])
    FS.join_panel(p, "fam")
    assert np.isnan(p.features["fc"]).all()          # stable name, no data


def test_append_mode_dedupes_keep_last(store):
    p = synthetic_panel(market=TOTAL, n=6)
    t0 = int(p.minute_ts[0])
    _write(rows=[(p.game_id, t0, 1.0)])
    df = pd.DataFrame([(p.game_id, t0, 5.0)], columns=["event_id", "known_from_ts", "value"])
    FS.write_field("fam", "fc", df, provenance={"source": "unit-test"}, mode="append")
    FS.join_panel(p, "fam")
    assert (p.features["fc"] == 5.0).all()           # corrected value wins


def test_mtime_cache_picks_up_fresh_writes(store):
    p = synthetic_panel(market=TOTAL, n=6)
    t0 = int(p.minute_ts[0])
    _write(rows=[(p.game_id, t0, 1.0)])
    FS.join_panel(p, "fam")
    _write(rows=[(p.game_id, t0, 2.0)])              # data agent re-writes
    p2 = synthetic_panel(market=TOTAL, n=6)
    FS.join_panel(p2, "fam")
    assert (p2.features["fc"] == 2.0).all()


def test_broken_field_file_never_breaks_loading(store):
    p = synthetic_panel(market=TOTAL, n=6)
    d = FS.features_dir("fam")
    d.mkdir(parents=True)
    (d / "bad.parquet").write_text("not parquet")
    FS.join_panel(p, "fam")                           # must not raise
    assert "bad" not in p.features


def test_provenance_written_alongside(store):
    import json
    _write(rows=[("e", 0, 1.0)], prov={"source": "open-meteo",
                                       "point_in_time_rule": "issued<=known_from_ts"})
    prov = json.loads((FS.features_dir("fam") / "fc.provenance.json").read_text())
    assert prov["source"] == "open-meteo"
    assert prov["rows"] == 1 and "written_at" in prov
