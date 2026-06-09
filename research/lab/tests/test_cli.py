"""Tests for research.lab.cli — standalone (only types + synthetic fixtures).

Sibling lab modules (session, hypothesis) are monkeypatched, so these tests run
in an isolated worktree where those siblings are not present.
"""
from __future__ import annotations

import sys
import types

import pytest

from research.lab import cli
from research.lab.types import GateResult, Hypothesis


# --------------------------------------------------------------------------- #
# demo — fully self-contained
# --------------------------------------------------------------------------- #
def test_demo_exits_zero_and_prints(capsys):
    rc = cli.main(["demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "demo" in out.lower()
    assert "verdict" in out.lower()
    # gate-shaped summary fields are present
    assert "cents_per_contract" in out
    assert "n_games" in out


def test_demo_reports_full_sample(capsys):
    # Every synthetic game produces one trade -> n == n_games == 40.
    cli.main(["demo"])
    out = capsys.readouterr().out
    assert "n: 40  n_games: 40" in out
    # Verdict is one of the two valid states (settlement-driven, not forced).
    assert ("PASS" in out) or ("FAIL" in out)


# --------------------------------------------------------------------------- #
# parser / arg handling
# --------------------------------------------------------------------------- #
def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0


def test_no_command_prints_help_returns_2(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 2
    assert "usage" in out.lower()


def test_backtest_requires_market():
    with pytest.raises(SystemExit):
        cli.main(["backtest", "mod:STRAT"])


def test_load_strategy_bad_spec():
    with pytest.raises(ValueError):
        cli._load_strategy("no_colon_here")


# --------------------------------------------------------------------------- #
# backtest dispatch — monkeypatch the lazily-imported sibling lab.session
# --------------------------------------------------------------------------- #
def _install_fake_session(monkeypatch, gate, captured):
    class FakeLab:
        def backtest(self, strategy, market, split="nontest"):
            captured["strategy"] = strategy
            captured["market"] = market
            captured["split"] = split
            return gate

    fake = types.ModuleType("research.lab.session")
    fake.lab = lambda: FakeLab()
    monkeypatch.setitem(sys.modules, "research.lab.session", fake)


def _install_fake_strategy_module(monkeypatch, name="fake_strat_mod"):
    mod = types.ModuleType(name)
    mod.STRATEGY = object()
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def test_backtest_dispatches_and_returns_zero_on_pass(monkeypatch, capsys):
    captured: dict = {}
    gate = GateResult(passed=True, cents_per_contract=1.9, ci_lo=0.5,
                      ci_hi=3.3, n=120, n_games=40)
    _install_fake_session(monkeypatch, gate, captured)
    mod = _install_fake_strategy_module(monkeypatch)

    rc = cli.main(["backtest", "fake_strat_mod:STRATEGY",
                   "--market", "total", "--split", "val"])
    out = capsys.readouterr().out

    assert rc == 0
    assert captured["strategy"] is mod.STRATEGY
    assert captured["market"] == "total"
    assert captured["split"] == "val"
    assert "PASS" in out


def test_backtest_returns_one_on_fail(monkeypatch, capsys):
    captured: dict = {}
    gate = GateResult(passed=False, cents_per_contract=-1.0, ci_lo=-3.0,
                      ci_hi=1.0, n=80, n_games=30, reasons=["ci_lo<=0"])
    _install_fake_session(monkeypatch, gate, captured)
    _install_fake_strategy_module(monkeypatch)

    rc = cli.main(["backtest", "fake_strat_mod:STRATEGY", "--market", "total"])
    out = capsys.readouterr().out

    assert rc == 1
    assert captured["split"] == "nontest"  # default
    assert "FAIL" in out
    assert "ci_lo<=0" in out


# --------------------------------------------------------------------------- #
# hypotheses dispatch — monkeypatch lazily-imported sibling lab.hypothesis
# --------------------------------------------------------------------------- #
def _install_fake_hyp(monkeypatch):
    calls: dict = {}
    fake = types.ModuleType("research.lab.hypothesis")

    def query(status=None, market=None, path=None):
        calls["query"] = {"status": status, "market": market}
        return [Hypothesis(market="total", mechanism="m", signal_desc="anchoring gap",
                           direction="over", id="abc123", status="open")]

    def register(h, path=None):
        calls["register"] = h
        h.id = "newid01"
        return h

    def claim(hyp_id, agent, path=None):
        calls["claim"] = {"id": hyp_id, "agent": agent}
        return Hypothesis(market="total", mechanism="m", signal_desc="s",
                          direction="over", id=hyp_id, status="running")

    fake.query = query
    fake.register = register
    fake.claim = claim
    monkeypatch.setitem(sys.modules, "research.lab.hypothesis", fake)
    return calls


def test_hypotheses_list_default(monkeypatch, capsys):
    calls = _install_fake_hyp(monkeypatch)
    rc = cli.main(["hypotheses"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "abc123" in out
    assert "anchoring gap" in out
    assert calls["query"] == {"status": None, "market": None}


def test_hypotheses_list_with_filters(monkeypatch, capsys):
    calls = _install_fake_hyp(monkeypatch)
    rc = cli.main(["hypotheses", "list", "--status", "open", "--market", "total"])
    assert rc == 0
    assert calls["query"] == {"status": "open", "market": "total"}


def test_hypotheses_new(monkeypatch, capsys):
    calls = _install_fake_hyp(monkeypatch)
    rc = cli.main(["hypotheses", "new", "--market", "total",
                   "--mechanism", "pace overreaction",
                   "--signal", "anchoring gap > 5",
                   "--direction", "over"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "newid01" in out
    h = calls["register"]
    assert h.market == "total"
    assert h.mechanism == "pace overreaction"
    assert h.signal_desc == "anchoring gap > 5"
    assert h.direction == "over"


def test_hypotheses_new_missing_args(monkeypatch):
    _install_fake_hyp(monkeypatch)
    with pytest.raises(ValueError):
        cli.main(["hypotheses", "new", "--market", "total"])


def test_hypotheses_claim(monkeypatch, capsys):
    calls = _install_fake_hyp(monkeypatch)
    rc = cli.main(["hypotheses", "claim", "abc123", "--agent", "analyst7"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls["claim"] == {"id": "abc123", "agent": "analyst7"}
    assert "running" in out


def test_hypotheses_claim_requires_id(monkeypatch):
    _install_fake_hyp(monkeypatch)
    with pytest.raises(ValueError):
        cli.main(["hypotheses", "claim"])
