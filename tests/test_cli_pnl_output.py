"""CLI-output regression tests for PnL summary channels."""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbitrage_analyzer import ArbitrageAnalysisSystem


def test_cli_summary_prints_confirmed_realized_channel(capsys):
    system = ArbitrageAnalysisSystem()

    scan_report = {
        "estimated_pnl_per_scan_usd": 12.0,
        "estimated_pnl_per_hour_usd": 1440.0,
        "estimated_pnl_per_day_usd": 34560.0,
        "estimated_pnl_opportunity_count": 3,
        "guaranteed_pnl_per_scan_usd": 5.0,
        "guaranteed_pnl_per_hour_usd": 600.0,
        "guaranteed_pnl_per_day_usd": 14400.0,
        "guaranteed_pnl_opportunity_count": 1,
        "confirmed_realized_pnl_per_scan_usd": 2.0,
        "confirmed_realized_pnl_per_hour_usd": 240.0,
        "confirmed_realized_pnl_per_day_usd": 5760.0,
        "confirmed_realized_pnl_opportunity_count": 1,
        "confirmed_settled_execution_count": 1,
        "confirmed_pending_execution_count": 0,
        "confirmed_counting_simulated_confirmations": False,
        "synthetic_orderbook_opportunities": 0,
        "real_orderbook_opportunities": 1,
    }

    system._print_guaranteed_pnl_summary(scan_report)
    output = capsys.readouterr().out

    assert "Estimated PnL" in output
    assert "Guaranteed PnL (simulated fills)" in output
    assert "Confirmed Realized PnL (settled fills)" in output
    assert "confirmed_realized=1" in output
    assert "include_simulated=False" in output
