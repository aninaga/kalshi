"""Tests for the MarketDataProvider seam (research.lab.providers).

Covers: registry register/get/for_market/family_of resolution, the NBA
adapter's delegation, and that the public ``data`` façades route non-NBA
markets through the seam. No real cache, no network.

Module-identity note: the suite's conftest purges ``research.lab.*`` from
``sys.modules`` after every test while test modules keep their collection-time
module objects. Tests that patch one lab module and exercise another must
therefore resolve BOTH through ``importlib`` inside the test body, so patch
target and call path share one module generation (the same gotcha documented
in ``test_analyst.py``).
"""
from __future__ import annotations

import importlib
import unittest
from unittest import mock

from research.lab.types import SPREAD, TOTAL, WINNER, synthetic_panel


def _mods():
    """The CURRENT generation of (data, providers), sys.modules-coherent."""
    return (importlib.import_module("research.lab.data"),
            importlib.import_module("research.lab.providers"))


class FakeProvider:
    """Minimal duck-typed provider used to exercise the registry."""

    family = "fake"
    markets = ("widget",)

    def __init__(self):
        self.calls = []

    def enumerate_events(self, market, *, start=None, end=None, split=None):
        self.calls.append(("enumerate", market, split))
        return [{"id": "e1"}]

    def load_panel(self, event, market):
        self.calls.append(("load_panel", event, market))
        return synthetic_panel(game_id="fake_e1", market=TOTAL)

    def load_panels(self, market, split=None, *, start=None, end=None, limit=None):
        self.calls.append(("load_panels", market, split, limit))
        return [self.load_panel({"id": "e1"}, market)]

    def available(self, market):
        return 1

    def event_duration_sec(self, event):
        return None  # untimed family


class TestRegistry(unittest.TestCase):
    def test_builtin_nba_family_known(self) -> None:
        _, providers = _mods()
        self.assertIn("nba", providers.families())
        prov = providers.get("nba")
        self.assertEqual(prov.family, "nba")
        self.assertEqual(tuple(prov.markets), (WINNER, TOTAL, SPREAD))

    def test_get_unknown_family_raises(self) -> None:
        _, providers = _mods()
        with self.assertRaises(KeyError):
            providers.get("nope")

    def test_for_market_resolves_nba_markets(self) -> None:
        _, providers = _mods()
        for market in (WINNER, TOTAL, SPREAD):
            self.assertEqual(providers.for_market(market).family, "nba")
            self.assertEqual(providers.family_of(market), "nba")

    def test_for_market_unknown_raises_valueerror(self) -> None:
        _, providers = _mods()
        with self.assertRaises(ValueError):
            providers.for_market("moneyline")
        self.assertIsNone(providers.family_of("moneyline"))

    def test_register_and_resolve_custom_family(self) -> None:
        _, providers = _mods()
        fake = providers.register(FakeProvider())
        self.assertIs(providers.get("fake"), fake)
        self.assertIs(providers.for_market("widget"), fake)
        self.assertEqual(providers.family_of("widget"), "fake")

    def test_register_requires_family(self) -> None:
        _, providers = _mods()

        class NoFamily:
            pass

        with self.assertRaises(ValueError):
            providers.register(NoFamily())

    def test_duck_typing_satisfies_protocol(self) -> None:
        _, providers = _mods()
        base = importlib.import_module("research.lab.providers.base")
        self.assertIsInstance(FakeProvider(), base.MarketDataProvider)
        self.assertIsInstance(providers.get("nba"), base.MarketDataProvider)


class TestNBAAdapter(unittest.TestCase):
    """The adapter resolves the data module via importlib at call time; patch
    the importlib-current data module so target and call path agree."""

    def test_enumerate_events_filters_window_and_split(self) -> None:
        data, providers = _mods()
        cached = [
            {"date": "2025-10-21", "away": "AAA", "home": "BBB"},
            {"date": "2026-07-01", "away": "GGG", "home": "HHH"},  # out of window
        ]
        splits = {"train_game_ids": ["2025-10-21_AAA_at_BBB"],
                  "val_game_ids": [], "test_game_ids": []}
        prov = providers.get("nba")
        with mock.patch.object(data, "_cached_games", return_value=cached), \
             mock.patch.object(data, "_load_splits", return_value=splits):
            evs = prov.enumerate_events(TOTAL, split="train")
        self.assertEqual(evs, [cached[0]])

    def test_load_panels_delegates_to_data_facade(self) -> None:
        data, providers = _mods()
        prov = providers.get("nba")
        with mock.patch.object(data, "load_panels",
                               return_value=[synthetic_panel(market=TOTAL)]) as lp:
            panels = prov.load_panels(TOTAL, split="nontest", limit=2)
        self.assertEqual(len(panels), 1)
        lp.assert_called_once_with(TOTAL, "nontest", start=None, end=None, limit=2)

    def test_event_duration_is_nba_regulation(self) -> None:
        _, providers = _mods()
        self.assertEqual(providers.get("nba").event_duration_sec({}), 2880.0)


class TestDataFacadeRouting(unittest.TestCase):
    def test_facade_routes_to_registered_provider(self) -> None:
        data, providers = _mods()
        fake = providers.register(FakeProvider())
        panels = data.load_panels("widget", split="train")
        self.assertEqual(len(panels), 1)
        self.assertEqual(data.available("widget"), 1)
        self.assertIn(("load_panels", "widget", "train", None), fake.calls)

    def test_facade_nba_market_stays_module_local(self) -> None:
        # NBA markets must NOT bounce through the provider seam (the module-
        # local fast-path is the historical patching contract).
        data, _ = _mods()
        with mock.patch.object(data, "_provider",
                               side_effect=AssertionError("nba must not hit the seam")), \
             mock.patch.object(data, "_cached_games", return_value=[]), \
             mock.patch.object(data, "_load_splits", return_value={}):
            self.assertEqual(data.load_panels(TOTAL, split="nontest"), [])
            self.assertEqual(data.available(TOTAL), 0)

    def test_facade_unknown_market_raises(self) -> None:
        data, _ = _mods()
        with self.assertRaises(ValueError):
            data.load_panel({"date": "x", "away": "A", "home": "B"}, "moneyline")


if __name__ == "__main__":
    unittest.main()
