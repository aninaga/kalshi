"""ExecutionCapture: estimate scaled to filled size for fair est-vs-real drift.

Caught running monitor --execute: a $14.85 full-depth opportunity that the $100
position cap trimmed to a partial fill realized $8.56, and est_vs_real booked a
$6 "drift" that was purely the safety cap — masking true fill quality. The
estimate must scale to the size actually filled.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.execution.capture import ExecutionCapture


class _Result:
    def __init__(self, filled, net):
        self.opportunity_id = "opp"
        self.execution_id = "paper-x"
        self.confirmation_source = "paper"
        self.skipped_reason = None
        self.buy_platform = "kalshi"
        self.sell_platform = "polymarket"
        self.requested_volume = 1021
        self.filled_volume = filled
        self.net_profit = net
        self.gross_profit = net
        self.avg_buy_price = 0.5
        self.avg_sell_price = 0.47
        self.buy_fees = 0.0
        self.sell_fees = 0.0
        self.latency_ms = 10


def test_estimate_scales_to_filled_size(tmp_path):
    cap = ExecutionCapture(path=str(tmp_path / "ex.jsonl"))
    opp = {"strategy_type": "complementary", "total_profit": 14.85,
           "max_tradeable_volume": 1021, "kalshi_price": 0.5, "polymarket_price": 0.47,
           "buy_platform": "kalshi", "sell_platform": "polymarket"}
    # Cap trims 1021 -> ~588 filled; a clean fill realizes the proportional net.
    row = cap.record(opp, _Result(filled=588, net=8.55))
    assert abs(row["expected_net_full"] - 14.85) < 1e-9        # full-depth estimate kept
    assert abs(row["expected_net"] - 14.85 * 588 / 1021) < 1e-6  # scaled to fill
    # est_vs_real now measures fill quality (~0), not the safety cap.
    assert abs(row["est_vs_real_delta"]) < 0.05


def test_full_fill_estimate_unchanged(tmp_path):
    cap = ExecutionCapture(path=str(tmp_path / "ex.jsonl"))
    opp = {"strategy_type": "complementary", "total_profit": 7.63,
           "max_tradeable_volume": 91, "kalshi_price": 0.83, "polymarket_price": 0.0745,
           "buy_platform": "kalshi", "sell_platform": "polymarket"}
    row = cap.record(opp, _Result(filled=91, net=7.627))
    assert abs(row["expected_net"] - 7.63) < 1e-9  # full fill → unscaled
    assert abs(row["est_vs_real_delta"]) < 0.01
