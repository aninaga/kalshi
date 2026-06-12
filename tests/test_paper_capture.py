"""Phase C: execution capture + paper-run analysis."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.execution.capture import ExecutionCapture
from kalshi_arbitrage.mock_execution import ExecutionResult
from kalshi_arbitrage.validation.paper.analyze_paper_run import analyze, load_records


def _result(**kw):
    base = dict(
        opportunity_id="opp-1", buy_platform="kalshi", sell_platform="polymarket",
        requested_volume=100, filled_volume=100, avg_buy_price=0.4, avg_sell_price=0.5,
        buy_fees=0.1, sell_fees=0.2, gross_profit=10.0, net_profit=9.7,
        profit_margin=0.24, latency_ms=120, timestamp=0.0,
        confirmation_source="paper",
    )
    base.update(kw)
    return ExecutionResult(**base)


def _opp(**kw):
    base = dict(opportunity_id="opp-1", buy_platform="kalshi", sell_platform="polymarket",
                kalshi_price=0.4, polymarket_price=0.5, total_profit=10.0,
                strategy="test", strategy_type="same_outcome", polarity="aligned",
                match_data={"kalshi_market": {"id": "T"}})
    base.update(kw)
    return base


def test_capture_writes_estimate_and_realized(tmp_path):
    cap = ExecutionCapture(path=str(tmp_path / "ex.jsonl"))
    row = cap.record(_opp(), _result())
    assert row["expected_net"] == 10.0
    assert row["realized_net"] == 9.7
    assert row["est_vs_real_delta"] == pytest.approx(-0.3)
    # persisted
    lines = (tmp_path / "ex.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["opportunity_id"] == "opp-1"


def test_capture_appends(tmp_path):
    cap = ExecutionCapture(path=str(tmp_path / "ex.jsonl"))
    cap.record(_opp(), _result())
    cap.record(_opp(opportunity_id="opp-2"), _result(opportunity_id="opp-2"))
    assert len(load_records(str(tmp_path / "ex.jsonl"))) == 2


def test_analyze_computes_drift_and_hedge(tmp_path):
    cap = ExecutionCapture(path=str(tmp_path / "ex.jsonl"))
    cap.record(_opp(), _result(net_profit=9.0))                      # delta -1.0
    cap.record(_opp(), _result(net_profit=11.0))                     # delta +1.0
    cap.record(_opp(), _result(skipped_reason="below_min_profit"))   # skipped
    records = load_records(str(tmp_path / "ex.jsonl"))
    report = analyze(records)
    assert report.total == 3
    assert report.filled == 2
    assert report.skipped == 1
    assert report.by_skip_reason == {"below_min_profit": 1}
    assert report.est_vs_real_mean == pytest.approx(0.0, abs=1e-9)
    assert report.est_vs_real_ci[0] <= report.est_vs_real_mean <= report.est_vs_real_ci[1]


def test_analyze_flags_unknown_polarity_as_error(tmp_path):
    cap = ExecutionCapture(path=str(tmp_path / "ex.jsonl"))
    cap.record(_opp(polarity="unknown"), _result())
    report = analyze(load_records(str(tmp_path / "ex.jsonl")))
    assert report.polarity_errors == 1


def test_capture_refuses_production_ledger_under_pytest():
    # Defect C6: a test that constructs ExecutionCapture() with the DEFAULT
    # path (no explicit path, DATA_DIR unpatched) must NOT write the production
    # ledger — reconcile.py replays confirmation_source=="exchange" rows there
    # as locked-in live P&L. Under pytest the default path is diverted.
    import os
    from kalshi_arbitrage.config import Config

    default_path = os.path.abspath(
        os.path.join(Config.DATA_DIR, "executions", "executions.jsonl"))
    cap = ExecutionCapture()  # no path, no DATA_DIR patch — the dangerous case
    assert os.path.abspath(cap.path) != default_path, (
        "ExecutionCapture must divert away from the production ledger under pytest")
    # And it must not have (re)created the production file as a side effect.
    cap.record(_opp(), _result(confirmation_source="exchange"))
    written = json.loads(Path(cap.path).read_text().splitlines()[0])
    assert written["confirmation_source"] == "exchange"
    assert Path(cap.path).resolve() != Path(default_path).resolve()
