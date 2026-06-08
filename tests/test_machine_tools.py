"""Machine orchestration wiring: CLI subcommands, ledger, allowlist emission.

No network — exercises the plumbing (arg parsing, ledger persistence, allowlist
writing) with the discovery monkeypatched, so the operator surface is guarded
against regressions independently of live market state.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage import cli


def test_cli_routes_passthrough_verbatim(monkeypatch):
    # main() bypasses argparse for these verbs and hands the tool its raw argv
    # (incl. a leading --flag, which argparse REMAINDER would have dropped).
    import importlib
    captured = {}
    for cmd, mod_name in cli._PASSTHROUGH.items():
        mod = importlib.import_module(mod_name)
        monkeypatch.setattr(mod, "main", lambda argv, _c=cmd: captured.__setitem__(_c, argv) or 0)
        rc = cli.main([cmd, "--min-net-edge", "0.01"])
        assert rc == 0
        assert captured[cmd] == ["--min-net-edge", "0.01"]


def test_cli_help_lists_machine_subcommands():
    p = cli.build_parser()
    choices = p._subparsers._group_actions[0].choices
    for cmd in ("find-arb", "monitor", "backtest-arb", "machine"):
        assert cmd in choices


def test_monitor_ledger_records_episode(tmp_path, monkeypatch):
    # Stub discovery to a single pair and price_pair to a steady open gap, then a
    # close — assert the closed episode is written to the ledger.
    from tools import monitor_arb
    pair = {"ktk": "K1", "pid": "P1", "kt": "Test market", "pt": "Test", "tokens": [], "polarity": "aligned"}
    monkeypatch.setattr(monitor_arb.lp, "discover", lambda: [pair])
    state = {"n": 0}

    def fake_price(p, bps):
        state["n"] += 1
        if state["n"] == 1:  # gap open on first poll, closed thereafter
            return {**pair, "net": 5.0, "net_edge": 0.03, "size": 100, "cost": 95,
                    "a_src": "K", "b_src": "P"}
        return None

    monkeypatch.setattr(monitor_arb.lp, "price_pair", fake_price)

    ledger = tmp_path / "paper.jsonl"
    # Small positive duration so the bounded loop runs >=2 polls then stops.
    rc = monitor_arb.main(["--interval", "0", "--duration", "0.05", "--ledger", str(ledger)])
    assert rc == 0
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["ktk"] == "K1" and rows[0]["peak_net"] == 5.0


def test_run_machine_writes_allowlist_excluding_review(tmp_path, monkeypatch):
    from tools import run_machine
    genuine = {"ktk": "KG", "pid": "PG", "kt": "Genuine", "pt": "g", "polarity": "aligned",
               "net": 10.0, "net_edge": 0.02, "cost": 490, "size": 500, "uncertain": False,
               "a_src": "K", "b_src": "P"}
    review = {**genuine, "ktk": "KR", "pid": "PR", "kt": "Arrest", "uncertain": True}
    monkeypatch.setattr(run_machine.lp, "discover", lambda: [genuine, review])
    monkeypatch.setattr(run_machine.lp, "price_pair",
                        lambda p, bps: genuine if p["ktk"] == "KG" else review)

    out = tmp_path / "allow.json"
    rc = run_machine.main(["--allowlist", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    # Genuine allowlisted; the uncertain/review pair is excluded by design.
    ids = {e["kalshi"] for e in data["approved"]}
    assert ids == {"KG"}
