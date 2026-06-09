"""WS5 tests: durable snapshot sink + Polymarket flat-fee fix.

The arbitrage bot has its own dependency stack (aiohttp, fuzzywuzzy, dotenv, …)
that is intentionally NOT in the research venv, and its package __init__ eagerly
imports the whole async/fuzzy system. This test lives OUTSIDE the package and
loads the two WS5 modules in ISOLATION (bypassing __init__, stubbing aiohttp +
a minimal config) so it exercises the real module code without the full bot env.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

import pandas as pd

_PKG_DIR = Path(__file__).resolve().parents[1] / "kalshi_arbitrage"


def _ensure_stubs():
    if "kalshi_arbitrage" not in sys.modules or not getattr(
        sys.modules["kalshi_arbitrage"], "_ws5_isolated", False
    ):
        pkg = types.ModuleType("kalshi_arbitrage")
        pkg.__path__ = [str(_PKG_DIR)]
        pkg._ws5_isolated = True
        sys.modules["kalshi_arbitrage"] = pkg
    if "kalshi_arbitrage.config" not in sys.modules:
        cfg = types.ModuleType("kalshi_arbitrage.config")

        class _Cfg:
            POLYMARKET_FLAT_TAKER_RATE = 0.02
            POLYMARKET_FEE_RATE_TTL_SECONDS = 60
            POLYMARKET_FEE_RATE_ENDPOINT = "http://x/{token_id}"

        cfg.Config = _Cfg
        sys.modules["kalshi_arbitrage.config"] = cfg
    if "aiohttp" not in sys.modules:
        try:
            import aiohttp  # noqa: F401  (use the real one when available)
        except Exception:  # noqa: BLE001
            stub = types.ModuleType("aiohttp")
            stub.ClientSession = object            # used only in def-time annotations
            stub.ClientTimeout = lambda *a, **k: None
            sys.modules["aiohttp"] = stub


def _load(mod_name: str, filename: str):
    _ensure_stubs()
    full = f"kalshi_arbitrage.{mod_name}"
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSnapshotStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="snapstore_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.SnapshotStore = _load("snapshot_store", "snapshot_store.py").SnapshotStore

    def test_roundtrip_and_append(self):
        s = self.SnapshotStore(self.tmp)
        s.record_book("kalshi", "KX-1", 1000,
                      [(0.5, 10), (0.49, 20)],
                      [(0.51, 5), {"price": 0.52, "size": 8}])
        s.record_trade("polymarket", "tok", 1001, "BUY", 0.5, 3)
        s.flush()
        snaps = pd.read_parquet(s.snapshots_path)
        self.assertEqual(len(snaps), 4)
        self.assertEqual(set(snaps.side), {"bid", "ask"})
        self.assertEqual(len(pd.read_parquet(s.trades_path)), 1)
        # A second flush appends rather than overwrites.
        s.record_book("kalshi", "KX-1", 1002, [(0.5, 10)], [(0.51, 5)])
        s.flush()
        self.assertEqual(len(pd.read_parquet(s.snapshots_path)), 6)


class TestPolymarketOfficialFee(unittest.TestCase):
    """Pins the OFFICIAL Polymarket taker fee: C × rate × p × (1−p).

    Verified against docs.polymarket.com/trading/fees (2026-06-09). The legacy
    model added a flat 2%-of-notional piece that does not exist in the official
    schedule and over-charged extreme-priced legs — exactly where durable
    cross-venue arb lives.
    """

    def test_official_parabolic_values(self):
        FeeModel = _load("mock_execution", "mock_execution.py").FeeModel
        # Peak of the parabola: 100 × 0.10 × 0.5 × 0.5 = 2.5 (no flat piece).
        self.assertAlmostEqual(FeeModel.polymarket_taker_fee(0.5, 100.0, 1000), 2.5, places=5)
        # Default Config rate (500 bps = 0.05, Economics/Other category).
        self.assertAlmostEqual(FeeModel.polymarket_taker_fee(0.5, 100.0, 500), 1.25, places=5)
        # Extreme price: 100 × 0.05 × 0.02 × 0.98 = 0.098 — near-zero, NOT
        # the legacy 0.02×notional flat charge.
        self.assertAlmostEqual(FeeModel.polymarket_taker_fee(0.02, 100.0, 500), 0.098, places=5)
        # Geopolitics is a genuine zero-fee category.
        self.assertEqual(FeeModel.polymarket_taker_fee(0.5, 100.0, 0), 0.0)
        # Minimum charged fee: tiny positive raw rounds up to 0.00001.
        self.assertEqual(FeeModel.polymarket_taker_fee(0.001, 0.01, 500), 0.00001)

    def test_matches_research_harness_under_official_profile(self):
        FeeModel = _load("mock_execution", "mock_execution.py").FeeModel
        from research.harness.cost_profile import OFFICIAL_2026, use_profile
        from research.harness.realistic_fills import _polymarket_taker_fee

        with use_profile(OFFICIAL_2026):
            for price in (0.02, 0.35, 0.5, 0.65, 0.97):
                fee = FeeModel.polymarket_taker_fee(price, 100.0, 500)
                ref = _polymarket_taker_fee(price, 100.0, 500)
                self.assertAlmostEqual(fee, ref, places=5, msg=f"price={price}")


if __name__ == "__main__":
    unittest.main()
