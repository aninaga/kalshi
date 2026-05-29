"""Durable order-book snapshot + trade sink (WS5).

The live arbitrage bot historically held quotes only in memory (a per-ticker
``price_cache`` + a bounded deque), so the order-book data it saw was lost —
the root reason historical Kalshi/Polymarket book DEPTH is unrecoverable today.
This sink persists timestamped L2 snapshots (and trades) to disk in a long,
lake-compatible shape so FUTURE collection feeds the research lake and the
data-loss gap never recurs.

Schemas (long format, one row per level / per trade):
  book_snapshots: ts_wall, platform, market_id, side('bid'|'ask'), level, price, size
  trades:         ts_wall, platform, market_id, side, price, size

Integration point (from the live loop / websocket handler):
    store = SnapshotStore(Path("market_data/live_capture"))
    store.record_book("kalshi", ticker, ts, bids, asks)   # levels: [(price,size)] or [{'price','size'}]
    store.record_trade("polymarket", token, ts, "BUY", 0.51, 120)
    ...
    store.flush()    # periodically and on shutdown
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

SNAP_COLS = ["ts_wall", "platform", "market_id", "side", "level", "price", "size"]
TRADE_COLS = ["ts_wall", "platform", "market_id", "side", "price", "size"]


def _coerce_levels(levels) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for lv in levels or []:
        if isinstance(lv, dict):
            p = lv.get("price")
            s = lv.get("size") if lv.get("size") is not None else lv.get("quantity")
        elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
            p, s = lv[0], lv[1]
        else:
            continue
        try:
            out.append((float(p), float(s)))
        except (TypeError, ValueError):
            continue
    return out


class SnapshotStore:
    """Buffered, append-only writer for L2 book snapshots and trades."""

    def __init__(self, out_dir, flush_every: int = 2000):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.flush_every = int(flush_every)
        self._snaps: list[tuple] = []
        self._trades: list[tuple] = []

    @property
    def snapshots_path(self) -> Path:
        return self.out_dir / "book_snapshots.parquet"

    @property
    def trades_path(self) -> Path:
        return self.out_dir / "trades.parquet"

    def record_book(self, platform: str, market_id: str, ts_wall, bids, asks) -> None:
        try:
            tsw = int(ts_wall)
        except (TypeError, ValueError):
            return
        for side, levels in (("bid", bids), ("ask", asks)):
            for i, (p, s) in enumerate(_coerce_levels(levels)):
                self._snaps.append((tsw, str(platform), str(market_id), side, i, p, s))
        if len(self._snaps) >= self.flush_every:
            self.flush()

    def record_trade(self, platform: str, market_id: str, ts_wall, side, price, size) -> None:
        try:
            self._trades.append(
                (int(ts_wall), str(platform), str(market_id), str(side),
                 float(price), float(size))
            )
        except (TypeError, ValueError):
            return
        if len(self._trades) >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if self._snaps:
            self._append(self.snapshots_path, pd.DataFrame(self._snaps, columns=SNAP_COLS))
            self._snaps = []
        if self._trades:
            self._append(self.trades_path, pd.DataFrame(self._trades, columns=TRADE_COLS))
            self._trades = []

    @staticmethod
    def _append(path: Path, df: pd.DataFrame) -> None:
        if path.exists():
            df = pd.concat([pd.read_parquet(path), df], ignore_index=True)
        df.to_parquet(path, index=False)
