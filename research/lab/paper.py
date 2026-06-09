"""research.lab.paper — forward paper-trading harness (the bridge to live).

The backtest gate judges the PAST; a fund needs forward evidence. This module
runs registered strategies against LIVE Kalshi quotes, records the paper fills
it would have taken (real bid/ask, real fee schedule, one contract each), and
settles them against realized outcomes — placing NO orders, ever. It is the
last honest step before capital: if a strategy's forward paper record diverges
from its backtest, the backtest's assumptions (not reality) are wrong.

Design
------
* **Append-only book.** ``market_data/paper/<book>.jsonl`` — one ``open`` row
  per position, one ``settle`` row when its event resolves; state is replayed
  last-write-wins per position id (the registry/ledger pattern). The book is
  TRACKED in git: the forward record is evidence and must survive containers.
* **Signal must be CURRENT.** A position opens only when the strategy's entry
  fires within the trailing ``recent_min`` minutes of live data. Without this
  gate a panel-so-far replay would "enter" on stale signals from hours ago at
  today's price — look-ahead's forward-mode twin.
* **Real quotes, honest costs.** Fills go through ``lab.execution.FillModel``
  (venue fees + half-spread) against the live panel's REAL ladder quotes,
  with the same strike pinning available to backtests.
* **Frozen tenants.** Strategies enroll in :data:`STRATEGIES` with their
  pre-registered parameters; the harness never tunes anything. The first
  tenant is a deliberate CONTROL — the (DEAD) drift-fade with expectation
  ~0c gross minus ~3c costs — so forward reality can validate the cost model
  itself before any live candidate exists.

CLI (operator/cron; network for --open/--settle)::

    python -m research.lab.paper --open   --book weather_v1 --strategy drift_fade_control
    python -m research.lab.paper --settle --book weather_v1
    python -m research.lab.paper --status --book weather_v1
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from research.lab.types import Panel

_ROOT = Path(__file__).resolve().parents[2]
PAPER_DIR = _ROOT / "market_data" / "paper"

# Sides that pay 1.0 when the outcome ends ABOVE the strike (lab.strategy).
_OVER_SIDES = frozenset({"over", "long_home", "home", "long", "buy", "yes",
                         "cover_home"})


# --------------------------------------------------------------------------- #
# the frozen strategy registry (pre-registered params only; never tuned here)
# --------------------------------------------------------------------------- #
def _drift_fade_control():
    """COST-MODEL CONTROL: fade the 5-bar implied-temp drift (both directions
    of this mechanism are DEAD in backtest: fade ≈ +3.0c gross − 3.24c costs).
    Forward expectation ≈ −0.3c/trade net. If the forward record differs
    materially, the COST MODEL (spread/fee assumptions) is miscalibrated —
    that is exactly what a control tenant is for. Params frozen from the
    pre-registered fade runs (`af866e8a`/`ac6c9c02`)."""
    from research.lab.strategy import Strategy

    K, TAU = 5, 0.75

    def _drift(p):
        mid = np.asarray(p.mid, float)
        out = np.full_like(mid, np.nan)
        out[K:] = mid[K:] - mid[:-K]
        return out

    def entry(p):
        d = _drift(p)
        return np.isfinite(d) & (np.abs(d) >= TAU)

    def side(p, i):
        return "under" if _drift(p)[i] > 0 else "over"   # fade the move

    return Strategy(name="paper.drift_fade_control", entry=entry, side=side,
                    exit="settlement", min_elapsed=3600.0, max_elapsed=1e9,
                    max_stale_min=2.0)


STRATEGIES = {
    "drift_fade_control": {
        "factory": _drift_fade_control,
        "family": "weather", "market": "temp",
        "expectation": "control: ~-0.3c/trade net (validates the cost model)",
    },
}


# --------------------------------------------------------------------------- #
# book persistence (append-only JSONL, replayed)
# --------------------------------------------------------------------------- #
def _book_path(book: str) -> Path:
    return PAPER_DIR / f"{book}.jsonl"


def _append(book: str, row: dict) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    with open(_book_path(book), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def positions(book: str) -> dict:
    """Replay the book: position id -> merged row (open fields + settlement)."""
    path = _book_path(book)
    state: dict = {}
    if not path.exists():
        return state
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = row.get("id")
            if not pid:
                continue
            state.setdefault(pid, {}).update(row)
    return state


def _pid(book: str, strategy: str, event_id: str) -> str:
    return f"{strategy}|{event_id}"   # one position per strategy per event


# --------------------------------------------------------------------------- #
# live data glue (weather family; thin seams, patched in tests)
# --------------------------------------------------------------------------- #
def _live_weather_events() -> list:
    """Open (unsettled) daily-high events across the configured cities."""
    from research.lab.providers import _kalshi_fetch as K
    from research.lab.providers.weather import CITY_SERIES

    out = []
    for city, series in CITY_SERIES.items():
        d = K._get("/events", {"series_ticker": series, "status": "open",
                               "limit": 50})
        for e in (d or {}).get("events", []):
            tick = e.get("event_ticker")
            if tick:
                out.append({"event_ticker": tick, "city": city,
                            "series": series})
        time.sleep(0.1)
    return out


def _live_weather_rec(ev: dict, *, settled: bool = False) -> Optional[dict]:
    """A cache-shaped record for one live (or just-settled) event."""
    from research.lab.providers import _kalshi_fetch as K
    from research.lab.providers.weather import event_date

    tick = ev["event_ticker"]
    markets = K.event_markets(tick)
    if not markets:
        return None
    rows = []
    now = int(time.time())
    for m in markets:
        mt = m.get("ticker")
        if mt is None:
            continue
        try:
            from research.lab.providers.weather import _iso_ts
            start = _iso_ts(m.get("open_time")) or (now - 3 * 86400)
        except Exception:  # noqa: BLE001
            start = now - 3 * 86400
        candles = K.fetch_candles(ev.get("series") or tick.split("-")[0], mt,
                                  start, now)
        rows.append({"ticker": mt, "floor": m.get("floor_strike"),
                     "cap": m.get("cap_strike"), "result": m.get("result"),
                     "sub": m.get("yes_sub_title"), "candles": candles})
        time.sleep(0.1)
    return {"event_ticker": tick, "series": ev.get("series", ""),
            "city": ev.get("city", ""), "date": event_date(tick),
            "markets": rows}


def _build_live_panel(rec: dict) -> Optional[Panel]:
    from research.lab.providers.weather import build_panel
    return build_panel(rec, split_of=lambda gid: "live")


# --------------------------------------------------------------------------- #
# open / settle / status
# --------------------------------------------------------------------------- #
def scan_and_open(strategy_name: str, *, book: str, recent_min: float = 30.0,
                  fill_model=None, now_ts: Optional[float] = None,
                  live_events=_live_weather_events,
                  live_rec=_live_weather_rec, log=print) -> list:
    """One forward pass: open a paper position per live event whose signal
    fires NOW (within the trailing ``recent_min`` of data). Idempotent: an
    event already holding a position for this strategy is skipped."""
    spec = STRATEGIES[strategy_name]
    strat = spec["factory"]()
    if fill_model is None:
        from research.lab.execution import FillModel
        fill_model = FillModel(venue="kalshi", half_spread=0.015)

    held = positions(book)
    opened = []
    for ev in live_events():
        pid = _pid(book, strategy_name, ev["event_ticker"])
        if pid in held:
            continue
        rec = live_rec(ev)
        panel = _build_live_panel(rec) if rec else None
        if panel is None or panel.n < 10:
            continue
        trade = strat._run_one(panel, fill_model)
        if trade is None:
            continue
        now = float(now_ts if now_ts is not None else time.time())
        # The signal must be CURRENT: its bar inside the trailing window.
        signal_ts = float(panel.minute_ts[int(trade.meta["signal_bar"])])
        if now - signal_ts > recent_min * 60.0:
            continue
        row = {
            "kind": "open", "id": pid, "book": book,
            "strategy": strategy_name, "family": spec["family"],
            "market": spec["market"], "event_id": ev["event_ticker"],
            "city": ev.get("city", ""),
            "opened_ts": now, "signal_ts": signal_ts,
            "side": trade.side, "strike": trade.entry_strike,
            "fill_mid": trade.entry_price,
            "half_spread": trade.meta["half_spread"],
            "fee": trade.meta["fee"], "all_in": trade.meta["all_in_price"],
            "status": "open",
        }
        _append(book, row)
        opened.append(row)
        log(f"[paper:{book}] OPEN {pid} {row['side']} strike={row['strike']} "
            f"all_in={row['all_in']:.3f}")
    return opened


def settle_book(book: str, *, live_rec=_live_weather_rec, log=print) -> list:
    """Settle every open position whose event has resolved."""
    settled = []
    for pid, pos in positions(book).items():
        if pos.get("status") != "open":
            continue
        rec = live_rec({"event_ticker": pos["event_id"]}, settled=True)
        panel = _build_live_panel(rec) if rec else None
        if panel is None or panel.final_total is None:
            continue   # not resolved yet (or ambiguous) — stays open
        outcome = float(panel.final_total)
        bet_above = pos["side"] in _OVER_SIDES
        strike = pos.get("strike")
        if strike is None:
            continue
        payoff = 1.0 if ((outcome > float(strike)) == bet_above) else 0.0
        pnl = payoff - float(pos["all_in"])
        row = {"kind": "settle", "id": pid, "settled_ts": time.time(),
               "outcome": outcome, "payoff": payoff, "pnl": pnl,
               "status": "settled"}
        _append(book, row)
        settled.append(row)
        log(f"[paper:{book}] SETTLE {pid} outcome={outcome} payoff={payoff} "
            f"pnl={pnl:+.3f}")
    return settled


def status(book: str) -> dict:
    """Forward record summary: open/settled counts, total + mean pnl (cents)."""
    state = positions(book)
    open_rows = [p for p in state.values() if p.get("status") == "open"]
    done = [p for p in state.values() if p.get("status") == "settled"]
    pnls = [float(p["pnl"]) for p in done if "pnl" in p]
    by_strat: dict = {}
    for p in done:
        by_strat.setdefault(p.get("strategy", "?"), []).append(float(p["pnl"]))
    return {
        "book": book, "open": len(open_rows), "settled": len(done),
        "total_pnl_c": round(100 * sum(pnls), 2) if pnls else 0.0,
        "mean_cents_per_trade": (round(100 * float(np.mean(pnls)), 2)
                                 if pnls else None),
        "by_strategy": {k: {"n": len(v),
                            "mean_c": round(100 * float(np.mean(v)), 2)}
                        for k, v in by_strat.items()},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--book", default="weather_v1")
    ap.add_argument("--strategy", default="drift_fade_control",
                    choices=sorted(STRATEGIES))
    ap.add_argument("--open", action="store_true",
                    help="scan live events and open current-signal positions")
    ap.add_argument("--settle", action="store_true",
                    help="settle resolved positions")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--recent-min", type=float, default=30.0)
    a = ap.parse_args(argv)
    if a.open:
        scan_and_open(a.strategy, book=a.book, recent_min=a.recent_min)
    if a.settle:
        settle_book(a.book)
    if a.status or not (a.open or a.settle):
        print(json.dumps(status(a.book), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
