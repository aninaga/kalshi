"""Ledger-honesty regression tests (defects C5 + structural re-count).

Two ways the capture pipeline used to overstate realized P&L:

  C5 — monitor_arb dropped the ``uncertain`` (basis-risk) flag on episode rows,
       so basis-risk markets (uncertain resolution, NOT risk-free) looked clean.

  Structural — ``open_eps`` lives only in monitor process memory, so a restart
       re-opens every standing arb and re-flushes it as a fresh ``still_open``
       row at the next shutdown. ``analyze_ledger`` summed ``peak_net`` over ALL
       rows with no dedup and projected $/day from that sum — counting the same
       standing position once per restart and laundering basis-risk into the
       headline.

These tests assert the monitor now propagates ``uncertain`` + ``run_id`` and the
analyzer reports clean-closed separately, dedups restart-duplicated still_open
flushes, and never projects from the raw peak_net sum.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.analyze_ledger import summarize


def _episode(**kw):
    base = dict(market="Mkt", ktk="K", key="comp:K", strategy="comp",
                peak_net=10.0, peak_edge=0.02, open_minutes=5.0, ticks=3)
    base.update(kw)
    return base


def test_clean_closed_reported_separately_from_basis_risk():
    rows = [
        _episode(market="CleanA", key="comp:A", peak_net=12.0, uncertain=False),
        _episode(market="CleanB", key="comp:B", peak_net=9.0, uncertain=False),
        _episode(market="ArrestX", key="comp:X", peak_net=50.0, uncertain=True),
    ]
    s = summarize(rows)
    # Clean-closed is ONLY the two non-uncertain closed episodes.
    assert s["clean_closed"]["n"] == 2
    assert s["clean_closed"]["net"] == pytest.approx(21.0)
    # The fat basis-risk episode is quarantined into its own bucket, NOT clean.
    assert s["basis_risk"]["n"] == 1
    assert s["basis_risk"]["net"] == pytest.approx(50.0)
    # The basis-risk $50 must NOT leak into the clean realized figure.
    assert s["clean_closed"]["net"] < s["basis_risk"]["net"]


def test_restart_duplicated_still_open_is_deduped_not_double_counted():
    # The SAME standing arb (same episode key) re-flushed across THREE restarts.
    # Process memory is fresh each time, so peak_net wobbles, but it is one
    # standing position — not three captures.
    rows = [
        _episode(market="Standing", key="comp:S", peak_net=8.0,
                 still_open=True, run_id="run1", uncertain=False),
        _episode(market="Standing", key="comp:S", peak_net=11.0,
                 still_open=True, run_id="run2", uncertain=False),
        _episode(market="Standing", key="comp:S", peak_net=7.0,
                 still_open=True, run_id="run3", uncertain=False),
        # A genuinely closed, clean episode alongside.
        _episode(market="ClosedClean", key="comp:C", peak_net=6.0,
                 still_open=False, uncertain=False),
    ]
    s = summarize(rows)
    # The three still_open rows collapse to ONE standing position (max peak_net).
    assert s["standing"]["n_raw"] == 3
    assert s["standing"]["n"] == 1
    assert s["standing"]["net"] == pytest.approx(11.0)  # max of the chain, once
    # Standing mass must NOT be folded into realized clean-closed.
    assert s["clean_closed"]["n"] == 1
    assert s["clean_closed"]["net"] == pytest.approx(6.0)
    # Raw sum (back-compat) still double-counts: 8+11+7+6 = 32. The honest
    # clean-closed number (6) is what callers must project from.
    assert s["total_net"] == pytest.approx(32.0)
    assert s["clean_closed"]["net"] != s["total_net"]


def test_main_projects_only_from_clean_closed(tmp_path, capsys):
    from tools import analyze_ledger
    led = tmp_path / "ledger.jsonl"
    rows = [
        _episode(market="CleanClosed", key="comp:C", peak_net=10.0,
                 still_open=False, uncertain=False),
        # restart-duplicated standing arb worth a notional $100 across flushes
        _episode(market="Standing", key="comp:S", peak_net=100.0,
                 still_open=True, run_id="r1", uncertain=False),
        _episode(market="Standing", key="comp:S", peak_net=100.0,
                 still_open=True, run_id="r2", uncertain=False),
        # basis-risk closed worth a notional $80
        _episode(market="Arrest", key="comp:X", peak_net=80.0,
                 still_open=False, uncertain=True),
    ]
    led.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rc = analyze_ledger.main(["--path", str(led), "--session-hours", "24"])
    assert rc == 0
    out = capsys.readouterr().out
    # Projection is driven off clean-closed ($10), NOT the $290 raw sum.
    assert "run-rate (CLEAN-CLOSED only): $10.00" in out
    assert "~$10/day" in out
    # And the inflated buckets are surfaced honestly, not as realized P&L.
    assert "BASIS-RISK" in out and "$80.00" in out
    assert "STANDING" in out
    # The double-counted $290 / $200 standing sum must NOT appear as run-rate.
    assert "$290" not in out
    assert "~$200/day" not in out


def test_monitor_episode_rows_carry_uncertain_and_run_id(tmp_path, monkeypatch):
    # End-to-end: a basis-risk gap that opens then closes must write an episode
    # row that preserves uncertain=True + a run_id + pm_fee_bps, so the analyzer
    # can quarantine it. Previously the flag was dropped at flush time (C5).
    from tools import monitor_arb
    pair = {"ktk": "K1", "pid": "P1", "kt": "Arrest market", "pt": "Arrest",
            "tokens": [], "polarity": "aligned"}
    monkeypatch.setattr(monitor_arb.lp, "discover", lambda: [pair])
    monkeypatch.setattr(monitor_arb, "_load_screens", lambda a: [])
    monkeypatch.setattr(monitor_arb, "_start_ws_feed", lambda a, w: None)
    state = {"n": 0}

    def fake_structures(p, bps, **kw):
        state["n"] += 1
        if state["n"] == 1:  # basis-risk gap open on first poll, gone after
            return [{**pair, "net": 5.0, "net_edge": 0.03, "size": 100, "cost": 95,
                     "uncertain": True}]
        return []

    monkeypatch.setattr(monitor_arb.lp, "pair_structures", fake_structures)
    ledger = tmp_path / "paper.jsonl"
    rc = monitor_arb.main(["--interval", "0", "--duration", "0.05",
                           "--ledger", str(ledger), "--pm-fee-bps", "700"])
    assert rc == 0
    eps = [json.loads(l) for l in ledger.read_text().splitlines()
           if '"snapshot"' not in l]
    assert len(eps) == 1
    row = eps[0]
    assert row["uncertain"] is True            # C5: flag preserved through close
    assert row.get("run_id")                   # restart-dedup lineage present
    assert row["pm_fee_bps"] == 700            # fee context stamped
    # And the analyzer routes it to basis-risk, never clean-closed.
    s = summarize(eps)
    assert s["clean_closed"]["n"] == 0
    assert s["basis_risk"]["n"] == 1
    assert s["basis_risk"]["net"] == pytest.approx(5.0)
