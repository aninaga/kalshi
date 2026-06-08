"""Execution capture for paper/live validation (Phase C).

Persists one record per execution attempt — the pre-trade *estimate* alongside
the *realized* outcome — so a paper run can be analyzed for estimated-vs-realized
drift, hedge frequency, and polarity errors before any real capital is risked.

Stored as JSONL (dependency-free, append-only, trivially analyzable). The schema
is lake-compatible: ``research/paper/analyze_paper_run.py`` reads it directly.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from ..config import Config

logger = logging.getLogger(__name__)


class ExecutionCapture:
    """Append-only sink for execution estimate/realized records."""

    def __init__(self, path: Optional[str] = None):
        if path is None:
            path = os.path.join(Config.DATA_DIR, "executions", "executions.jsonl")
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def record(self, opportunity: Dict[str, Any], result: Any,
               estimate: Optional[Dict[str, Any]] = None,
               hedge: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Write one execution record. Returns the row (also useful for tests)."""
        match = opportunity.get("match_data", {}) or {}
        verification = match.get("verification") if isinstance(match, dict) else None
        estimate = estimate or self.estimate_from_opportunity(opportunity)

        # Scale the estimate to the size ACTUALLY filled. ``total_profit`` is the
        # full-depth estimate, but the executor caps to MAX_POSITION_SIZE_USD per
        # leg — so comparing full-size estimate vs capped realized would book the
        # safety cap as "drift" and mask real fill quality. expected_net (scaled)
        # measures slippage+fees on the traded size; expected_net_full is kept for
        # reference.
        expected_full = estimate.get("expected_net")
        full_vol = float(opportunity.get("max_tradeable_volume") or 0)
        filled_vol = float(getattr(result, "filled_volume", 0) or 0)
        if expected_full is not None and full_vol > 0 and filled_vol > 0:
            expected_scaled = expected_full * (filled_vol / full_vol)
        else:
            expected_scaled = expected_full

        row = {
            "ts": time.time(),
            "opportunity_id": getattr(result, "opportunity_id", opportunity.get("opportunity_id")),
            "execution_id": getattr(result, "execution_id", None),
            "strategy": opportunity.get("strategy"),
            "strategy_type": opportunity.get("strategy_type"),
            "polarity": opportunity.get("polarity"),
            "verification": verification,
            "confirmation_source": getattr(result, "confirmation_source", None),
            "skipped_reason": getattr(result, "skipped_reason", None),
            "buy_platform": getattr(result, "buy_platform", None),
            "sell_platform": getattr(result, "sell_platform", None),
            "requested_volume": getattr(result, "requested_volume", 0),
            "filled_volume": getattr(result, "filled_volume", 0),
            # Estimate (pre-trade expectation). expected_net is scaled to the
            # filled size for a fair fill-quality comparison; expected_net_full is
            # the full-depth estimate.
            "expected_net": expected_scaled,
            "expected_net_full": expected_full,
            "expected_buy_price": estimate.get("buy_price"),
            "expected_sell_price": estimate.get("sell_price"),
            # Realized.
            "realized_net": getattr(result, "net_profit", None),
            "realized_gross": getattr(result, "gross_profit", None),
            "avg_buy_price": getattr(result, "avg_buy_price", None),
            "avg_sell_price": getattr(result, "avg_sell_price", None),
            "buy_fees": getattr(result, "buy_fees", None),
            "sell_fees": getattr(result, "sell_fees", None),
            "latency_ms": getattr(result, "latency_ms", None),
            # Hedge.
            "hedge_residual": (hedge or {}).get("residual", 0.0),
            "hedge_filled": (hedge or {}).get("unwind_filled"),
        }
        # Estimated-vs-realized delta when both are known.
        if row["expected_net"] is not None and row["realized_net"] is not None:
            row["est_vs_real_delta"] = row["realized_net"] - row["expected_net"]

        with self._lock:
            with open(self.path, "a") as fh:
                fh.write(json.dumps(row) + "\n")
        return row

    @staticmethod
    def estimate_from_opportunity(opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """Derive the pre-trade estimate from the analyzer's opportunity dict."""
        return {
            "expected_net": opportunity.get("total_profit"),
            "buy_price": opportunity.get("kalshi_price")
            if opportunity.get("buy_platform") == "kalshi"
            else opportunity.get("polymarket_price"),
            "sell_price": opportunity.get("polymarket_price")
            if opportunity.get("sell_platform") == "polymarket"
            else opportunity.get("kalshi_price"),
        }
