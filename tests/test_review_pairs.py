"""Operator review console: queue building, decisions, allowlist writes."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import review_pairs


def _setup(tmp_path, monkeypatch, answers):
    pairs = [
        {"ktk": "K1", "pid": "P1", "kt": "A?", "pt": "A?", "uncertain": True,
         "polarity": "aligned", "pdesc": "rules text long enough to matter"},
        {"ktk": "K2", "pid": "P2", "kt": "B?", "pt": "B?", "uncertain": True,
         "polarity": "aligned", "pdesc": "other rules text"},
        {"ktk": "K3", "pid": "P3", "kt": "C?", "pt": "C?", "uncertain": False},
        {"ktk": "K4", "pid": "P4", "kt": "D?", "pt": "D?", "uncertain": True},
    ]
    watch = tmp_path / "watch.json"
    watch.write_text(json.dumps({"pairs": pairs}))
    ledger = tmp_path / "led.jsonl"
    ledger.write_text(json.dumps({
        "kind": "snapshot",
        "by_market_uncertain": {"K2": 50.0, "K1": 5.0}}) + "\n")
    allow = tmp_path / "allow.json"
    # K4 already denied -> never resurfaces.
    allow.write_text(json.dumps({"approved": [],
                                 "denied": [{"kalshi": "K4", "polymarket": "P4"}]}))
    monkeypatch.setattr(review_pairs, "_verdict_for", lambda p: (None, {
        "raw_data": {"rules_primary": "stub rules"}}))
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a: next(it))
    return watch, ledger, allow


def test_queue_sorted_by_standing_net_and_skips_decided(tmp_path, monkeypatch, capsys):
    watch, ledger, allow = _setup(tmp_path, monkeypatch, [])
    rc = review_pairs.main(["--watchlist", str(watch), "--ledger", str(ledger),
                            "--allowlist", str(allow), "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    # K2 ($50) before K1 ($5); clean K3 and already-denied K4 absent.
    assert out.index("K2") < out.index("K1")
    assert "K3" not in out and "K4" not in out
    assert "Held-for-review: 2 pairs" in out


def test_decisions_written_to_allowlist(tmp_path, monkeypatch):
    watch, ledger, allow = _setup(tmp_path, monkeypatch, ["y", "n"])
    rc = review_pairs.main(["--watchlist", str(watch), "--ledger", str(ledger),
                            "--allowlist", str(allow)])
    assert rc == 0
    d = json.loads(allow.read_text())
    assert [e["kalshi"] for e in d["approved"]] == ["K2"]   # highest net first
    assert d["approved"][0]["polarity"] == "aligned"
    assert {e["kalshi"] for e in d["denied"]} == {"K4", "K1"}


def test_quit_writes_nothing(tmp_path, monkeypatch):
    watch, ledger, allow = _setup(tmp_path, monkeypatch, ["q"])
    before = allow.read_text()
    review_pairs.main(["--watchlist", str(watch), "--ledger", str(ledger),
                       "--allowlist", str(allow)])
    assert allow.read_text() == before


def test_cli_routes_review():
    from kalshi_arbitrage.cli import _PASSTHROUGH
    assert _PASSTHROUGH["review"] == "tools.review_pairs"
