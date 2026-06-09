"""PolymarketOrderClient against the CLOB V2 SDK (py-clob-client-v2), mocked.

Polymarket's CLOB V2 cutover (2026-04-28) killed the legacy py-clob-client.
These tests stub the ``py_clob_client_v2`` module and pin the adapter's V2
behavior: limit orders carry per-market tick-size/neg-risk options (cached),
FOK/FAK route through the market-order path, GTD carries a wire-level
expiration, rejected orders surface the gateway's ``error`` contract, and
``get_trades`` (the reconciler's venue-truth source) filters by order id.
No network, no real SDK required.
"""

import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_sdk_stub(record):
    """Install a fake py_clob_client_v2 exposing the V2 surface the adapter uses."""
    sdk = types.ModuleType("py_clob_client_v2")

    class Side:
        BUY = "BUY"
        SELL = "SELL"

    class OrderType:
        FOK = "FOK"
        FAK = "FAK"
        GTC = "GTC"
        GTD = "GTD"

    class OrderArgs:
        def __init__(self, token_id, price, size, side, expiration=None):
            self.token_id, self.price, self.size = token_id, price, size
            self.side, self.expiration = side, expiration

    class MarketOrderArgs:
        def __init__(self, token_id, amount, side):
            self.token_id, self.amount, self.side = token_id, amount, side

    class PartialCreateOrderOptions:
        def __init__(self, tick_size, neg_risk=False):
            self.tick_size, self.neg_risk = tick_size, neg_risk

    class TradeParams:
        def __init__(self, id=None):
            self.id = id

    class BalanceAllowanceParams:
        def __init__(self, asset_type=None):
            self.asset_type = asset_type

    class AssetType:
        COLLATERAL = "COLLATERAL"

    class FakeClobClient:
        def __init__(self, **kwargs):
            record["client_kwargs"] = kwargs

        def create_or_derive_api_key(self):
            return "fake-creds"

        def set_api_creds(self, creds):
            record["creds"] = creds

        def get_tick_size(self, token_id):
            record["tick_calls"] = record.get("tick_calls", 0) + 1
            return "0.001"

        def get_neg_risk(self, token_id):
            record["neg_calls"] = record.get("neg_calls", 0) + 1
            return {"neg_risk": True}

        def create_and_post_order(self, order_args, options=None, order_type=None):
            record["limit"] = (order_args, options, order_type)
            if record.get("reject"):
                return {"success": False, "errorMsg": "not enough balance/allowance"}
            return {"success": True, "orderID": "0xlimit", "status": "live",
                    "transactionsHashes": [], "tradeIDs": []}

        def create_and_post_market_order(self, order_args, options=None, order_type=None):
            record["market"] = (order_args, options, order_type)
            return {"success": True, "orderID": "0xmkt", "status": "matched",
                    "tradeIDs": ["trade-1"]}

        def cancel(self, order_id):
            return {"canceled": [order_id]}

        def cancel_all(self):
            return {"canceled": []}

        def get_order(self, order_id):
            return {"id": order_id, "status": "live"}

        def get_trades(self, params=None):
            record["trade_params"] = params
            return [
                {"taker_order_id": "0xlimit", "price": "0.05", "size": "100"},
                {"taker_order_id": "0xunrelated", "price": "0.5", "size": "1"},
            ]

        def get_balance_allowance(self, params=None):
            record["balance_params"] = params
            return {"balance": "25000000"}  # 25 pUSD in 6-decimal base units

    sdk.Side = Side
    sdk.OrderType = OrderType
    sdk.OrderArgs = OrderArgs
    sdk.MarketOrderArgs = MarketOrderArgs
    sdk.PartialCreateOrderOptions = PartialCreateOrderOptions
    sdk.TradeParams = TradeParams
    sdk.BalanceAllowanceParams = BalanceAllowanceParams
    sdk.AssetType = AssetType
    sdk.ClobClient = FakeClobClient
    sys.modules["py_clob_client_v2"] = sdk
    return sdk


@pytest.fixture()
def client(monkeypatch):
    """A PolymarketOrderClient wired to the stubbed V2 SDK."""
    record = {}
    _install_sdk_stub(record)
    from kalshi_arbitrage import polymarket_executor as pe
    monkeypatch.setattr(pe, "_sdk", None)          # force re-import of the stub
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("POLYMARKET_FUNDER_ADDRESS", raising=False)
    monkeypatch.delenv("POLYMARKET_SIGNATURE_TYPE", raising=False)
    c = pe.PolymarketOrderClient()
    yield c, record
    sys.modules.pop("py_clob_client_v2", None)
    pe._sdk = None


async def test_gtc_limit_order_carries_tick_size_and_neg_risk(client):
    c, record = client
    resp = await c.place_order("tok1", "BUY", 0.05, 100, order_type="GTC")
    assert resp["orderID"] == "0xlimit"
    order_args, options, order_type = record["limit"]
    assert (order_args.token_id, order_args.price, order_args.size) == ("tok1", 0.05, 100)
    assert order_args.side == "BUY"
    assert options.tick_size == "0.001"           # fetched from the venue, not assumed
    assert options.neg_risk is True
    assert order_type == "GTD" or order_type == "GTC"
    assert order_type == "GTC"


async def test_market_metadata_cached_per_token(client):
    c, record = client
    await c.place_order("tok1", "BUY", 0.05, 100, order_type="GTC")
    await c.place_order("tok1", "SELL", 0.06, 50, order_type="GTC")
    assert record["tick_calls"] == 1              # second order hits the cache
    assert record["neg_calls"] == 1


async def test_fok_and_fak_route_through_market_order_path(client):
    c, record = client
    resp = await c.place_order("tok1", "BUY", 0.05, 25, order_type="FOK")
    assert resp["orderID"] == "0xmkt"
    args, _options, order_type = record["market"]
    assert args.amount == 25 and args.side == "BUY"
    assert order_type == "FOK"

    await c.place_order("tok1", "SELL", 0.05, 10, order_type="FAK")
    assert record["market"][2] == "FAK"


async def test_gtd_sets_wire_level_expiration(client):
    c, record = client
    before = int(time.time())
    await c.place_order("tok1", "BUY", 0.05, 100, order_type="GTD", ttl_seconds=90)
    order_args = record["limit"][0]
    assert order_args.expiration is not None
    assert before + 85 <= order_args.expiration <= before + 95


async def test_rejected_order_surfaces_error_contract(client):
    # V2 reports success=False + errorMsg; the gateway keys off 'error'.
    c, record = client
    record["reject"] = True
    resp = await c.place_order("tok1", "BUY", 0.05, 100, order_type="GTC")
    assert "error" in resp and "balance" in resp["error"]


async def test_sdk_exception_returns_error_dict(client):
    c, record = client
    await c.place_order("tok1", "BUY", 0.05, 1, order_type="GTC")  # init client
    def boom(*a, **k):
        raise RuntimeError("clob down")
    c._client.create_and_post_order = boom
    resp = await c.place_order("tok1", "BUY", 0.05, 1, order_type="GTC")
    assert resp == {"error": "clob down"}


async def test_get_trades_filters_to_order_id(client):
    # The reconciler's venue-truth source: only the requested order's fills.
    c, record = client
    trades = await c.get_trades(order_id="0xlimit")
    assert len(trades) == 1 and trades[0]["taker_order_id"] == "0xlimit"
    assert record["trade_params"].id == "0xlimit"


async def test_balance_converts_pusd_base_units(client):
    c, record = client
    bal = await c.get_balance_usdc()
    assert bal == 25.0
    assert record["balance_params"].asset_type == "COLLATERAL"


async def test_cancel_roundtrip(client):
    c, _ = client
    assert await c.cancel_order("0xlimit") is True


def test_missing_sdk_error_names_v2_package(monkeypatch):
    from kalshi_arbitrage import polymarket_executor as pe
    monkeypatch.setattr(pe, "_sdk", None)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", None)  # forces ImportError
    with pytest.raises(ImportError, match="py-clob-client-v2"):
        pe._ensure_sdk()
