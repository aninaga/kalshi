"""Tests for research.lab.session — the ``Lab`` façade (Unit 7).

These tests stand alone: only ``research.lab.types`` (+ this module) is required.
Every sibling lab module the façade delegates to (``data``/``strategy``/
``evaluate``/``hypothesis``/``execution``) is injected as a fake into
``sys.modules`` so we can assert that ``Lab`` wires calls through correctly and
that :meth:`Lab.backtest` composes ``load -> run -> evaluate`` in order — without
requiring any real sibling to exist.
"""
from __future__ import annotations

import sys
import types as _types
from dataclasses import dataclass, field

import pytest

from research.lab import session as session_mod
from research.lab.session import Lab, lab
from research.lab.types import (
    GateResult,
    Hypothesis,
    Trades,
    synthetic_panels,
)

# Sibling modules the façade lazily imports. We replace each with a fake.
_SIBLINGS = (
    "research.lab.data",
    "research.lab.strategy",
    "research.lab.evaluate",
    "research.lab.hypothesis",
    "research.lab.execution",
)


# --------------------------------------------------------------------------- #
# Fakes for the sibling lab modules                                           #
# --------------------------------------------------------------------------- #
@dataclass
class _StubFillModel:
    """Stands in for research.lab.execution.FillModel."""

    half_spread: float = 0.015
    venue: str = "polymarket"


class _CallLog:
    """Shared ordered record of every sibling call, for composition assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def record(self, _label: str, **kw) -> None:
        self.calls.append((_label, kw))

    @property
    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@dataclass
class _StubStrategy:
    """Stands in for a research.lab.strategy.Strategy instance."""

    name: str
    entry: object = None
    side: object = None
    log: _CallLog | None = None
    trades: Trades | None = None
    seen_fill_model: object = None
    seen_panels: object = None
    extra: dict = field(default_factory=dict)

    def run(self, panels, fill_model=None) -> Trades:
        self.seen_panels = panels
        self.seen_fill_model = fill_model
        if self.log is not None:
            self.log.record("strategy.run", panels=panels, fill_model=fill_model)
        return self.trades if self.trades is not None else Trades(rows=[])


def _install_siblings(monkeypatch, log: _CallLog, *,
                      panels=None, trades=None, gate=None,
                      realistic=None) -> dict:
    """Inject fake sibling modules into sys.modules; return a handle dict.

    The façade imports these lazily, so swapping ``sys.modules`` entries is
    sufficient — no real sibling module ever has to exist.
    """
    panels = panels if panels is not None else synthetic_panels(n_games=3)
    trades = trades if trades is not None else Trades(rows=[])
    gate = gate if gate is not None else GateResult(
        passed=True, cents_per_contract=1.0, ci_lo=0.5, ci_hi=1.5, n=3, n_games=3)
    realistic = realistic if realistic is not None else _StubFillModel()

    # --- research.lab.data ------------------------------------------------- #
    data_mod = _types.ModuleType("research.lab.data")

    def load_panels(market, split=None, **kw):
        log.record("data.load_panels", market=market, split=split, kw=kw)
        return panels

    data_mod.load_panels = load_panels  # type: ignore[attr-defined]

    # --- research.lab.strategy --------------------------------------------- #
    strategy_mod = _types.ModuleType("research.lab.strategy")

    def _Strategy(name, entry=None, side=None, **kw):
        log.record("strategy.Strategy", name=name, entry=entry, side=side, kw=kw)
        return _StubStrategy(name=name, entry=entry, side=side, log=log,
                             trades=trades, extra=kw)

    strategy_mod.Strategy = _Strategy  # type: ignore[attr-defined]

    # --- research.lab.evaluate --------------------------------------------- #
    evaluate_mod = _types.ModuleType("research.lab.evaluate")

    def evaluate(tr, **kw):
        log.record("evaluate.evaluate", trades=tr, kw=kw)
        return gate

    evaluate_mod.evaluate = evaluate  # type: ignore[attr-defined]

    # --- research.lab.hypothesis ------------------------------------------- #
    hypothesis_mod = _types.ModuleType("research.lab.hypothesis")
    _open_list: list[Hypothesis] = []

    def register(h):
        log.record("hypothesis.register", hypothesis=h)
        h.id = h.hash()
        _open_list.append(h)
        return h

    def open_hypotheses():
        log.record("hypothesis.open_hypotheses")
        return list(_open_list)

    hypothesis_mod.register = register  # type: ignore[attr-defined]
    hypothesis_mod.open_hypotheses = open_hypotheses  # type: ignore[attr-defined]

    # --- research.lab.execution -------------------------------------------- #
    execution_mod = _types.ModuleType("research.lab.execution")
    execution_mod.REALISTIC = realistic  # type: ignore[attr-defined]
    execution_mod.FillModel = _StubFillModel  # type: ignore[attr-defined]

    for name, mod in {
        "research.lab.data": data_mod,
        "research.lab.strategy": strategy_mod,
        "research.lab.evaluate": evaluate_mod,
        "research.lab.hypothesis": hypothesis_mod,
        "research.lab.execution": execution_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    return {
        "panels": panels, "trades": trades, "gate": gate, "realistic": realistic,
        "data": data_mod, "strategy": strategy_mod, "evaluate": evaluate_mod,
        "hypothesis": hypothesis_mod, "execution": execution_mod,
    }


@pytest.fixture()
def log() -> _CallLog:
    return _CallLog()


# --------------------------------------------------------------------------- #
# Import-time / construction                                                   #
# --------------------------------------------------------------------------- #
def test_session_imports_with_only_types_present():
    # If we got here, importing session_mod did not require any sibling.
    assert hasattr(session_mod, "Lab")
    assert hasattr(session_mod, "lab")


def test_lab_factory_returns_default_session():
    obj = lab()
    assert isinstance(obj, Lab)
    # The realistic fill model is resolved lazily, so it stays None until used.
    assert obj.fill_model is None


def test_lab_exposes_full_contract_surface():
    obj = Lab()
    for method in ("load", "strategy", "evaluate", "backtest",
                   "register", "open_hypotheses"):
        assert callable(getattr(obj, method))


# --------------------------------------------------------------------------- #
# fill_model resolution (realistic default)                                    #
# --------------------------------------------------------------------------- #
def test_fill_model_defaults_to_execution_realistic(monkeypatch, log):
    h = _install_siblings(monkeypatch, log)
    obj = Lab()
    resolved = obj._resolve_fill_model()
    assert resolved is h["realistic"]
    # And it is cached on the instance.
    assert obj.fill_model is h["realistic"]


def test_explicit_fill_model_is_not_overridden(monkeypatch, log):
    _install_siblings(monkeypatch, log)
    custom = _StubFillModel(half_spread=0.02)
    obj = Lab(fill_model=custom)
    assert obj._resolve_fill_model() is custom


# --------------------------------------------------------------------------- #
# load                                                                          #
# --------------------------------------------------------------------------- #
def test_load_delegates_to_data_load_panels(monkeypatch, log):
    h = _install_siblings(monkeypatch, log)
    out = Lab().load("total", "nontest", limit=5)
    assert out is h["panels"]
    assert log.names == ["data.load_panels"]
    _, kw = log.calls[0]
    assert kw["market"] == "total"
    assert kw["split"] == "nontest"
    assert kw["kw"] == {"limit": 5}


def test_load_default_split_is_none(monkeypatch, log):
    _install_siblings(monkeypatch, log)
    Lab().load("winner")
    _, kw = log.calls[0]
    assert kw["split"] is None


# --------------------------------------------------------------------------- #
# strategy                                                                      #
# --------------------------------------------------------------------------- #
def test_strategy_delegates_to_strategy_constructor(monkeypatch, log):
    _install_siblings(monkeypatch, log)
    entry = lambda p: None  # noqa: E731
    side = lambda p, i: "over"  # noqa: E731
    strat = Lab().strategy("my_strat", entry, side, min_elapsed=720.0)
    assert strat.name == "my_strat"
    assert log.names == ["strategy.Strategy"]
    _, kw = log.calls[0]
    assert kw["name"] == "my_strat"
    assert kw["entry"] is entry
    assert kw["side"] is side
    assert kw["kw"] == {"min_elapsed": 720.0}


# --------------------------------------------------------------------------- #
# evaluate                                                                      #
# --------------------------------------------------------------------------- #
def test_evaluate_delegates_and_injects_fill_model(monkeypatch, log):
    h = _install_siblings(monkeypatch, log)
    trades = Trades(rows=[])
    result = Lab().evaluate(trades)
    assert result is h["gate"]
    assert log.names == ["evaluate.evaluate"]
    _, kw = log.calls[0]
    assert kw["trades"] is trades
    # Realistic fill model threaded through by default.
    assert kw["kw"]["fill_model"] is h["realistic"]


def test_evaluate_respects_caller_fill_model_override(monkeypatch, log):
    _install_siblings(monkeypatch, log)
    override = _StubFillModel(venue="kalshi")
    Lab().evaluate(Trades(rows=[]), fill_model=override)
    _, kw = log.calls[0]
    assert kw["kw"]["fill_model"] is override


# --------------------------------------------------------------------------- #
# backtest — the headline composition                                          #
# --------------------------------------------------------------------------- #
def test_backtest_composes_load_run_evaluate_in_order(monkeypatch, log):
    h = _install_siblings(monkeypatch, log)
    strat = Lab().strategy("s", lambda p: None, lambda p, i: "over")
    log.calls.clear()  # ignore the construction call; focus on backtest order

    result = Lab().backtest(strat, "total")
    assert result is h["gate"]

    # Exact pipeline order: load -> run -> evaluate.
    assert log.names == [
        "data.load_panels",
        "strategy.run",
        "evaluate.evaluate",
    ]


def test_backtest_threads_panels_and_fill_model_through(monkeypatch, log):
    h = _install_siblings(monkeypatch, log)
    strat = Lab().strategy("s", lambda p: None, lambda p, i: "over")
    log.calls.clear()

    Lab().backtest(strat, "total", split="train")

    by_name = {name: kw for name, kw in log.calls}
    # load got the requested market/split
    assert by_name["data.load_panels"]["market"] == "total"
    assert by_name["data.load_panels"]["split"] == "train"
    # run got the loaded panels and the realistic fill model
    assert by_name["strategy.run"]["panels"] is h["panels"]
    assert by_name["strategy.run"]["fill_model"] is h["realistic"]
    # evaluate got the trades the strategy produced, with the same fill model
    assert by_name["evaluate.evaluate"]["trades"] is h["trades"]
    assert by_name["evaluate.evaluate"]["kw"]["fill_model"] is h["realistic"]


def test_backtest_default_split_is_nontest(monkeypatch, log):
    _install_siblings(monkeypatch, log)
    strat = Lab().strategy("s", lambda p: None, lambda p, i: "x")
    log.calls.clear()
    Lab().backtest(strat, "spread")
    by_name = {name: kw for name, kw in log.calls}
    assert by_name["data.load_panels"]["split"] == "nontest"


def test_backtest_uses_single_fill_model_instance_for_run_and_evaluate(monkeypatch, log):
    _install_siblings(monkeypatch, log)
    strat = Lab().strategy("s", lambda p: None, lambda p, i: "x")
    log.calls.clear()
    Lab().backtest(strat, "total")
    by_name = {name: kw for name, kw in log.calls}
    fm_run = by_name["strategy.run"]["fill_model"]
    fm_eval = by_name["evaluate.evaluate"]["kw"]["fill_model"]
    assert fm_run is fm_eval


# --------------------------------------------------------------------------- #
# hypothesis registry                                                          #
# --------------------------------------------------------------------------- #
def test_register_delegates_to_hypothesis_register(monkeypatch, log):
    _install_siblings(monkeypatch, log)
    hyp = Hypothesis(
        market="total",
        mechanism="pace anchoring persists late",
        signal_desc="projection minus mid",
        direction="over",
    )
    out = Lab().register(hyp)
    assert out is hyp
    assert out.id == hyp.hash()
    assert log.names == ["hypothesis.register"]


def test_open_hypotheses_delegates(monkeypatch, log):
    _install_siblings(monkeypatch, log)
    obj = Lab()
    obj.register(Hypothesis(market="total", mechanism="m", signal_desc="s",
                            direction="over"))
    log.calls.clear()
    opened = obj.open_hypotheses()
    assert log.names == ["hypothesis.open_hypotheses"]
    assert len(opened) == 1


# --------------------------------------------------------------------------- #
# E2E smoke (synthetic, monkeypatched stubs)                                   #
# --------------------------------------------------------------------------- #
def test_e2e_smoke_backtest_runs_three_stages(monkeypatch, log):
    """Lab().backtest(stub_strategy, "total") drives all three stages."""
    _install_siblings(monkeypatch, log)
    stub_strategy = Lab().strategy("smoke", lambda p: None, lambda p, i: "over")
    log.calls.clear()

    result = Lab().backtest(stub_strategy, "total")

    assert isinstance(result, GateResult)
    assert log.names == ["data.load_panels", "strategy.run", "evaluate.evaluate"]
