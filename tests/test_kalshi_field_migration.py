"""Kalshi REST field-name migration: parse the NEW _fp/_dollars schema.

Kalshi deprecated the flat field names (volume, yes_bid, last_price, and the
orderbook yes/no arrays) to return null, moving the data to fixed-point/dollars
fields (volume_fp, yes_bid_dollars, orderbook_fp.yes_dollars/no_dollars). The bot
read the old names and saw every market as empty. These tests pin the new parse.
"""

from kalshi_arbitrage.api_clients import KalshiClient
from kalshi_arbitrage.market_analyzer import MarketAnalyzer


# --- Orderbook parsing (the critical arb path) ----------------------------- #

def test_parse_new_orderbook_fp_structure():
    kc = KalshiClient()
    payload = {"orderbook": {"orderbook_fp": {
        "yes_dollars": [["0.55", "100"], ["0.50", "8"]],   # YES bids (dollars)
        "no_dollars":  [["0.40", "50"]],                    # NO bids (dollars)
    }}}
    book = kc._parse_orderbook_payload(payload)
    # YES bids parsed in dollars, no /100.
    assert {"price": 0.55, "size": 100.0} in book["yes_bids"]
    assert {"price": 0.50, "size": 8.0} in book["yes_bids"]
    # YES ask derived from NO bid: 1 - 0.40 = 0.60.
    assert book["yes_asks"] == [{"price": 0.60, "size": 50.0}]
    # NO ask derived from YES bid: 1 - 0.55 = 0.45 (and 1 - 0.50 = 0.50).
    assert {"price": 0.45, "size": 100.0} in book["no_asks"]


def test_parse_legacy_cents_orderbook_still_works():
    kc = KalshiClient()
    # Pre-migration flat arrays in cents.
    payload = {"orderbook": {"yes": [[55, 100]], "no": [[40, 50]]}}
    book = kc._parse_orderbook_payload(payload)
    assert book["yes_bids"] == [{"price": 0.55, "size": 100.0}]   # cents -> dollars
    assert book["yes_asks"] == [{"price": 0.60, "size": 50.0}]


def test_parse_empty_orderbook_does_not_crash():
    kc = KalshiClient()
    book = kc._parse_orderbook_payload({"orderbook": {"orderbook_fp": {}}})
    assert book["yes_bids"] == [] and book["yes_asks"] == []


def test_normalize_levels_already_dollars_skips_conversion():
    kc = KalshiClient()
    out = kc._normalize_kalshi_levels([["0.55", "12"], ["0.50", "8"]], already_dollars=True)
    assert out == [{"price": 0.55, "size": 12.0}, {"price": 0.50, "size": 8.0}]


# --- Market metadata / price field reads ----------------------------------- #

def test_fp_reads_new_field_names():
    m = {"volume_fp": "538.00", "open_interest_fp": "12.00",
         "yes_bid_dollars": "0.50", "yes_ask_dollars": "0.55"}
    assert KalshiClient._fp(m, "volume") == 538.0
    assert KalshiClient._fp(m, "yes_bid") == 0.50          # NOT 0.005 or 50
    assert KalshiClient._fp(m, "open_interest") == 12.0


def test_fp_falls_back_to_legacy_names():
    assert KalshiClient._fp({"volume": "7"}, "volume") == 7.0
    assert KalshiClient._fp({}, "volume", default=-1) == -1


def test_analyzer_kalshi_price_and_count_helpers():
    raw = {"volume_fp": "538.00", "yes_bid_dollars": "0.50"}
    assert MarketAnalyzer._kalshi_count(raw, "volume") == 538.0
    assert MarketAnalyzer._kalshi_price(raw, "yes_bid") == 0.50
    # Legacy cents value (>1) gets normalized.
    assert MarketAnalyzer._kalshi_price({"yes_bid": 42}, "yes_bid") == 0.42
