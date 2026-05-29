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


class TestPolymarketFlatFee(unittest.TestCase):
    def test_flat_fee_present_and_matches_research_harness(self):
        FeeModel = _load("mock_execution", "mock_execution.py").FeeModel
        from research.harness.cost_profile import PESSIMISTIC, use_profile
        from research.harness.realistic_fills import _polymarket_taker_fee

        price, size, bps = 0.5, 100.0, 1000
        fee = FeeModel.polymarket_taker_fee(price, size, bps)
        # The 2% flat piece alone is 0.02 * (0.5*100) = 1.0, so a curve-only
        # model (the bug) would return ~0.78 < 0.9. The fix must clear this.
        self.assertGreater(fee, 0.9)
        # And it must match the research harness fee model (same 1000bps + 2%).
        with use_profile(PESSIMISTIC):
            ref = _polymarket_taker_fee(price, size, bps)
        self.assertAlmostEqual(fee, ref, places=4)


if __name__ == "__main__":
    unittest.main()
