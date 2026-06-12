"""WebSocket order-book mirrors — the tick-resolution capture layer.

The 20s REST sweep samples transient gaps poorly (the analysis showed the
bulk of a high week's edge is minutes-long sports transients, and sub-20s
flickers are invisible to polling). This module maintains live local mirrors
of the venues' books and a *dirty set* of instruments whose books changed,
so the monitor can re-price exactly the pairs that moved, seconds after they
move, instead of waiting for the next sweep.

Two clients, run in ONE daemon thread with its own asyncio loop:

- :class:`PMWsClient` — Polymarket CLOB market channel (PUBLIC, no auth).
  Subscribes to the watch-list's token ids; handles full ``book`` snapshots
  and incremental ``price_change`` events. Live wherever the box has
  outbound network.
- :class:`KalshiWsClient` — Kalshi ``orderbook_delta`` channel. Kalshi's WS
  requires API-key auth, so this client only arms when credentials are
  present (``KALSHI_API_KEY`` + private key); otherwise the Kalshi leg keeps
  coming from REST at sweep cadence. Built + tested now, live on creds.

Design rules:
- The mirror is advisory: every entry carries ``ts`` and consumers must
  treat books older than ``max_age`` as absent (fall back to REST). A parse
  anomaly or reconnect drops the affected books rather than serving stale
  state.
- Reconnect with exponential backoff, full resubscribe, mirrors cleared on
  disconnect (snapshots re-arrive on subscribe).
- Thread-safe via a single coarse lock (book updates are tiny dict writes).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

PM_WS_URL = os.getenv(
    "POLYMARKET_WS_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market")
KALSHI_WS_URL = os.getenv(
    "KALSHI_WS_URL",
    "wss://api.elections.kalshi.com/trade-api/ws/v2")


class BookMirror:
    """Live ask-ladders by instrument id, with timestamps and a dirty set."""

    def __init__(self):
        self._lock = threading.Lock()
        self._asks: Dict[str, List[Tuple[float, float]]] = {}
        self._ts: Dict[str, float] = {}
        self._dirty: set = set()

    def set_asks(self, key: str, asks: Iterable[Tuple[float, float]]) -> None:
        ladder = sorted((float(p), float(s)) for p, s in asks if float(s) > 0)
        with self._lock:
            self._asks[key] = ladder
            self._ts[key] = time.time()
            self._dirty.add(key)

    def drop(self, key: str) -> None:
        with self._lock:
            self._asks.pop(key, None)
            self._ts.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._asks.clear()
            self._ts.clear()

    def get_asks(self, key: str, max_age: float = 30.0) -> Optional[list]:
        """The mirrored ladder, or None if absent/stale (caller RESTs)."""
        with self._lock:
            ts = self._ts.get(key)
            if ts is None or time.time() - ts > max_age:
                return None
            return list(self._asks.get(key) or [])

    def pop_dirty(self) -> set:
        with self._lock:
            d = self._dirty
            self._dirty = set()
            return d

    def stats(self) -> Dict[str, float]:
        with self._lock:
            now = time.time()
            ages = [now - t for t in self._ts.values()]
            return {"books": len(self._asks),
                    "max_age": max(ages) if ages else 0.0}


# --------------------------------------------------------------------------- #
#  Polymarket market channel (public)                                          #
# --------------------------------------------------------------------------- #

class PMWsClient:
    """Mirrors PM token ask-books via the public CLOB market channel."""

    def __init__(self, mirror: BookMirror, token_ids: List[str]):
        self.mirror = mirror
        self.token_ids = [str(t) for t in token_ids if t]
        self.connected = False
        self._asks: Dict[str, Dict[float, float]] = {}   # tok -> price -> size

    # -- message handling (pure; unit-tested without a socket) -------------- #

    def _publish(self, tok: str) -> None:
        self.mirror.set_asks(tok, list(self._asks.get(tok, {}).items()))

    def handle_message(self, msg: Dict) -> None:
        et = msg.get("event_type") or msg.get("type")
        if et == "book":
            tok = str(msg.get("asset_id") or "")
            if tok:
                self._asks[tok] = {float(a.get("price", 0)): float(a.get("size", 0))
                                   for a in (msg.get("asks") or [])}
                self._publish(tok)
        elif et == "price_change":
            # price_change carries the NEW ABSOLUTE size at a level (not a
            # delta). Apply to the ask side; bid-side changes don't move our
            # ask ladders. A change for a token with no snapshot yet is
            # skipped — never publish a partial book.
            changes = msg.get("changes") or msg.get("price_changes")
            if changes is None and msg.get("asset_id"):
                changes = [msg]
            for ch in changes or []:
                tok = str(ch.get("asset_id") or "")
                if not tok or tok not in self._asks:
                    continue
                side = str(ch.get("side") or "").upper()
                if side not in ("SELL", "ASK"):
                    continue
                try:
                    price = float(ch.get("price"))
                    size = float(ch.get("size", 0))
                except (TypeError, ValueError):
                    self.mirror.drop(tok)       # unparseable: invalidate
                    self._asks.pop(tok, None)
                    continue
                if size <= 0:
                    self._asks[tok].pop(price, None)
                else:
                    self._asks[tok][price] = size
                self._publish(tok)
        # 'tick_size_change' and unknown events are ignored.

    # -- connection loop ----------------------------------------------------- #

    async def run(self, stop: asyncio.Event) -> None:
        import aiohttp
        backoff = 1.0
        while not stop.is_set():
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(PM_WS_URL, heartbeat=20) as ws:
                        await ws.send_json({"type": "market",
                                            "assets_ids": self.token_ids})
                        self.connected = True
                        backoff = 1.0
                        logger.info("PM WS connected (%d tokens)", len(self.token_ids))
                        async for raw in ws:
                            if stop.is_set():
                                break
                            if raw.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                payload = json.loads(raw.data)
                            except json.JSONDecodeError:
                                continue
                            msgs = payload if isinstance(payload, list) else [payload]
                            for m in msgs:
                                if isinstance(m, dict):
                                    self.handle_message(m)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("PM WS dropped: %s (reconnect in %.0fs)", exc, backoff)
            self.connected = False
            self.mirror.clear()   # never serve pre-disconnect state
            self._asks.clear()
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)


# --------------------------------------------------------------------------- #
#  Kalshi orderbook_delta channel (auth-gated; armed when creds exist)         #
# --------------------------------------------------------------------------- #

class KalshiWsClient:
    """Mirrors Kalshi books via orderbook_snapshot/_delta. Needs API creds.

    Mirror keys: ``kyes:<ticker>`` and ``kno:<ticker>`` ask-ladders in the
    same orientation the REST :func:`live_probe.kalshi_book` returns
    (yes-ask = 1 - best no-bid), so consumers are source-agnostic.
    """

    def __init__(self, mirror: BookMirror, tickers: List[str]):
        self.mirror = mirror
        self.tickers = [t for t in tickers if t]
        self.connected = False
        self._books: Dict[str, Dict[str, Dict[float, float]]] = {}

    @staticmethod
    def creds_present() -> bool:
        return bool(os.getenv("KALSHI_API_KEY"))

    # -- message handling (pure) --------------------------------------------- #

    def _publish(self, ticker: str) -> None:
        b = self._books.get(ticker) or {}
        yes_bids = b.get("yes") or {}   # price->size, dollars, BID side
        no_bids = b.get("no") or {}
        # Ask ladders from the opposite side's bids (unified book).
        self.mirror.set_asks(f"kyes:{ticker}",
                             ((round(1.0 - p, 6), s) for p, s in no_bids.items() if s > 0))
        self.mirror.set_asks(f"kno:{ticker}",
                             ((round(1.0 - p, 6), s) for p, s in yes_bids.items() if s > 0))

    def handle_message(self, msg: Dict) -> None:
        mtype = msg.get("type")
        data = msg.get("msg") or {}
        ticker = data.get("market_ticker") or ""
        if mtype == "orderbook_snapshot" and ticker:
            book = {"yes": {}, "no": {}}
            for side in ("yes", "no"):
                for p, s in data.get(side) or []:
                    book[side][float(p) / 100.0] = float(s)
            self._books[ticker] = book
            self._publish(ticker)
        elif mtype == "orderbook_delta" and ticker:
            book = self._books.setdefault(ticker, {"yes": {}, "no": {}})
            side = data.get("side")
            if side in ("yes", "no"):
                price = float(data.get("price", 0)) / 100.0
                lvl = book[side].get(price, 0.0) + float(data.get("delta", 0))
                if lvl <= 0:
                    book[side].pop(price, None)
                else:
                    book[side][price] = lvl
                self._publish(ticker)

    async def run(self, stop: asyncio.Event) -> None:
        if not self.creds_present():
            logger.info("Kalshi WS dormant (no KALSHI_API_KEY); REST sweep covers it")
            return
        import aiohttp
        from .kalshi_executor import kalshi_auth_headers   # signs the WS upgrade
        backoff = 1.0
        while not stop.is_set():
            try:
                headers = kalshi_auth_headers("GET", "/trade-api/ws/v2")
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(KALSHI_WS_URL, headers=headers,
                                               heartbeat=10) as ws:
                        await ws.send_json({
                            "id": 1, "cmd": "subscribe",
                            "params": {"channels": ["orderbook_delta"],
                                       "market_tickers": self.tickers}})
                        self.connected = True
                        backoff = 1.0
                        logger.info("Kalshi WS connected (%d tickers)", len(self.tickers))
                        async for raw in ws:
                            if stop.is_set():
                                break
                            if raw.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                msg = json.loads(raw.data)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(msg, dict):
                                self.handle_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Kalshi WS dropped: %s (reconnect in %.0fs)", exc, backoff)
            self.connected = False
            self._books.clear()
            for tk in self.tickers:
                self.mirror.drop(f"kyes:{tk}")
                self.mirror.drop(f"kno:{tk}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)


# --------------------------------------------------------------------------- #
#  Thread harness                                                              #
# --------------------------------------------------------------------------- #

class WsBookFeed:
    """Runs the WS clients in one daemon thread; exposes the mirror + dirty set.

    ``token_to_pair`` maps PM token id -> pair key so the monitor can translate
    book changes into 'these pairs need re-pricing now'.
    """

    def __init__(self, pairs: List[Dict]):
        self.mirror = BookMirror()
        self.token_to_pair: Dict[str, str] = {}
        tokens: List[str] = []
        tickers: List[str] = []
        for p in pairs:
            ktk = p.get("ktk") or ""
            tickers.append(ktk)
            for t in p.get("tokens") or []:
                tid = str(t.get("token_id") or "")
                # 'us:' ids are synthetic Polymarket-US slugs, not CLOB tokens
                # — the global PM channel doesn't know them.
                if tid and not tid.startswith("us:"):
                    tokens.append(tid)
                    self.token_to_pair[tid] = ktk
        self.pm = PMWsClient(self.mirror, tokens)
        self.kalshi = KalshiWsClient(self.mirror, tickers)
        self._stop_evt: Optional[asyncio.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self) -> None:
        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._stop_evt = asyncio.Event()
            try:
                loop.run_until_complete(asyncio.gather(
                    self.pm.run(self._stop_evt),
                    self.kalshi.run(self._stop_evt)))
            except Exception as exc:   # the feed must never kill the monitor
                logger.error("WS feed thread died: %s", exc)
            finally:
                loop.close()
        self._thread = threading.Thread(target=_runner, name="ws-books", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop and self._stop_evt:
            self._loop.call_soon_threadsafe(self._stop_evt.set)

    def dirty_pairs(self) -> set:
        """Pair keys whose PM book changed since the last call."""
        keys = set()
        for k in self.mirror.pop_dirty():
            if k in self.token_to_pair:
                keys.add(self.token_to_pair[k])
            elif k.startswith(("kyes:", "kno:")):
                keys.add(k.split(":", 1)[1])
        return keys

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
