"""WebSocket book-mirror layer: parsing, staleness, dirty-set, orientation.

No sockets — the clients' ``handle_message`` methods are pure and tested
against recorded venue message shapes. The connection loops are exercised
only for their fail-safe contracts (Kalshi dormant without creds).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.ws_books import (
    BookMirror,
    KalshiWsClient,
    PMWsClient,
    WsBookFeed,
)


def test_mirror_set_get_sorted_and_zero_sizes_dropped():
    m = BookMirror()
    m.set_asks("t1", [(0.50, 10), (0.40, 5), (0.45, 0)])
    assert m.get_asks("t1") == [(0.40, 5.0), (0.50, 10.0)]


def test_mirror_staleness_returns_none(monkeypatch):
    m = BookMirror()
    m.set_asks("t1", [(0.5, 1)])
    assert m.get_asks("t1", max_age=30) is not None
    m._ts["t1"] = time.time() - 31
    assert m.get_asks("t1", max_age=30) is None


def test_mirror_dirty_set_drains_once():
    m = BookMirror()
    m.set_asks("a", [(0.5, 1)])
    m.set_asks("b", [(0.5, 1)])
    assert m.pop_dirty() == {"a", "b"}
    assert m.pop_dirty() == set()


def test_pm_book_snapshot_populates_mirror():
    m = BookMirror()
    c = PMWsClient(m, ["tok1"])
    c.handle_message({"event_type": "book", "asset_id": "tok1",
                      "asks": [{"price": "0.45", "size": "120"},
                               {"price": "0.46", "size": "50"}],
                      "bids": [{"price": "0.40", "size": "10"}]})
    assert m.get_asks("tok1") == [(0.45, 120.0), (0.46, 50.0)]


def test_pm_price_change_applies_absolute_level_sizes():
    # price_change carries the NEW ABSOLUTE size at a level: ask-side changes
    # update the ladder in place; size 0 removes the level; bid-side changes
    # leave the ask ladder alone.
    m = BookMirror()
    c = PMWsClient(m, ["tok1"])
    c.handle_message({"event_type": "book", "asset_id": "tok1",
                      "asks": [{"price": "0.45", "size": "120"}]})
    c.handle_message({"event_type": "price_change",
                      "changes": [{"asset_id": "tok1", "price": "0.44",
                                   "size": "10", "side": "SELL"}]})
    assert m.get_asks("tok1") == [(0.44, 10.0), (0.45, 120.0)]
    c.handle_message({"event_type": "price_change",
                      "changes": [{"asset_id": "tok1", "price": "0.45",
                                   "size": "0", "side": "SELL"}]})
    assert m.get_asks("tok1") == [(0.44, 10.0)]
    c.handle_message({"event_type": "price_change",
                      "changes": [{"asset_id": "tok1", "price": "0.40",
                                   "size": "99", "side": "BUY"}]})
    assert m.get_asks("tok1") == [(0.44, 10.0)]


def test_pm_price_change_without_snapshot_never_publishes_partial():
    m = BookMirror()
    c = PMWsClient(m, ["tok1"])
    c.handle_message({"event_type": "price_change",
                      "changes": [{"asset_id": "tok1", "price": "0.44",
                                   "size": "10", "side": "SELL"}]})
    assert m.get_asks("tok1") is None


def test_kalshi_snapshot_orientation_matches_rest_helper():
    # REST kalshi_book returns ask ladders: yes_ask = 1 - best NO bid. The WS
    # mirror must use the same orientation so consumers are source-agnostic.
    m = BookMirror()
    c = KalshiWsClient(m, ["T1"])
    c.handle_message({"type": "orderbook_snapshot",
                      "msg": {"market_ticker": "T1",
                              "yes": [[40, 100]],     # yes BID 40c x100
                              "no": [[55, 200]]}})    # no  BID 55c x200
    assert m.get_asks("kyes:T1") == [(0.45, 200.0)]   # 1 - 0.55
    assert m.get_asks("kno:T1") == [(0.60, 100.0)]    # 1 - 0.40


def test_kalshi_delta_applies_and_removes_levels():
    m = BookMirror()
    c = KalshiWsClient(m, ["T1"])
    c.handle_message({"type": "orderbook_snapshot",
                      "msg": {"market_ticker": "T1", "yes": [[40, 100]], "no": []}})
    c.handle_message({"type": "orderbook_delta",
                      "msg": {"market_ticker": "T1", "side": "yes",
                              "price": 40, "delta": -100}})
    assert m.get_asks("kno:T1") == []                  # level emptied
    c.handle_message({"type": "orderbook_delta",
                      "msg": {"market_ticker": "T1", "side": "no",
                              "price": 30, "delta": 50}})
    assert m.get_asks("kyes:T1") == [(0.70, 50.0)]


def test_kalshi_ws_dormant_without_creds(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    assert KalshiWsClient.creds_present() is False


def test_feed_maps_dirty_tokens_to_pair_keys():
    pairs = [{"ktk": "K1", "tokens": [{"token_id": "t1", "outcome": "Yes"},
                                      {"token_id": "t2", "outcome": "No"}]},
             {"ktk": "K2", "tokens": [{"token_id": "t3", "outcome": "Yes"}]}]
    feed = WsBookFeed(pairs)          # not started: no sockets
    feed.mirror.set_asks("t2", [(0.5, 1)])
    feed.mirror.set_asks("t3", [(0.5, 1)])
    feed.mirror.set_asks("kyes:K2", [(0.5, 1)])
    assert feed.dirty_pairs() == {"K1", "K2"}
    assert feed.dirty_pairs() == set()
