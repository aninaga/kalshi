"""Tests for the forward paper-trading harness (no network).

Contracts under test: the current-signal gate (no entering on stale signals —
forward look-ahead's twin), idempotent opens, settle math against realized
outcomes, and book replay. Live fetch seams are injected with fakes built on
the weather provider's own synthetic event records.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from research.lab import paper
from research.lab.execution import FillModel


def _candles(probs, *, t0, spread=0.04):
    out = []
    for i, p in enumerate(probs):
        out.append({"ts": t0 + 60 * i, "yes_bid": max(p - spread / 2, 0.01),
                    "yes_ask": min(p + spread / 2, 0.99), "mid": p, "last": p})
    return out


def _rec(tick="KXHIGHNY-26JUN10", n=120, t0=1_780_000_000, drift=0.0,
         results=("no", "no", "yes", "no")):
    """4-bucket live book: <87 | 87-88 | 89-90 | >90.

    ``drift`` is applied over the LAST 10 bars only, so the implied-temp move
    is concentrated and recent — the fade signal fires near "now", which is
    what the current-signal gate admits.
    """
    ramp = np.concatenate([np.zeros(n - 10), np.linspace(0.0, drift, 10)])
    return {
        "event_ticker": tick, "series": "KXHIGHNY", "city": "NYC",
        "date": "2026-06-10",
        "markets": [
            {"ticker": "T87", "floor": None, "cap": 87, "result": results[0],
             "candles": _candles(np.clip(0.30 - ramp, 0.01, 0.99), t0=t0)},
            {"ticker": "B87.5", "floor": 87, "cap": 88, "result": results[1],
             "candles": _candles(np.clip(0.30 - ramp / 2, 0.01, 0.99), t0=t0)},
            {"ticker": "B89.5", "floor": 89, "cap": 90, "result": results[2],
             "candles": _candles(np.clip(0.25 + ramp, 0.01, 0.99), t0=t0)},
            {"ticker": "T90", "floor": 90, "cap": None, "result": results[3],
             "candles": _candles(np.clip(0.15 + ramp / 2, 0.01, 0.99), t0=t0)},
        ],
    }


_T0 = 1_780_000_000


class _PaperBookCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = paper.PAPER_DIR
        paper.PAPER_DIR = Path(self._tmp.name)
        self.addCleanup(setattr, paper, "PAPER_DIR", self._old)


class TestScanAndOpen(_PaperBookCase):
    def _open(self, *, drift, now_offset_min, results=(None,) * 4, n=120):
        rec = _rec(drift=drift, n=n,
                   results=tuple(r or "" for r in results))
        events = [{"event_ticker": rec["event_ticker"], "city": "NYC",
                   "series": "KXHIGHNY"}]
        now = _T0 + (n - 1) * 60 + now_offset_min * 60
        return paper.scan_and_open(
            "drift_fade_control", book="t", recent_min=30.0,
            fill_model=FillModel(venue="kalshi", half_spread=0.015),
            now_ts=now,
            live_events=lambda: events, live_rec=lambda ev, **kw: rec,
            log=lambda *a: None)

    def test_current_signal_opens_position(self) -> None:
        # Strong recent drift -> fade signal fires near the last bars.
        opened = self._open(drift=3.0, now_offset_min=5)
        self.assertEqual(len(opened), 1)
        row = opened[0]
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["side"], "under")        # fades an UP drift
        self.assertIn(row["strike"], (86.5, 88.5, 90.5))
        self.assertGreater(row["all_in"], row["fill_mid"])  # spread+fee billed

    def test_stale_signal_is_rejected(self) -> None:
        # Same panel, but "now" is 3 hours after the last bar: signal stale.
        opened = self._open(drift=3.0, now_offset_min=180)
        self.assertEqual(opened, [])

    def test_no_signal_no_position(self) -> None:
        opened = self._open(drift=0.0, now_offset_min=5)   # flat book
        self.assertEqual(opened, [])

    def test_idempotent_per_event(self) -> None:
        first = self._open(drift=3.0, now_offset_min=5)
        second = self._open(drift=3.0, now_offset_min=5)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])                   # already held


class TestSettle(_PaperBookCase):
    def test_settles_against_realized_bucket(self) -> None:
        opened = paper.scan_and_open(
            "drift_fade_control", book="t", recent_min=30.0,
            fill_model=FillModel(venue="kalshi", half_spread=0.015),
            now_ts=_T0 + 119 * 60 + 300,
            live_events=lambda: [{"event_ticker": "KXHIGHNY-26JUN10",
                                  "city": "NYC", "series": "KXHIGHNY"}],
            live_rec=lambda ev, **kw: _rec(drift=3.0, results=("",) * 4),
            log=lambda *a: None)
        self.assertEqual(len(opened), 1)

        # Event resolves: 89-90 bucket wins -> outcome representative 89.5.
        settled = paper.settle_book(
            "t", live_rec=lambda ev, **kw: _rec(drift=3.0), log=lambda *a: None)
        self.assertEqual(len(settled), 1)
        s = settled[0]
        self.assertEqual(s["outcome"], 89.5)
        row = opened[0]
        bet_above = row["side"] in paper._OVER_SIDES
        expect_payoff = 1.0 if ((89.5 > row["strike"]) == bet_above) else 0.0
        self.assertEqual(s["payoff"], expect_payoff)
        self.assertAlmostEqual(s["pnl"], expect_payoff - row["all_in"], places=9)

        st = paper.status("t")
        self.assertEqual((st["open"], st["settled"]), (0, 1))
        self.assertIsNotNone(st["mean_cents_per_trade"])

    def test_unresolved_event_stays_open(self) -> None:
        paper.scan_and_open(
            "drift_fade_control", book="t", recent_min=30.0,
            fill_model=FillModel(venue="kalshi", half_spread=0.015),
            now_ts=_T0 + 119 * 60 + 300,
            live_events=lambda: [{"event_ticker": "KXHIGHNY-26JUN10",
                                  "city": "NYC", "series": "KXHIGHNY"}],
            live_rec=lambda ev, **kw: _rec(drift=3.0, results=("",) * 4),
            log=lambda *a: None)
        settled = paper.settle_book(
            "t", live_rec=lambda ev, **kw: _rec(drift=3.0, results=("",) * 4),
            log=lambda *a: None)
        self.assertEqual(settled, [])
        self.assertEqual(paper.status("t")["open"], 1)


class TestRegistry(unittest.TestCase):
    def test_control_tenant_is_registered_and_buildable(self) -> None:
        spec = paper.STRATEGIES["drift_fade_control"]
        strat = spec["factory"]()
        self.assertEqual(spec["family"], "weather")
        self.assertTrue(callable(strat.entry))


if __name__ == "__main__":
    unittest.main()
