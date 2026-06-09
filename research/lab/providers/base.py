"""research.lab.providers.base — the MarketDataProvider seam.

The lab substrate is market-agnostic everywhere except where Panels come from.
This module defines the duck-typed contract a data provider implements so any
event class (an "event-class family": nba, weather, econ, politics, ...) can
feed the same signals -> strategy -> realistic execution -> gate pipeline.

A provider owns:
  * one ``family`` name (the governance partition key for the DSR hurdle),
  * the set of ``markets`` (plain strings) it can build Panels for,
  * its own cache + splits (per-family ``splits.json``; the TEST split is never
    surfaced unless asked for explicitly — same invariant as the NBA loader).

Providers are looked up via the registry in ``research.lab.providers`` (keyed
by family, with ``for_market`` resolving which family owns a market string).
``research.lab.data`` keeps its public ``load_panels/load_panel/available`` API
and delegates here, so existing callers and tests are unaffected.

Design notes
------------
* Duck-typed ``Protocol`` (not an ABC): tests and adapters can supply any
  object with the right attributes, consistent with the lab's seam style
  (scout ``proposer``, director ``ranker``, analyst ``executor``).
* ``event_duration_sec`` feeds ``Panel.duration_sec`` so time-parameterized
  signals (pace projection) generalize beyond the NBA's 2880s; untimed events
  (e.g. a CPI print) return ``None`` and pace-style signals gracefully no-op.
* Future providers may read OTHER recorded sources (e.g. the live-capture
  parquet lake under ``market_data/live_capture/``) — strictly read-only.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from research.lab.types import Panel


@runtime_checkable
class MarketDataProvider(Protocol):
    """Contract for one event-class family's Panel source.

    Required attributes::

        family:  str                  # e.g. "nba", "weather" — governance key
        markets: tuple[str, ...]      # market strings this family owns
    """

    family: str
    markets: tuple

    def enumerate_events(self, market: str, *, start: Optional[str] = None,
                         end: Optional[str] = None,
                         split: Optional[str] = None) -> list:
        """Event descriptors (provider-defined dicts/ids) with cached data for
        ``market``, date-windowed and split-filtered. The TEST split must be
        excluded unless ``split == "test"`` is passed explicitly."""
        ...

    def load_panel(self, event, market: str) -> Optional[Panel]:
        """Build one Panel for ``event``+``market`` from the provider's cache
        (cache-only — never a live network build). ``None`` if unusable."""
        ...

    def load_panels(self, market: str, split: Optional[str] = None, *,
                    start: Optional[str] = None, end: Optional[str] = None,
                    limit: Optional[int] = None) -> list:
        """Bulk loader: one Panel per enumerated event (skipping unusable)."""
        ...

    def available(self, market: str) -> int:
        """Count of cached events for ``market`` (no build, no network)."""
        ...

    def event_duration_sec(self, event) -> Optional[float]:
        """Trading-relevant event duration in seconds, or ``None`` for untimed
        event classes (no in-event clock — pace-style signals do not apply)."""
        ...
