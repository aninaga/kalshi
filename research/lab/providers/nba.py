"""research.lab.providers.nba — the NBA family as a MarketDataProvider.

A thin adapter over the historical NBA loader in ``research.lab.data``: the
cache enumeration, split filtering, ladder reconstruction, and per-game Panel
construction stay physically in ``data.py`` (several tests monkeypatch those
exact module attributes), and this provider resolves them through the module
at call time so patches keep working. The provider's job is only to give the
NBA path a family identity ("nba") behind the generic seam.
"""
from __future__ import annotations

from typing import Optional

from research.lab.types import MARKETS, Panel

GAME_SEC = 2880.0  # NBA regulation length (48 min); feeds Panel.duration_sec


class NBAProvider:
    """MarketDataProvider for the 2025-26 NBA winner/total/spread cache."""

    family = "nba"
    markets = MARKETS  # (winner, total, spread)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _data():
        # Resolved through sys.modules at call time (importlib) so this adapter
        # always sees the CURRENT data module, mirroring how the lab's other
        # cross-module seams cope with the test suite's module purge.
        import importlib

        return importlib.import_module("research.lab.data")

    # ---------------------------------------------------------------- seam
    def enumerate_events(self, market: str, *, start: Optional[str] = None,
                         end: Optional[str] = None,
                         split: Optional[str] = None) -> list:
        data = self._data()
        predicate, _ = data._split_filter(split)
        lo = start if start is not None else data._DEFAULT_START
        hi = end if end is not None else data._DEFAULT_END
        return [g for g in data._cached_games(market)
                if lo <= g["date"] <= hi and predicate(data._gid_of(g))]

    def load_panel(self, event, market: str) -> Optional[Panel]:
        # The public façade serves NBA markets module-locally.
        return self._data().load_panel(event, market)

    def load_panels(self, market: str, split: Optional[str] = None, *,
                    start: Optional[str] = None, end: Optional[str] = None,
                    limit: Optional[int] = None) -> list:
        return self._data().load_panels(market, split, start=start, end=end,
                                        limit=limit)

    def available(self, market: str) -> int:
        return self._data().available(market)

    def event_duration_sec(self, event) -> Optional[float]:
        return GAME_SEC
