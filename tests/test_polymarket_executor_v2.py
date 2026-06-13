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
    # CLIENT-level contract: for FOK/FAK BUY the size param is ALREADY pUSD
    # dollars (the gateway converts shares→dollars before calling here); the
    # client must pass it through verbatim, never re-convert.
    c, record = client
    resp = await c.place_order("tok1", "BUY", 0.05, 25, order_type="FOK")
    assert resp["orderID"] == "0xmkt"
    args, _options, order_type = record["market"]
    assert args.amount == 25 and args.side == "BUY"
    assert order_type == "FOK"

    await c.place_order("tok1", "SELL", 0.05, 10, order_type="FAK")
    assert record["market"][2] == "FAK"


# --- Gateway-level shares→dollars conversion (C1) --------------------------- #

class _FakeFeeClient:
    async def get_fee_rate_bps(self, token_id):
        return 0

    async def close(self):
        pass


def _gateway(c, monkeypatch):
    from kalshi_arbitrage.execution.live_lock import LiveTradingLock
    from kalshi_arbitrage.execution.venue_gateway import PolymarketGateway

    # Bypass ONLY the live lock for this unit test — the client is the SDK stub.
    monkeypatch.setattr(LiveTradingLock.instance(), "assert_or_warn",
                        lambda venue: (True, None))
    return PolymarketGateway(client=c, fee_client=_FakeFeeClient())


async def test_gateway_fok_buy_sends_dollars_not_shares(client, monkeypatch):
    """C1: a marketable BUY of N shares must reach the SDK as N*price pUSD.

    Before the fix, a $5-clamped 12.5-share buy at 0.40 sent amount=12.5
    (i.e. $12.50 of shares — 2.5x the clamp). req.size must stay shares.
    """
    from kalshi_arbitrage.execution.order_types import POLYMARKET, OrderRequest

    c, record = client
    gw = _gateway(c, monkeypatch)
    req = OrderRequest(venue=POLYMARKET, action="buy", size=12.5,
                       limit_price=0.40, token_id="tok1", tif="FOK")
    out = await gw.place(req)
    args, _options, order_type = record["market"]
    assert args.amount == pytest.approx(5.0)   # 12.5 shares * $0.40 = $5 DOLLARS
    assert order_type == "FOK"
    assert req.size == 12.5                    # invariant: .size is shares, untouched
    assert out.requested_size == 12.5
    # FOK bare-success fallback derives SHARES from the notional actually sent.
    assert out.filled_size == pytest.approx(12.5)
    assert out.avg_price == pytest.approx(0.40)


async def test_gateway_fok_sell_stays_shares(client, monkeypatch):
    from kalshi_arbitrage.execution.order_types import POLYMARKET, OrderRequest

    c, record = client
    gw = _gateway(c, monkeypatch)
    req = OrderRequest(venue=POLYMARKET, action="sell", size=10,
                       limit_price=0.40, token_id="tok1", tif="FOK")
    await gw.place(req)
    args, _options, _order_type = record["market"]
    assert args.amount == 10                   # marketable SELL is shares
    assert args.side == "SELL"


async def test_gateway_limit_order_stays_shares(client, monkeypatch):
    from kalshi_arbitrage.execution.order_types import POLYMARKET, OrderRequest

    c, record = client
    gw = _gateway(c, monkeypatch)
    req = OrderRequest(venue=POLYMARKET, action="buy", size=100,
                       limit_price=0.05, token_id="tok1", tif="GTC")
    await gw.place(req)
    order_args, _options, _order_type = record["limit"]
    assert order_args.size == 100              # limit orders are shares


async def test_gateway_marketable_buy_without_price_fails_closed(client, monkeypatch):
    # No limit price → cannot convert shares to dollars → must REFUSE, never
    # fall back to sending shares as dollars.
    from kalshi_arbitrage.execution.order_types import POLYMARKET, OrderRequest

    c, record = client
    gw = _gateway(c, monkeypatch)
    req = OrderRequest(venue=POLYMARKET, action="buy", size=10,
                       limit_price=0.0, token_id="tok1", tif="FOK")
    out = await gw.place(req)
    assert out.status == "failed"
    assert out.error == "marketable_buy_requires_limit_price"
    assert "market" not in record              # nothing reached the SDK


# --- Gateway-level ambiguous-failure handling (no PM server dedup) ----------- #

class _FakePMClient:
    """Minimal PolymarketOrderClient stand-in for ambiguity tests."""

    def __init__(self, place_resp, trades=None):
        self.place_resp = place_resp
        self.trades = trades or []
        self.place_calls = 0

    async def place_order(self, **kwargs):
        self.place_calls += 1
        return self.place_resp

    async def get_trades(self, order_id=None):
        return self.trades

    async def get_order(self, order_id):
        return {}

    async def cancel_order(self, order_id):
        return True

    async def close(self):
        pass


async def test_ambiguous_timeout_is_not_retryable(monkeypatch):
    """A timeout after POST may have placed the order; PM has no server dedup,
    so the failure must come back NON-retryable (fail closed, no blind retry)."""
    from kalshi_arbitrage.execution.live_lock import LiveTradingLock
    from kalshi_arbitrage.execution.order_types import POLYMARKET, OrderRequest
    from kalshi_arbitrage.execution.venue_gateway import PolymarketGateway

    monkeypatch.setattr(LiveTradingLock.instance(), "assert_or_warn",
                        lambda venue: (True, None))
    c = _FakePMClient({"error": "HTTPSConnectionPool: Read timed out"})
    gw = PolymarketGateway(client=c, fee_client=_FakeFeeClient())
    req = OrderRequest(venue=POLYMARKET, action="buy", size=10,
                       limit_price=0.40, token_id="tokX", tif="FOK")
    out = await gw.place(req)
    assert out.status == "failed"
    assert out.retryable is False
    assert out.error.startswith("ambiguous_post:")


async def test_ambiguous_timeout_reconciles_real_fill(monkeypatch):
    """If the timed-out POST actually filled, reconciliation must surface the
    real fill (in SHARES) so it is recorded and hedged — not dropped."""
    import time as _time

    from kalshi_arbitrage.execution.live_lock import LiveTradingLock
    from kalshi_arbitrage.execution.order_types import POLYMARKET, OrderRequest
    from kalshi_arbitrage.execution.venue_gateway import PolymarketGateway

    monkeypatch.setattr(LiveTradingLock.instance(), "assert_or_warn",
                        lambda venue: (True, None))
    trades = [
        {"asset_id": "tokX", "side": "BUY", "size": "10", "price": "0.41",
         "match_time": _time.time(), "taker_order_id": "0xreal", "id": "t-1"},
        {"asset_id": "tokOTHER", "side": "BUY", "size": "99", "price": "0.5",
         "match_time": _time.time(), "taker_order_id": "0xother", "id": "t-2"},
    ]
    c = _FakePMClient({"error": "Read timed out"}, trades=trades)
    gw = PolymarketGateway(client=c, fee_client=_FakeFeeClient())
    req = OrderRequest(venue=POLYMARKET, action="buy", size=10,
                       limit_price=0.40, token_id="tokX", tif="FOK")
    out = await gw.place(req)
    assert out.status == "filled"
    assert out.filled_size == pytest.approx(10)     # shares, from venue trades
    assert out.avg_price == pytest.approx(0.41)
    assert out.order_id == "0xreal"
    assert out.error.startswith("ambiguous_post_reconciled:")


async def test_engine_never_retries_ambiguous_pm_failure(monkeypatch):
    """End-to-end: the engine must NOT re-POST an ambiguous PM failure."""
    from kalshi_arbitrage.config import Config
    from kalshi_arbitrage.execution.kill_switch import KillSwitch
    from kalshi_arbitrage.execution.live_lock import LiveTradingLock
    from kalshi_arbitrage.execution.order_types import POLYMARKET, OrderRequest
    from kalshi_arbitrage.execution.single_leg import ExecutionEngine
    from kalshi_arbitrage.execution.venue_gateway import PolymarketGateway

    monkeypatch.setattr(Config, "EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_MAX_RETRIES", 3, raising=False)
    monkeypatch.setattr(Config, "EXECUTION_RETRY_BASE_DELAY", 0.0, raising=False)
    KillSwitch.instance().reset()
    monkeypatch.setattr(LiveTradingLock.instance(), "assert_or_warn",
                        lambda venue: (True, None))
    c = _FakePMClient({"error": "Read timed out"})
    gw = PolymarketGateway(client=c, fee_client=_FakeFeeClient())
    engine = ExecutionEngine({POLYMARKET: gw})
    req = OrderRequest(venue=POLYMARKET, action="buy", size=10,
                       limit_price=0.40, token_id="tokX", tif="FOK")
    out = await engine.execute(req, stable_key="opp:buy")
    assert out.status == "failed"
    assert c.place_calls == 1, "ambiguous PM failure must never be re-POSTed"


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
