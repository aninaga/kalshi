"""Structure-complete capture: PM intra-market + event-level dutch arbs.

A single Kalshi binary market cannot self-arb (unified YES/NO book), but PM's
YES/NO tokens trade in separate books, and multi-outcome EVENTS have a
separate book per outcome on both venues. These tests pin the generalized
N-leg basket walk (fee-aware, depth-aware, marginal-profitability stop), the
top-of-book catalog screens for both venues, the precise dutch pricing with
cross-venue leg merging, and the monitor's strategy-tagged episode records.

Guarantee semantics pinned here:
  - YES-dutch pays $1/unit only if the outcome set is EXHAUSTIVE -> candidates
    without a catch-all outcome are flagged uncertain (held-for-review).
  - NO-dutch pays >= N-1 whenever AT MOST one outcome happens -> safe under
    the event's mutual-exclusivity flag alone, never uncertain.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage import live_probe as lp
from kalshi_arbitrage.mock_execution import FeeModel


def _k(price, size):
    return (price, size, True)


def _p(price, size):
    return (price, size, False)


# --- walk_basket: the generalized N-leg economics --------------------------- #

def test_yes_dutch_three_legs_nets_payout_minus_cost_and_fees():
    ladders = [[_k(0.30, 100)], [_k(0.30, 100)], [_k(0.30, 100)]]
    res = lp.walk_basket(ladders, payout=1.0)
    assert res is not None and res["size"] == 100
    fees = 3 * FeeModel.kalshi_taker_fee(0.30, 100)
    assert res["net"] == pytest.approx(1.0 * 100 - 0.90 * 100 - fees)


def test_no_dutch_payout_is_n_minus_one():
    # 3 outcomes, buy NO on each at 0.60: cost 1.80/unit vs payout 2.00/unit.
    ladders = [[_k(0.60, 50)], [_k(0.60, 50)], [_k(0.60, 50)]]
    res = lp.walk_basket(ladders, payout=2.0)
    assert res is not None and res["size"] == 50
    fees = 3 * FeeModel.kalshi_taker_fee(0.60, 50)
    assert res["net"] == pytest.approx(2.0 * 50 - 1.80 * 50 - fees)


def test_walk_stops_when_marginal_contract_unprofitable():
    # Second level pushes the sum past payout: only the first 10 fill.
    ladders = [[_k(0.45, 10), _k(0.60, 90)], [_k(0.45, 100)]]
    res = lp.walk_basket(ladders, payout=1.0)
    assert res is not None and res["size"] == 10


def test_walk_size_capped_by_thinnest_leg():
    ladders = [[_k(0.30, 5)], [_k(0.30, 500)], [_k(0.30, 500)]]
    res = lp.walk_basket(ladders, payout=1.0)
    assert res is not None and res["size"] == 5


def test_walk_returns_none_when_no_edge_or_empty():
    assert lp.walk_basket([[_k(0.55, 10)], [_k(0.55, 10)]], payout=1.0) is None
    assert lp.walk_basket([[], [_k(0.1, 10)]], payout=1.0) is None
    assert lp.walk_basket([], payout=1.0) is None


def test_mixed_venue_leg_charges_each_levels_own_fee():
    # One leg buys 10 on Kalshi then 10 on PM (merged ladder): fees must be
    # Kalshi-curve for the first 10 and PM-curve for the next 10.
    merged = [_k(0.30, 10), _p(0.31, 10)]
    other = [_k(0.40, 20)]
    res = lp.walk_basket([merged, other], pm_fee_bps=500, payout=1.0)
    assert res is not None and res["size"] == 20
    exp_fees = (FeeModel.kalshi_taker_fee(0.30, 10)
                + FeeModel.polymarket_taker_fee(0.31, 10, 500)
                + FeeModel.kalshi_taker_fee(0.40, 10)
                + FeeModel.kalshi_taker_fee(0.40, 10))
    exp_cost = 0.30 * 10 + 0.31 * 10 + 0.40 * 20
    assert res["net"] == pytest.approx(20 - exp_cost - exp_fees, abs=1e-6)


# --- pair_structures: PM intra rides the same book fetch -------------------- #

PAIR = {"ktk": "K1", "pid": "P1", "kt": "K title", "pt": "PM title",
        "polarity": "aligned", "uncertain": False,
        "tokens": [{"token_id": "ty", "outcome": "Yes"},
                   {"token_id": "tn", "outcome": "No"}]}


def test_pm_intra_detected_when_token_asks_sum_below_dollar(monkeypatch):
    monkeypatch.setattr(lp, "kalshi_book", lambda tk: ([], []))
    books = {"ty": [(0.40, 100)], "tn": [(0.55, 100)]}
    monkeypatch.setattr(lp, "pm_asks", lambda t: books[t])
    out = lp.pair_structures(PAIR)
    assert [r["strategy"] for r in out] == ["pm_intra"]
    r = out[0]
    assert r["key"] == "pm_intra:K1" and r["uncertain"] is False
    fees = (FeeModel.polymarket_taker_fee(0.40, 100, 500)
            + FeeModel.polymarket_taker_fee(0.55, 100, 500))
    assert r["net"] == pytest.approx(100 - 95.0 - fees)


def test_price_pair_back_compat_returns_comp_only(monkeypatch):
    monkeypatch.setattr(lp, "kalshi_book", lambda tk: ([(0.40, 100)], [(0.62, 100)]))
    books = {"ty": [(0.45, 100)], "tn": [(0.55, 100)]}
    monkeypatch.setattr(lp, "pm_asks", lambda t: books[t])
    r = lp.price_pair(PAIR)
    assert r is not None and r["strategy"] == "comp"
    assert r["a_src"] == "K" and r["b_src"] == "P"   # K-yes 0.40 + PM-no 0.55


# --- catalog screens (zero extra calls) ------------------------------------- #

def _mk_event(mx, asks, bids, titles=None, ev="EV1"):
    ms = []
    for i, (a, b) in enumerate(zip(asks, bids)):
        ms.append({"ticker": f"{ev}-{i}", "status": "active",
                   "yes_ask_dollars": a, "yes_bid_dollars": b,
                   "title": (titles or [f"Outcome {i}"] * len(asks))[i],
                   "yes_sub_title": ""})
    return {"event_ticker": ev, "mutually_exclusive": mx, "title": "Event", "markets": ms}


def test_kalshi_screen_flags_yes_dutch_only_with_catchall():
    asks, bids = [0.30, 0.30, 0.30], [0.25, 0.25, 0.25]
    no_catch = _mk_event(True, asks, bids)
    catch = _mk_event(True, asks, bids, titles=["A", "B", "Any other candidate"])
    assert lp.kalshi_event_screens([no_catch]) == []
    out = lp.kalshi_event_screens([catch])
    assert len(out) == 1 and out[0]["kind"] == "dutch_yes"
    assert out[0]["exhaustive"] and out[0]["gross"] == pytest.approx(0.10)


def test_kalshi_screen_flags_no_dutch_under_mx_alone():
    ev = _mk_event(True, [0.50, 0.50, 0.50], [0.40, 0.40, 0.40])
    out = lp.kalshi_event_screens([ev])
    assert len(out) == 1 and out[0]["kind"] == "dutch_no"
    assert out[0]["gross"] == pytest.approx(0.20)
    # Non-MX events are never dutch candidates.
    assert lp.kalshi_event_screens([_mk_event(False, [0.5] * 3, [0.4] * 3)]) == []


def test_conditional_race_with_subject_catchall_is_not_exhaustive():
    # The live false positive (2026-06-10): "Which AI will be the first to hit
    # 1550?" summed to $0.83 with an "Other" outcome — but "Other" only covers
    # subject-space; if NO model ever hits 1550 every outcome resolves NO and
    # the basket pays $0. A subject catch-all must NOT make a race exhaustive.
    ev = _mk_event(True, [0.30, 0.30, 0.23], [0.25, 0.25, 0.20],
                   titles=["Claude", "ChatGPT", "Other"])
    ev["title"] = "Which AI will be the first to hit 1550 on Text Arena?"
    assert lp.kalshi_event_screens([ev]) == []
    # An explicit NONE outcome covers the never-happens path -> exhaustive.
    ev2 = _mk_event(True, [0.30, 0.30, 0.23], [0.25, 0.25, 0.20],
                    titles=["Claude", "ChatGPT", "No one hits it"])
    ev2["title"] = "Which AI will be the first to hit 1550 on Text Arena?"
    out = lp.kalshi_event_screens([ev2])
    assert len(out) == 1 and out[0]["kind"] == "dutch_yes" and out[0]["exhaustive"]
    # Winner-type contest (someone always wins): subject catch-all suffices.
    ev3 = _mk_event(True, [0.30, 0.30, 0.23], [0.25, 0.25, 0.20],
                    titles=["Seattle", "San Francisco", "Any other team"])
    ev3["title"] = "NFC West Division Winner"
    assert [c["kind"] for c in lp.kalshi_event_screens([ev3])] == ["dutch_yes"]


def test_range_ladder_endpoints_are_exhaustive():
    ev = _mk_event(True, [0.30, 0.30, 0.30], [0.25, 0.25, 0.25],
                   titles=["89° or below", "90° to 91°", "92° or above"])
    ev["title"] = "Highest temperature in Washington DC on Jun 10?"
    out = lp.kalshi_event_screens([ev])
    assert len(out) == 1 and out[0]["kind"] == "dutch_yes" and out[0]["exhaustive"]


def test_kalshi_screen_missing_offer_blocks_yes_dutch():
    ev = _mk_event(True, [0.30, 0.30, 0], [0.25, 0.25, 0.25],
                   titles=["A", "B", "Any other"])
    assert lp.kalshi_event_screens([ev]) == []


def test_pm_screen_reads_negrisk_events(monkeypatch):
    page = [{
        "negRisk": True, "slug": "ev-slug", "title": "PM Event",
        "markets": [
            {"active": True, "closed": False, "bestAsk": 0.30, "bestBid": 0.25,
             "question": "Will A win?", "clobTokenIds": json.dumps(["1", "2"])},
            {"active": True, "closed": False, "bestAsk": 0.30, "bestBid": 0.25,
             "question": "Will B win?", "clobTokenIds": json.dumps(["3", "4"])},
            {"active": True, "closed": False, "bestAsk": 0.30, "bestBid": 0.25,
             "question": "Will any other person win?", "clobTokenIds": json.dumps(["5", "6"])},
        ],
    }]
    monkeypatch.setattr(lp, "get", lambda url, timeout=25: page if "offset=0" in url else [])
    out = lp.pm_event_screens()
    assert len(out) == 1 and out[0]["kind"] == "dutch_yes"
    assert out[0]["ev"] == "pm:ev-slug" and out[0]["n"] == 3


# --- precise dutch pricing --------------------------------------------------- #

def test_kalshi_no_dutch_priced_with_cross_venue_leg_merge(monkeypatch):
    # 3-outcome event; outcome 0 has a verified PM pair whose NO token is
    # cheaper than Kalshi's NO at the top level -> merged ladder buys PM first.
    cand = {"ev": "EV1", "kind": "dutch_no", "gross": 0.2, "n": 3,
            "tickers": ["EV1-0", "EV1-1", "EV1-2"], "exhaustive": False,
            "title": "Event"}
    books = {"EV1-0": ([(0.45, 100)], [(0.62, 100)]),
             "EV1-1": ([(0.40, 100)], [(0.58, 100)]),
             "EV1-2": ([(0.40, 100)], [(0.58, 100)])}
    monkeypatch.setattr(lp, "kalshi_book", lambda tk: books[tk])
    monkeypatch.setattr(lp, "pm_asks", lambda t: [(0.55, 30)] if t == "tn" else [(0.99, 1)])
    pairs = {"EV1-0": dict(PAIR, ktk="EV1-0")}
    res = lp.price_kalshi_dutch(cand, pairs_by_ktk=pairs)
    assert res is not None
    assert res["strategy"] == "dutch_no" and res["uncertain"] is False
    assert res["key"] == "dutch_no:EV1"
    # Leg 0 must have bought the cheaper PM NO (0.55) before Kalshi's 0.62.
    assert res["avg_prices"][0] < 0.62


def test_yes_dutch_without_catchall_is_uncertain(monkeypatch):
    cand = {"ev": "EV2", "kind": "dutch_yes", "gross": 0.1, "n": 2,
            "tickers": ["EV2-0", "EV2-1"], "exhaustive": False, "title": "E2"}
    monkeypatch.setattr(lp, "kalshi_book", lambda tk: ([(0.40, 50)], [(0.70, 50)]))
    res = lp.price_kalshi_dutch(cand, pairs_by_ktk={})
    assert res is not None and res["strategy"] == "dutch_yes"
    assert res["uncertain"] is True   # exhaustiveness unproven -> review


def test_pm_dutch_prices_token_books(monkeypatch):
    cand = {"ev": "pm:ev", "kind": "dutch_yes", "gross": 0.1, "n": 2,
            "tokens": [["1", "2"], ["3", "4"]], "exhaustive": True, "title": "E"}
    monkeypatch.setattr(lp, "pm_asks",
                        lambda t: [(0.45, 80)] if t in ("1", "3") else [(0.99, 1)])
    res = lp.price_pm_dutch(cand)
    assert res is not None and res["size"] == 80
    assert res["uncertain"] is False


# --- monitor integration: strategy-tagged episodes --------------------------- #

def test_monitor_records_dutch_episode_with_strategy(tmp_path, monkeypatch):
    from tools import monitor_arb
    pair = dict(PAIR)
    monkeypatch.setattr(monitor_arb.lp, "discover", lambda: [pair])
    monkeypatch.setattr(monitor_arb.lp, "pair_structures", lambda p, bps: [])
    cand = {"ev": "EV1", "kind": "dutch_no", "gross": 0.2, "n": 3,
            "tickers": ["a", "b", "c"], "exhaustive": False, "title": "Event"}
    monkeypatch.setattr(monitor_arb, "_load_screens", lambda a: [cand])
    state = {"n": 0}

    def fake_dutch(c, pairs_by_ktk=None, pm_fee_bps=500):
        state["n"] += 1
        if state["n"] == 1:
            return {"strategy": "dutch_no", "key": "dutch_no:EV1", "kt": "Event",
                    "net": 7.0, "net_edge": 0.04, "size": 50, "cost": 175,
                    "uncertain": False}
        return None

    monkeypatch.setattr(monitor_arb.lp, "price_kalshi_dutch", fake_dutch)
    ledger = tmp_path / "led.jsonl"
    rc = monitor_arb.main(["--interval", "0", "--duration", "0.05",
                           "--ledger", str(ledger)])
    assert rc == 0
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    eps = [r for r in rows if r.get("kind") != "snapshot"]
    assert len(eps) == 1
    assert eps[0]["strategy"] == "dutch_no" and eps[0]["key"] == "dutch_no:EV1"
    assert eps[0]["peak_net"] == 7.0
