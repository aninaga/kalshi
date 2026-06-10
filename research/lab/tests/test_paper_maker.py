"""Tests for the MAKER paper-trading lifecycle (no network).

Contracts under test: the rest -> fill and rest -> cancel state machine,
horizon discipline (no premature cancel, no fill past the horizon),
idempotent --open/--mark passes, settlement of filled rows, the per-signal
EV accounting in status(), and that the enrolled tenant carries the EXACT
frozen forecast-gap parameters (hyp 3cb384c482d1c2f7 / maker hyp
b8b071fa26ea4933). All live seams are injected with synthetic event records
shaped like the weather provider's cache (including the Phase-1 OHLC keys the
maker fill detector needs).
"""
from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from research.lab import paper

_T0 = 1_780_000_800              # minute-aligned (panels floor ts to minutes)
_TICK = "KXHIGHNY-99JAN01"   # synthetic: never collides with the real store


def _candles(probs, *, t0, spread=0.04, prints=None):
    """Candle rows with the full OHLC key set. ``prints``: {bar: (low, vol)}."""
    prints = prints or {}
    rows = []
    for i, p in enumerate(probs):
        bid = max(p - spread / 2, 0.01)
        ask = min(p + spread / 2, 0.99)
        low, vol = prints.get(i, (None, 0.0))
        rows.append({"ts": t0 + 60 * i, "yes_bid": bid, "yes_ask": ask,
                     "mid": p, "last": p,
                     "price_open": low, "price_high": low, "price_low": low,
                     "price_close": low, "volume": vol,
                     "yes_bid_low": bid, "yes_bid_high": bid,
                     "yes_ask_low": ask, "yes_ask_high": ask,
                     "open_interest": 0.0})
    return rows


def _rec(n=75, *, results=("", "", "", ""), prints_b895=None):
    """4-bucket book <87 | 87-88 | 89-90 | >90 (boundaries 86.5/88.5/90.5).

    Implied temp ~88.7 -> ATM strike 88.5; mid ticks every minute so the
    freshness gate passes. The quote window is 24h (close_ts), so the entry
    fraction crosses the frozen window's 0.05 lower edge at bar 72.
    """
    i = np.arange(n)
    return {
        "event_ticker": _TICK, "series": "KXHIGHNY", "city": "NYC",
        "date": "2099-01-01", "close_ts": _T0 + 86400,
        "markets": [
            {"ticker": "T87", "floor": None, "cap": 87, "result": results[0],
             "candles": _candles(np.full(n, 0.10), t0=_T0)},
            {"ticker": "B87.5", "floor": 87, "cap": 88, "result": results[1],
             "candles": _candles(np.full(n, 0.35), t0=_T0)},
            {"ticker": "B89.5", "floor": 89, "cap": 90, "result": results[2],
             "candles": _candles(0.40 + 0.0005 * i, t0=_T0,
                                 prints=prints_b895)},
            {"ticker": "T90", "floor": 90, "cap": None, "result": results[3],
             "candles": _candles(0.15 + 0.0002 * i, t0=_T0)},
        ],
    }


def _live_panel(rec):
    """The real live-panel builder + an injected forecast feature (93F: with
    NYC's frozen bias -2.00 and implied ~88.7 the debiased gap is ~+6F)."""
    panel = paper._build_live_panel(rec)
    if panel is not None:
        panel.features["forecast_high_f"] = np.full(panel.n, 93.0)
    return panel


_SIGNAL_BAR = 72                       # first bar with elapsed_frac >= 0.05
_SIGNAL_TS = _T0 + _SIGNAL_BAR * 60
_ENTRY_TS = _SIGNAL_TS + 60            # i+1
_HORIZON_END = _ENTRY_TS + 30 * 60


class _MakerBookCase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = paper.PAPER_DIR
        paper.PAPER_DIR = Path(self._tmp.name)
        self.addCleanup(setattr, paper, "PAPER_DIR", self._old)

    def _rest(self, *, now_ts=_SIGNAL_TS + 5 * 60, n=75):
        rec = _rec(n=n)
        events = [{"event_ticker": _TICK, "city": "NYC",
                   "series": "KXHIGHNY"}]
        return paper.scan_and_rest(
            "forecast_gap_maker", book="t", recent_min=30.0, now_ts=now_ts,
            live_events=lambda: events, live_rec=lambda ev, **kw: rec,
            live_panel=_live_panel, log=lambda *a: None)

    def _mark(self, rec, *, now_ts):
        return paper.mark_book("t", now_ts=now_ts,
                               live_rec=lambda ev, **kw: rec,
                               log=lambda *a: None)


class TestRest(_MakerBookCase):
    def test_current_signal_rests_order(self) -> None:
        rested = self._rest()
        self.assertEqual(len(rested), 1)
        row = rested[0]
        self.assertEqual(row["status"], "resting")
        self.assertEqual(row["side"], "over")          # gap > 0: toward forecast
        self.assertEqual(row["strike"], 88.5)          # ATM snap
        self.assertEqual(row["signal_ts"], _SIGNAL_TS)
        self.assertEqual(row["entry_ts"], _ENTRY_TS)
        self.assertEqual(row["horizon_end_ts"], _HORIZON_END)
        # Rest joins the boundary OVER best bid at i+1 = sum of the bids of
        # the legs above 88.5 (study construction, exact).
        j = 73
        expect = (0.40 + 0.0005 * j - 0.02) + (0.15 + 0.0002 * j - 0.02)
        self.assertAlmostEqual(row["rest_price"], expect, places=9)
        self.assertEqual(row["maker_fee_c"], 0.0)      # KXHIGH maker fee

    def test_stale_signal_is_rejected(self) -> None:
        # "now" 2 hours after the signal bar: not a current signal.
        self.assertEqual(self._rest(now_ts=_SIGNAL_TS + 7200), [])

    def test_idempotent_open_pass(self) -> None:
        self.assertEqual(len(self._rest()), 1)
        self.assertEqual(self._rest(), [])             # already held

    def test_resting_orders_never_settle(self) -> None:
        self._rest()
        settled = paper.settle_book(
            "t", live_rec=lambda ev, **kw: _rec(results=("no", "no", "yes", "no")),
            log=lambda *a: None)
        self.assertEqual(settled, [])                  # no position yet


class TestMark(_MakerBookCase):
    def test_print_at_rest_fills(self) -> None:
        rest = self._rest()[0]["rest_price"]
        # Bar 76 (3 min after the rest): B89.5 prints 2 lots at 0.30; the
        # boundary traded-low = 0.30 + T90's mid (~0.165) <= rest -> FILL.
        rec = _rec(n=90, prints_b895={76: (0.30, 2.0)})
        advanced = self._mark(rec, now_ts=_T0 + 90 * 60)
        self.assertEqual(len(advanced), 1)
        row = advanced[0]
        self.assertEqual(row["kind"], "fill")
        self.assertEqual(row["status"], "filled")
        self.assertEqual(row["fill_price"], rest)      # filled AT the rest
        self.assertEqual(row["fee"], 0.0)
        self.assertEqual(row["all_in"], rest)
        self.assertEqual(row["filled_ts"], _T0 + 76 * 60)
        self.assertAlmostEqual(row["fill_lat_min"], 3.0)
        # Idempotent: a second mark pass advances nothing.
        self.assertEqual(self._mark(rec, now_ts=_T0 + 91 * 60), [])

    def test_print_without_volume_does_not_fill(self) -> None:
        self._rest()
        # Traded-low reaches the rest but volume is 0 (quote flicker, not a
        # print): inside the horizon nothing happens; past it, CANCEL.
        rec = _rec(n=90, prints_b895={76: (0.30, 0.0)})
        self.assertEqual(self._mark(rec, now_ts=_T0 + 90 * 60), [])
        out = self._mark(rec, now_ts=_HORIZON_END + 60)
        self.assertEqual(out[0]["kind"], "cancel")

    def test_print_after_horizon_does_not_fill(self) -> None:
        self._rest()
        # A qualifying print at bar 110 = 37 min after entry: outside H=30.
        rec = _rec(n=120, prints_b895={110: (0.30, 2.0)})
        out = self._mark(rec, now_ts=_T0 + 120 * 60)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "cancel")
        self.assertEqual(out[0]["reason"], "horizon_expired_unfilled")

    def test_no_premature_cancel_inside_horizon(self) -> None:
        self._rest()
        rec = _rec(n=90)                               # no prints at all
        # now is still inside the horizon -> keep resting.
        self.assertEqual(self._mark(rec, now_ts=_HORIZON_END - 300), [])
        st = paper.status("t")
        self.assertEqual(st["maker"]["forecast_gap_maker"]["resting"], 1)
        # Horizon expired -> cancel; the row is KEPT (denominator).
        out = self._mark(rec, now_ts=_HORIZON_END + 60)
        self.assertEqual(out[0]["status"], "cancelled")
        self.assertEqual(self._mark(rec, now_ts=_HORIZON_END + 120), [])
        st = paper.status("t")
        m = st["maker"]["forecast_gap_maker"]
        self.assertEqual((m["resting"], m["cancelled"]), (0, 1))
        self.assertEqual(m["fill_rate"], 0.0)
        self.assertEqual(m["ev_per_signal_c"], 0.0)    # unfilled signal = EV 0


class TestSettleFilled(_MakerBookCase):
    def _fill(self):
        rest = self._rest()[0]["rest_price"]
        rec = _rec(n=90, prints_b895={76: (0.30, 2.0)})
        self._mark(rec, now_ts=_T0 + 90 * 60)
        return rest

    def test_settles_filled_row_against_realized_bucket(self) -> None:
        rest = self._fill()
        # 89-90 bucket settles YES -> outcome representative 89.5 > 88.5:
        # the over wins, payoff 1, pnl = 1 - rest (maker fee 0).
        settled = paper.settle_book(
            "t", live_rec=lambda ev, **kw: _rec(results=("no", "no", "yes", "no")),
            log=lambda *a: None)
        self.assertEqual(len(settled), 1)
        s = settled[0]
        self.assertEqual(s["outcome"], 89.5)
        self.assertEqual(s["payoff"], 1.0)
        self.assertAlmostEqual(s["pnl"], 1.0 - rest, places=9)

        st = paper.status("t")
        m = st["maker"]["forecast_gap_maker"]
        self.assertEqual((m["settled"], m["fill_rate"]), (1, 1.0))
        self.assertAlmostEqual(m["ev_per_signal_c"],
                               round(100 * (1.0 - rest), 2))

    def test_unresolved_event_keeps_filled_row(self) -> None:
        self._fill()
        settled = paper.settle_book(
            "t", live_rec=lambda ev, **kw: _rec(), log=lambda *a: None)
        self.assertEqual(settled, [])
        m = paper.status("t")["maker"]["forecast_gap_maker"]
        self.assertEqual(m["filled_unsettled"], 1)
        # An unsettled fill is excluded from the EV denominator until it
        # resolves (no resolved signals yet -> EV undefined).
        self.assertIsNone(m["ev_per_signal_c"])


class TestRegistry(unittest.TestCase):
    def test_maker_tenant_is_registered_with_frozen_params(self) -> None:
        from research.scripts import weather_forecast_gap_e2e as FROZEN
        from research.scripts import weather_maker_study as MK

        spec = paper.STRATEGIES["forecast_gap_maker"]
        self.assertEqual(spec["execution"], "maker")
        self.assertEqual(spec["book"], "weather_maker_v1")
        self.assertEqual(spec["horizon_min"], MK.HEADLINE_H)
        self.assertEqual(spec["maker_fee_c"], 0.0)
        self.assertEqual(spec["hyp_id"], MK.HYP_ID)
        self.assertEqual(spec["parent_hyp_id"], FROZEN.HYP_ID)
        # The factory builds the EXACT frozen parent strategy (no retuning).
        strat = spec["factory"]()
        self.assertEqual(strat.name, "weather.forecast_gap_toward")
        self.assertEqual(strat.max_stale_min, 2.0)
        self.assertEqual(strat.entry_latency_min, 1.0)
        self.assertEqual(FROZEN.GAP_THRESH_F, 2.0)
        self.assertEqual((FROZEN.FRAC_LO, FROZEN.FRAC_HI), (0.05, 0.30))
        self.assertEqual(FROZEN.CITY_BIAS["NYC"], -2.00)


if __name__ == "__main__":
    unittest.main()
