"""Standalone tests for research.lab.evaluate.

These use only research.lab.types + the real research.scorer (present in the
repo) — no real data, no sibling lab modules. We construct Trades directly with
a KNOWN positive edge (should clear the gate's bootstrap CI and the full gate)
and a KNOWN null (should fail), then assert the GateResult is populated, the
cost sweep is monotone, and the walk-forward is keyed by month.
"""
from __future__ import annotations

import numpy as np
import pytest

from research.lab.evaluate import evaluate
from research.lab.types import GateResult, Trade, Trades

_TEAMS = [f"T{i:02d}" for i in range(20)]
_MONTHS = ["2025-11", "2025-12", "2026-01", "2026-02"]


def _build_trades(
    *,
    edge: float,
    n: int = 240,
    noise: float = 0.05,
    seed: int = 0,
    stale_min: float | None = 1.0,
    win_rate_matches_price: bool = True,
) -> Trades:
    """One trade per synthetic game with ``pnl ~ edge + N(0, noise)``.

    Trades are spread evenly across teams and months so the gate's parity /
    season-half / concentration sub-checks have well-balanced inputs. With a
    positive ``edge`` small relative to ``noise``/sqrt(n) the block-bootstrap CI
    lower bound clears zero; with ``edge=0`` it straddles zero.
    """
    rng = np.random.default_rng(seed)
    pnl = edge + rng.normal(0.0, noise, n)
    rows = []
    for i in range(n):
        month = _MONTHS[i % len(_MONTHS)]
        day = 1 + (i // len(_MONTHS)) % 27
        date = f"{month}-{day:02d}"
        home = _TEAMS[i % len(_TEAMS)]
        primary = _TEAMS[(i + 1) % len(_TEAMS)]
        # Make win_rate ~ avg entry_price so estimator-bias check passes.
        entry_price = 0.50
        payoff = 1.0 if (rng.random() < (0.50 if win_rate_matches_price else 0.95)) else 0.0
        rows.append(Trade(
            game_id=f"g{i:04d}",
            date=date,
            market="total",
            home_team=home,
            primary_team=primary,
            side="over",
            entry_ts=float(1_700_000_000 + i * 3600),
            exit_ts=float(1_700_000_000 + i * 3600 + 7200),
            entry_price=entry_price,
            payoff=payoff,
            pnl=float(pnl[i]),
            entry_strike=220.0,
            meta={"stale_min": stale_min} if stale_min is not None else {},
        ))
    return Trades(rows=rows)


# ---------------------------------------------------------------------------
# Known-positive signal
# ---------------------------------------------------------------------------


def test_known_positive_passes_and_populates():
    trades = _build_trades(edge=0.03, n=260, noise=0.04, seed=1)
    res = evaluate(trades)

    assert isinstance(res, GateResult)
    # Positive point estimate in CENTS (×100).
    assert res.cents_per_contract > 0
    assert res.ci_lo > 0          # bootstrap CI clears zero -> gate-ish pass
    assert res.ci_hi > res.ci_lo
    assert res.n == 260
    assert res.n_games == 260     # one trade per game
    assert res.passed is True
    assert res.reasons == []


def test_known_positive_adversarial_checks_pass():
    trades = _build_trades(edge=0.03, n=260, noise=0.04, seed=2)
    res = evaluate(trades)
    adv = res.adversarial
    for key in ("staleness", "estimator_bias_vs_unconditional", "concentration", "oos"):
        assert key in adv
        assert "passed" in adv[key] and "detail" in adv[key]
        assert adv[key]["passed"] is True, f"{key}: {adv[key]['detail']}"


# ---------------------------------------------------------------------------
# Known null
# ---------------------------------------------------------------------------


def test_known_null_fails():
    trades = _build_trades(edge=0.0, n=260, noise=0.06, seed=3)
    res = evaluate(trades)

    assert isinstance(res, GateResult)
    assert res.passed is False
    assert res.ci_lo <= 0          # CI straddles / sits below zero
    assert res.reasons             # at least one failure reason


# ---------------------------------------------------------------------------
# Cost sweep
# ---------------------------------------------------------------------------


def test_cost_sweep_monotone_and_keyed_by_cents():
    trades = _build_trades(edge=0.03, n=240, noise=0.04, seed=4)
    res = evaluate(trades, cost_sweep=(0.0, 0.01, 0.02, 0.03, 0.04))

    # Keyed by cents.
    assert set(res.cost_sweep) == {0.0, 1.0, 2.0, 3.0, 4.0}
    cents = [res.cost_sweep[k]["cents"] for k in sorted(res.cost_sweep)]
    # Subtracting a larger constant cost -> strictly lower mean cents.
    assert all(b < a for a, b in zip(cents, cents[1:]))
    # ~1 cent drop per 0.01 cost step.
    assert cents[0] - cents[1] == pytest.approx(1.0, abs=1e-6)
    # Each entry has the expected shape.
    for v in res.cost_sweep.values():
        assert set(v) == {"cents", "ci_lo", "gate"}


def test_cost_sweep_gate_degrades_with_cost():
    trades = _build_trades(edge=0.02, n=240, noise=0.04, seed=5)
    res = evaluate(trades, cost_sweep=(0.0, 0.04))
    # A 4-cent drag should not be easier to pass than zero cost.
    assert res.cost_sweep[0.0]["ci_lo"] > res.cost_sweep[4.0]["ci_lo"]


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


def test_walkforward_keyed_by_month():
    trades = _build_trades(edge=0.03, n=240, noise=0.04, seed=6)
    res = evaluate(trades, walkforward=True)

    assert set(res.walkforward) == set(_MONTHS)
    total = 0
    for m, v in res.walkforward.items():
        assert set(v) == {"n", "cents", "ci_lo", "gate"}
        assert v["n"] > 0
        total += v["n"]
    assert total == 240


def test_walkforward_disabled():
    trades = _build_trades(edge=0.03, n=240, seed=7)
    res = evaluate(trades, walkforward=False, adversarial=False)
    assert res.walkforward == {}
    assert res.adversarial == {}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_trades():
    res = evaluate(Trades(rows=[]))
    assert res.passed is False
    assert res.n == 0
    assert "no trades" in res.reasons


def test_estimator_bias_flagged_when_price_mismatches_winrate():
    # win_rate ~0.95 but avg price paid 0.50 -> large gap -> check fails.
    trades = _build_trades(
        edge=0.03, n=240, seed=8, win_rate_matches_price=False
    )
    res = evaluate(trades)
    assert res.adversarial["estimator_bias_vs_unconditional"]["passed"] is False


def test_staleness_inconclusive_when_not_recorded():
    trades = _build_trades(edge=0.03, n=240, seed=9, stale_min=None)
    res = evaluate(trades)
    # No stale_min recorded -> passes as inconclusive.
    st = res.adversarial["staleness"]
    assert st["passed"] is True
    assert "not recorded" in st["detail"]


def test_staleness_fails_when_lines_stale():
    trades = _build_trades(edge=0.03, n=240, seed=10, stale_min=10.0)
    res = evaluate(trades)
    assert res.adversarial["staleness"]["passed"] is False


def test_missing_pnl_rows_dropped_when_no_fill_model():
    # NaN pnl with no sibling execution module / fill_model -> dropped.
    rows = []
    for i in range(10):
        rows.append(Trade(
            game_id=f"g{i}", date="2025-11-01", market="total",
            home_team="A", primary_team="B", side="over",
            entry_ts=0.0, exit_ts=1.0, entry_price=0.5, payoff=1.0,
            pnl=float("nan"),
        ))
    res = evaluate(Trades(rows=rows))
    # All dropped -> treated as no scorable trades.
    assert res.passed is False


def test_repriced_via_mock_fill_model():
    rows = []
    for i in range(4):
        rows.append(Trade(
            game_id=f"g{i}", date="2025-11-01", market="total",
            home_team="A", primary_team="B", side="over",
            entry_ts=0.0, exit_ts=1.0, entry_price=0.50, payoff=1.0,
            pnl=float("nan"),
        ))

    class MockFill:
        half_spread = 0.015

        def fee(self, price):
            return 0.01

    res = evaluate(Trades(rows=rows), fill_model=MockFill())
    # pnl = payoff(1.0) - (0.50 + 0.015 + 0.01) = 0.475 -> 47.5c; not dropped.
    assert res.n == 4
    assert res.cents_per_contract == pytest.approx(47.5, abs=1e-6)
