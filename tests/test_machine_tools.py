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
    # Stub discovery to a single pair and pair_structures to a steady open gap,
    # then a close — assert the closed episode is written to the ledger.
    from tools import monitor_arb
    pair = {"ktk": "K1", "pid": "P1", "kt": "Test market", "pt": "Test", "tokens": [], "polarity": "aligned"}
    monkeypatch.setattr(monitor_arb.lp, "discover", lambda: [pair])
    monkeypatch.setattr(monitor_arb, "_load_screens", lambda a: [])
    monkeypatch.setattr(monitor_arb, "_start_ws_feed", lambda a, w: None)
    state = {"n": 0}

    def fake_structures(p, bps, **kw):
        state["n"] += 1
        if state["n"] == 1:  # gap open on first poll, closed thereafter
            return [{**pair, "net": 5.0, "net_edge": 0.03, "size": 100, "cost": 95,
                     "a_src": "K", "b_src": "P"}]
        return []

    monkeypatch.setattr(monitor_arb.lp, "pair_structures", fake_structures)

    ledger = tmp_path / "paper.jsonl"
    # Small positive duration so the bounded loop runs >=2 polls then stops.
    rc = monitor_arb.main(["--interval", "0", "--duration", "0.05", "--ledger", str(ledger)])
    assert rc == 0
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["ktk"] == "K1" and rows[0]["peak_net"] == 5.0
    assert rows[0]["strategy"] == "comp"


def test_merge_watchlists_retains_pairs_dropped_by_catalog():
    # PM /sampling-markets sheds near-resolved extreme-priced markets (observed
    # live: the Musk pair vanished while its gap was ~8% gross) — a cached pair
    # must survive a fresh discovery that no longer contains it.
    from tools.monitor_arb import _merge_watchlists
    fresh = [{"ktk": "K1", "pid": "P1"}]
    cached = [{"ktk": "K1", "pid": "P1"},          # dup of fresh -> not doubled
              {"ktk": "KMUSK", "pid": "PMUSK"},    # dropped by catalog -> retained
              {"ktk": "KDEAD", "pid": "PDEAD"}]    # market closed -> pruned
    merged = _merge_watchlists(fresh, cached, is_alive=lambda p: p["ktk"] != "KDEAD")
    keys = [(p["ktk"], p["pid"]) for p in merged]
    assert keys == [("K1", "P1"), ("KMUSK", "PMUSK")]


def test_merge_watchlists_no_gate_retains_all():
    from tools.monitor_arb import _merge_watchlists
    merged = _merge_watchlists([], [{"ktk": "A", "pid": "B"}])
    assert merged == [{"ktk": "A", "pid": "B"}]


def test_kalshi_alive_fails_open_and_reads_status(monkeypatch):
    from tools import monitor_arb
    # Fetch error -> keep the pair (transient blip must not evict the cache).
    monkeypatch.setattr(monitor_arb.lp, "get", lambda url, **kw: None)
    assert monitor_arb._kalshi_alive({"ktk": "K"})
    monkeypatch.setattr(monitor_arb.lp, "get",
                        lambda url, **kw: {"market": {"status": "settled"}})
    assert not monitor_arb._kalshi_alive({"ktk": "K"})
    monkeypatch.setattr(monitor_arb.lp, "get",
                        lambda url, **kw: {"market": {"status": "active"}})
    assert monitor_arb._kalshi_alive({"ktk": "K"})


def test_monitor_watchlist_cache_persists_and_merges(tmp_path, monkeypatch):
    # First run discovers {K1}; second run discovers {} — with the cache, K1 must
    # still be watched (and re-persisted) on the second run.
    from tools import monitor_arb
    pair = {"ktk": "K1", "pid": "P1", "kt": "T", "pt": "T", "tokens": [],
            "polarity": "aligned"}
    monkeypatch.setattr(monitor_arb.lp, "pair_structures", lambda p, bps, **kw: [])
    monkeypatch.setattr(monitor_arb, "_load_screens", lambda a: [])
    monkeypatch.setattr(monitor_arb, "_start_ws_feed", lambda a, w: None)
    monkeypatch.setattr(monitor_arb, "_kalshi_alive", lambda p: True)
    monkeypatch.setattr(monitor_arb, "_reverify", lambda p: p)
    cache = tmp_path / "watch.json"

    monkeypatch.setattr(monitor_arb.lp, "discover", lambda: [pair])
    assert monitor_arb.main(["--interval", "0", "--duration", "0.01",
                             "--watchlist-cache", str(cache)]) == 0
    assert [p["ktk"] for p in json.loads(cache.read_text())["pairs"]] == ["K1"]

    monkeypatch.setattr(monitor_arb.lp, "discover", lambda: [])
    assert monitor_arb.main(["--interval", "0", "--duration", "0.01",
                             "--watchlist-cache", str(cache)]) == 0
    assert [p["ktk"] for p in json.loads(cache.read_text())["pairs"]] == ["K1"]


def test_watchlist_cache_respects_allowlist_removal(tmp_path, monkeypatch):
    # Cache retention must not override the operator: a pair removed from the
    # allowlist is dropped even though its market is still alive.
    from tools import monitor_arb
    keep = {"ktk": "K1", "pid": "P1", "kt": "T", "pt": "T", "tokens": [],
            "polarity": "aligned"}
    removed = {"ktk": "K2", "pid": "P2", "kt": "T2", "pt": "T2", "tokens": [],
               "polarity": "aligned"}
    cache = tmp_path / "watch.json"
    cache.write_text(json.dumps({"pairs": [keep, removed]}))
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"approved": [{"kalshi": "K1", "polymarket": "P1"}]}))
    monkeypatch.setattr(monitor_arb.lp, "discover", lambda: [])  # catalogs forgot both
    monkeypatch.setattr(monitor_arb, "_kalshi_alive", lambda p: True)
    monkeypatch.setattr(monitor_arb, "_reverify", lambda p: p)
    monkeypatch.setattr(monitor_arb.lp, "pair_structures", lambda p, bps, **kw: [])
    monkeypatch.setattr(monitor_arb, "_load_screens", lambda a: [])
    monkeypatch.setattr(monitor_arb, "_start_ws_feed", lambda a, w: None)
    assert monitor_arb.main(["--interval", "0", "--duration", "0.01",
                             "--allowlist", str(allow),
                             "--watchlist-cache", str(cache)]) == 0
    assert [p["ktk"] for p in json.loads(cache.read_text())["pairs"]] == ["K1"]


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


def test_monitor_sigterm_flushes_open_episodes(tmp_path, monkeypatch):
    # Deploy restarts SIGTERM the monitor with episodes open in memory — they
    # must be flushed to the ledger (still_open + terminated), not lost.
    import os
    import signal
    import threading
    from tools import monitor_arb
    pair = {"ktk": "K1", "pid": "P1", "kt": "T", "pt": "T", "tokens": [],
            "polarity": "aligned"}
    monkeypatch.setattr(monitor_arb.lp, "discover", lambda: [pair])
    monkeypatch.setattr(monitor_arb, "_load_screens", lambda a: [])
    monkeypatch.setattr(monitor_arb, "_start_ws_feed", lambda a, w: None)
    monkeypatch.setattr(monitor_arb.lp, "pair_structures",
                        lambda p, bps, **kw: [{**pair, "net": 5.0, "net_edge": 0.03,
                                               "size": 100, "cost": 95}])
    ledger = tmp_path / "led.jsonl"
    threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    rc = monitor_arb.main(["--interval", "600", "--duration", "0",
                           "--ledger", str(ledger)])
    assert rc == 0
    rows = [json.loads(l) for l in ledger.read_text().splitlines()
            if '"snapshot"' not in l]
    assert len(rows) == 1
    assert rows[0]["still_open"] is True and rows[0]["terminated"] is True
    assert rows[0]["peak_net"] == 5.0
