"""fable_crypto2 — favorite-longshot composite on hourly crypto ladders, PRE-REGISTERED.

LANE: second hypothesis of the crypto family (hourly KXBTCD/KXETHD threshold
ladders; provider research/lab/providers/crypto.py). UNLIKE crypto #1 this is
a PRE-VERIFIED candidate: it arrived from a scout wave through an adversarial
verification round (memo /tmp/codex_r2_fav_tail/analyst_memo.md, preserved at
research/lab/runs/fable_crypto2_favtail_verification_memo_codex_r2.md). The
lane's job is to test the verifier's §"Frozen pre-registration spec" (taker
variant) through the real Strategy -> FillModel -> promotion-gate stack, NOT
to improve it. Memo dev-panel expectation for this exact capped taker rule:
+3.24c/contract, n=587, 492 events, cluster SE 0.93c, t=3.48 (fills AT the
touch + unrounded 0.07*p*(1-p) fee — the lane model below is strictly harsher:
candle-mid + flat 1.5c half-spread + per-order-ceiling Kalshi taker fee).

Frozen spec (memo §Frozen pre-registration spec, taker variant — registered
BEFORE any strategy PnL; registry id below; spec-resolution log in
runs/fable_crypto2_favtail_20260610T081834Z.md):

* Clock: close_ts = event date at hour:00 America/New_York (== memo's
  date+(hour+4)h over this all-EDT Apr-Jun 2026 sample; close_ts == last
  candle minute on 1,195/1,198 cached events). Entry signals read the EXACT
  minute candle at close_ts-900 (T-15) / close_ts-300 (T-5); a threshold with
  no exact-minute candle is ineligible; no eligible threshold => leg skipped.
  Time-to-close conditioning, never elapsed fraction.
* Eligibility (both legs): floor-only threshold (all 25,403 cached markets
  are), required side quote non-missing, non-crossed (yes_ask >= yes_bid when
  both exist), candle volume > 0 in the entry minute.
* LEG A favorite, T-15: yes_ask in [0.70, 0.75] -> BUY 1 YES (side="over").
  Pick |ask-0.72| min; tie lower ask, then lower boundary (memo silent past
  "lower ask"; frozen for determinism).
* LEG B longshot, T-5: yes_bid in [0.02, 0.08] -> SELL 1 YES / buy NO
  (side="under"). Pick highest bid; tie lexical ticker (memo rule).
* Max one threshold per event per leg; hold to settlement (exact
  expiration_value vs floor strike).
* Execution: honest i+1 (fill 1 min after the signal minute) with the strike
  PINNED to the signal's selection (banded quote-conditioned signal == the
  snap-flip hazard case), FillModel(venue="kalshi", half_spread=0.015);
  measured TRAIN in-band median half-spreads are 1.0c (fav) / 0.5c (longshot)
  so the flat 1.5c is conservative vs the memo's at-the-touch fill.
* TWO legs under ONE hypothesis id (the lane Strategy fires once per event):
  the DECISIVE gate object is the COMBINED trade set (the memo carries
  forward "the single combined rule"); per-leg gates are diagnostics — the
  favorite leg alone cannot clear the n>=200 floor by construction.
* Freshness: the spec's own hygiene (exact-minute candle + volume>0 +
  non-crossed) replaces the lane's panel-mid staleness guard
  (max_stale_min inert at 1e9) — panel-mid staleness is not part of the
  verified spec and would arbitrarily drop eligible snapshots.

Run::

    PYTHONPATH=. python3 -m research.scripts.crypto_fav_tail_e2e --register
    PYTHONPATH=. python3 -m research.scripts.crypto_fav_tail_e2e --split train
    PYTHONPATH=. python3 -m research.scripts.crypto_fav_tail_e2e --split nontest
"""
from __future__ import annotations

import argparse
import json
import pickle
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from research.lab import data as lab_data
from research.lab import evaluate as lab_eval
from research.lab import hypothesis as lab_hyp
from research.lab.execution import FillModel
from research.lab.providers.crypto import CACHE_DIR, COIN_PX, SPLITS_PATH
from research.lab.strategy import Strategy
from research.lab.types import Hypothesis, Trades

HYP_ID = "94879b60a8a90098"          # registered 2026-06-10T08:22:55Z, agent fable_crypto2
AGENT = "fable_crypto2"
LEDGER = "research/reports/alpha/ledger.jsonl"
FAMILY = "crypto"
MEMO = "research/lab/runs/fable_crypto2_favtail_verification_memo_codex_r2.md"

# ---- pre-registered strategy parameters (frozen before scoring) -------------
FAV_SNAP_SEC = 900          # favorite signal minute: close_ts - 900 (T-15)
FAV_ASK_LO, FAV_ASK_HI = 0.70, 0.75   # inclusive yes_ask band
FAV_TARGET_ASK = 0.72       # pick |ask-0.72| min; tie lower ask, lower boundary
LS_SNAP_SEC = 300           # longshot signal minute: close_ts - 300 (T-5)
LS_BID_LO, LS_BID_HI = 0.02, 0.08     # inclusive yes_bid band
HALF_SPREAD = 0.015         # lane flat half-spread (> measured 1.0c/0.5c medians)
VENUE = "kalshi"
ENTRY_LATENCY_MIN = 1.0     # honest i+1: fill one minute after the signal
MAX_STALE_MIN = 1.0e9       # inert — the spec's own freshness hygiene applies


def hypothesis() -> Hypothesis:
    return Hypothesis(
        market=COIN_PX,
        mechanism=(
            "Favorite-longshot bias in the final minutes of Kalshi hourly "
            "crypto threshold ladders: late flow overpays for cheap tail YES "
            "legs (lottery demand on a 'one more push' cross) and underprices "
            "near-certain upper favorites, leaving a taker-collectable "
            "premium net of fees. PRE-VERIFIED candidate: adversarial "
            "verification round codex_r2_fav_tail (memo preserved at "
            "research/lab/runs/fable_crypto2_favtail_verification_memo_"
            "codex_r2.md) WEAKENED the broad scout rule and froze this exact "
            "capped taker spec; memo dev-panel expectation +3.24c/contract "
            "(n=587, 492 events, cluster SE 0.93c, t=3.48), survives 2x fee "
            "stress (+2.72c)."),
        signal_desc=(
            "Exact minute candles relative to settlement close (close_ts = "
            "event date at hour:00 America/New_York == memo's date+(hour+4)h "
            "over this all-EDT sample; candle quotes are minute-close). "
            "Eligible thresholds: exact entry-minute candle present, required "
            "side quote non-missing, non-crossed (yes_ask >= yes_bid when "
            "both exist), candle volume > 0 that minute. LEG A at T-15: "
            "yes_ask in [0.70, 0.75]. LEG B at T-5: yes_bid in [0.02, 0.08]. "
            "Max one threshold per event per leg; favorite pick = ask closest "
            "to 0.72 (tie lower ask, then lower boundary), longshot pick = "
            "highest bid (tie lexical ticker)."),
        direction=(
            "TWO legs under this one hypothesis, each one-trade-per-event, "
            "hold to settlement; the DECISIVE gate object is the COMBINED "
            "trade set (per-leg gates are diagnostics; the favorite leg alone "
            "cannot clear the n>=200 floor). LEG A: BUY YES (side='over') the "
            "selected 70-75c favorite at T-15. LEG B: SELL YES / buy NO "
            "(side='under') the selected 2-8c longshot at T-5. Honest i+1: "
            "fill 1 minute after the signal with the strike PINNED to the "
            "signal's selection. FillModel(venue='kalshi', "
            "half_spread=0.015)."),
    )


# --------------------------------------------------------------------------- #
# leg table: per event, the spec's selected contract per leg (quotes only)
# --------------------------------------------------------------------------- #
def _close_ts(rec: dict) -> int:
    """Settlement close: event date at hour:00 ET (memo: date + (hour+4)h)."""
    et = datetime.fromisoformat(rec["date"] + "T00:00:00").replace(
        tzinfo=ZoneInfo("America/New_York"))
    return int(et.timestamp()) + int(rec["hour"]) * 3600


def _exact_minute(rec: dict, ts: int) -> list:
    """(boundary, ticker, candle) for thresholds with a candle EXACTLY at ts."""
    out = []
    for m in rec.get("markets", []):
        if m.get("floor") is None or m.get("cap") is not None:
            continue  # floor-only universe (cache scan: 25,403/25,403)
        for c in m.get("candles", []):
            if c.get("ts") == ts:
                out.append((float(m["floor"]), str(m["ticker"]), c))
                break
    return out


def build_leg_table(panels) -> tuple[dict, dict]:
    """{game_id: {"fav": {...}, "ls": {...}}} from the raw cache records.

    Pure spec application — quotes only, no outcomes. Returns (table, stats);
    ``stats["ladder_miss"]`` counts selected contracts missing from the
    panel's ladder (expected 0: volume>0 in the entry minute implies a finite
    candle mid, so build_panel kept the market).
    """
    table: dict = {}
    stats = {"fav": 0, "ls": 0, "both": 0, "ladder_miss": 0}
    for p in panels:
        path = CACHE_DIR / f"{p.game_id}.pkl"
        if not path.exists():
            continue
        with open(path, "rb") as fh:
            rec = pickle.load(fh)
        cts = _close_ts(rec)
        legs: dict = {}

        cands = []
        for b, tk, c in _exact_minute(rec, cts - FAV_SNAP_SEC):
            ask, bid, vol = c.get("yes_ask"), c.get("yes_bid"), c.get("volume")
            if ask is None or not (vol or 0) > 0:
                continue
            if bid is not None and ask < bid:
                continue
            if FAV_ASK_LO <= ask <= FAV_ASK_HI:
                cands.append((abs(ask - FAV_TARGET_ASK), ask, b, tk))
        if cands:
            cands.sort()
            _, ask, b, tk = cands[0]
            if b in p.ladder:
                legs["fav"] = {"signal_ts": cts - FAV_SNAP_SEC, "strike": b,
                               "ticker": tk, "quote": ask}
            else:
                stats["ladder_miss"] += 1

        cands = []
        for b, tk, c in _exact_minute(rec, cts - LS_SNAP_SEC):
            ask, bid, vol = c.get("yes_ask"), c.get("yes_bid"), c.get("volume")
            if bid is None or not (vol or 0) > 0:
                continue
            if ask is not None and ask < bid:
                continue
            if LS_BID_LO <= bid <= LS_BID_HI:
                cands.append((-bid, tk, b))
        if cands:
            cands.sort()
            nb, tk, b = cands[0]
            if b in p.ladder:
                legs["ls"] = {"signal_ts": cts - LS_SNAP_SEC, "strike": b,
                              "ticker": tk, "quote": -nb}
            else:
                stats["ladder_miss"] += 1

        if legs:
            table[p.game_id] = legs
            stats["fav"] += int("fav" in legs)
            stats["ls"] += int("ls" in legs)
            stats["both"] += int(len(legs) == 2)
    return table, stats


# --------------------------------------------------------------------------- #
# strategies (one per leg; the Strategy seam fires once per event)
# --------------------------------------------------------------------------- #
def _leg_strategy(table: dict, leg: str, side_label: str, name: str) -> Strategy:
    def entry(panel) -> np.ndarray:
        info = table.get(panel.game_id, {}).get(leg)
        if info is None:
            return np.zeros(panel.n, dtype=bool)
        ts = np.asarray(panel.minute_ts, dtype=np.int64)
        return ts == int(info["signal_ts"])

    def side(panel, i: int) -> str:   # noqa: ARG001 — leg-fixed by design
        return side_label

    def pick_strike(panel, i: int):   # noqa: ARG001 — pin the signal's pick
        return table[panel.game_id][leg]["strike"]

    return Strategy(
        name=name, entry=entry, side=side, exit="settlement",
        entry_latency_min=ENTRY_LATENCY_MIN, max_stale_min=MAX_STALE_MIN,
        min_elapsed=0.0, max_elapsed=2.0e9,   # window handled by signal_ts
        pick_strike=pick_strike,
    )


def _summ(trades: Trades, gate) -> dict:
    df = trades.df()
    return {
        "n_trades": len(trades),
        "n_events": (int(df["game_id"].nunique()) if len(df) else 0),
        "avg_fill": (float(df["entry_price"].mean()) if len(df) else None),
        "avg_fee": (float(np.mean([t.meta["fee"] for t in trades.rows]))
                    if len(trades) else None),
        "win_rate": (float((df["payoff"] > 0.5).mean()) if len(df) else None),
        "gross_c": (float((df["payoff"] - df["entry_price"]).mean() * 100)
                    if len(df) else None),
        "gate": {
            "passed": gate.passed,
            "cents_per_contract": gate.cents_per_contract,
            "ci": [gate.ci_lo, gate.ci_hi],
            "n": gate.n, "n_games": gate.n_games,
            "reasons": gate.reasons,
            "cost_sweep": gate.cost_sweep,
            "walkforward": gate.walkforward,
            "adversarial": {k: v for k, v in gate.adversarial.items()},
            "governance": gate.governance,
        },
    }


def run(split: str, ledger: bool = True) -> dict:
    panels = lab_data.load_panels(COIN_PX, split=split)
    table, stats = build_leg_table(panels)
    fills = FillModel(venue=VENUE, half_spread=HALF_SPREAD)
    fav = _leg_strategy(table, "fav", "over", "crypto.favtail.fav_t15").run(
        panels, fill_model=fills)
    ls = _leg_strategy(table, "ls", "under", "crypto.favtail.ls_t5").run(
        panels, fill_model=fills)
    combined = Trades(rows=list(fav.rows) + list(ls.rows))

    lp = LEDGER if ledger else None
    gate_fav = lab_eval.evaluate(fav, ledger_path=lp, family=FAMILY)
    gate_ls = lab_eval.evaluate(ls, ledger_path=lp, family=FAMILY)
    gate_all = lab_eval.evaluate(combined, ledger_path=lp, family=FAMILY)

    out = {
        "hyp_id": HYP_ID, "split": split,
        "params": {"fav": {"snap_sec": FAV_SNAP_SEC, "ask_band": [FAV_ASK_LO, FAV_ASK_HI],
                           "target_ask": FAV_TARGET_ASK, "side": "over (buy YES)"},
                   "ls": {"snap_sec": LS_SNAP_SEC, "bid_band": [LS_BID_LO, LS_BID_HI],
                          "pick": "highest bid, tie lexical ticker",
                          "side": "under (sell YES)"},
                   "volume_filter": "candle volume > 0 in entry minute",
                   "half_spread": HALF_SPREAD, "venue": VENUE,
                   "entry_latency_min": ENTRY_LATENCY_MIN,
                   "max_stale_min": MAX_STALE_MIN},
        "n_panels": len(panels), "leg_table": stats,
        "favorite": _summ(fav, gate_fav),
        "longshot": _summ(ls, gate_ls),
        "combined": _summ(combined, gate_all),
    }

    # Diagnostic reproduction cross-check (declared pre-scoring, NOT a second
    # gate attempt): the memo's dev panel was the 879 LOCKED train+val ids;
    # provider nontest additionally includes 163 post-lock ETH events. Slice
    # the SAME frozen-rule trades down to the locked ids for comparability.
    if split == "nontest":
        sp = json.loads(SPLITS_PATH.read_text())
        locked = set(sp["train_game_ids"]) | set(sp["val_game_ids"])
        sub = Trades(rows=[t for t in combined.rows if t.game_id in locked])
        g = lab_eval.evaluate(sub, ledger_path=None, family=FAMILY)
        out["locked879_subset_diagnostic"] = {
            "n_trades": len(sub), "n_events": int(sub.df()["game_id"].nunique()),
            "cents_per_contract": g.cents_per_contract,
            "ci": [g.ci_lo, g.ci_hi], "passed": g.passed,
        }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--register", action="store_true",
                    help="register + claim the hypothesis (idempotent; prints id)")
    ap.add_argument("--split", default=None, choices=["train", "val", "nontest"])
    ap.add_argument("--no-ledger", action="store_true",
                    help="diagnostic rerun: do not touch the trial ledger")
    a = ap.parse_args(argv)
    if a.register:
        h = lab_hyp.register(hypothesis())
        h = lab_hyp.claim(h.id, AGENT)
        h = lab_hyp.update(h.id, results={"provenance": {
            "pipeline": "scout wave -> adversarial verification -> this lane",
            "verification_round": "codex_r2_fav_tail",
            "memo": "/tmp/codex_r2_fav_tail/analyst_memo.md",
            "memo_preserved_at": MEMO,
            "memo_dev_panel": "nontest dev panel = 879 locked train+val ids "
                              "(memo numbers are in-sample on that panel)",
            "memo_expected": {"net_c": 3.24, "n": 587, "events": 492,
                              "cluster_se_c": 0.93, "t": 3.48,
                              "fee_2x_net_c": 2.72},
            "spec_source": "memo §Frozen pre-registration spec — taker variant",
            "spec_resolution_log":
                "research/lab/runs/fable_crypto2_favtail_20260610T081834Z.md",
        }})
        print(json.dumps({"id": h.id, "status": h.status,
                          "created": h.created}, indent=2))
    if a.split:
        print(json.dumps(run(a.split, ledger=not a.no_ledger),
                         indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
