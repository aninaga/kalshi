"""research.lab.types — the shared contract every lab module builds against.

This module is intentionally dependency-light (stdlib + numpy + pandas) so it
imports cleanly in any isolated worktree. Every other ``research/lab/*`` module
imports its dataclasses from here; nothing here imports the rest of the lab.

The substrate's data flow is:

    Panel(s)  --signals-->  features  --Strategy-->  Trades  --evaluate-->  GateResult
                              ^                          ^
                          (lab.signals)            (lab.execution FillModel)

See ``research/lab/CONTRACT.md`` for the full per-module public API.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

# Market identifiers used across the lab.
WINNER = "winner"   # contract: P(home wins); mid = implied home win prob in [0,1]
TOTAL = "total"     # contract: P(final total > strike); mid = implied total points
SPREAD = "spread"   # contract: P(home margin > strike); mid = implied home margin
MARKETS = (WINNER, TOTAL, SPREAD)


@dataclass
class Panel:
    """One game's per-minute frame for ONE market.

    Arrays are all length ``n`` and index-aligned by minute. ``mid`` is the
    market's implied 0.5-crossing level (win-prob for winner; points for
    total; signed margin for spread). ``ladder`` maps each listed strike to its
    per-minute P(over/cover) array (empty for the winner market, which has no
    strike ladder). Realistic execution reads the ladder; never assume 0.50.
    """
    game_id: str
    date: str
    market: str
    home_team: str
    away_team: str
    minute_ts: np.ndarray            # wall-clock minute timestamps (epoch sec)
    elapsed_sec: np.ndarray          # game-elapsed seconds (0..~2880)
    margin: np.ndarray               # home_score - away_score
    total: np.ndarray                # home_score + away_score
    mid: np.ndarray                  # implied 0.5-level for this market
    ladder: dict = field(default_factory=dict)     # strike(float) -> prob np.ndarray
    features: dict = field(default_factory=dict)    # name -> np.ndarray (len n)
    home_won: Optional[float] = None               # 1.0 / 0.0 / None
    final_total: Optional[float] = None
    final_margin: Optional[float] = None
    split: str = "unknown"           # "train" | "val" | "test" | "unknown"
    # Trading-relevant event duration in seconds. Defaults to NBA regulation
    # (2880s) so legacy panels keep their numerics; providers for other event
    # classes set their own. ``None`` means UNTIMED (no in-event clock — e.g. a
    # CPI print): pace-style signals emit NaN and never fire.
    duration_sec: Optional[float] = 2880.0

    @property
    def n(self) -> int:
        return int(len(self.elapsed_sec))

    def df(self) -> pd.DataFrame:
        cols = {"minute_ts": self.minute_ts, "elapsed_sec": self.elapsed_sec,
                "margin": self.margin, "total": self.total, "mid": self.mid}
        cols.update({f"feat_{k}": v for k, v in self.features.items()})
        return pd.DataFrame(cols)


@dataclass
class Trade:
    """One realized trade. ``entry_price`` is the REAL fill mid (never assume
    0.50); ``payoff`` is the settlement value in [0,1] (or the exit price for
    non-hold-to-settlement). ``pnl`` is net of costs once execution is applied."""
    game_id: str
    date: str
    market: str
    home_team: str
    primary_team: str                # the side actually bet (concentration/parity)
    side: str                        # human label: "over"/"under"/"long_home"/...
    entry_ts: float
    exit_ts: float
    entry_price: float
    payoff: float
    pnl: float = float("nan")
    entry_strike: Optional[float] = None
    meta: dict = field(default_factory=dict)


@dataclass
class Trades:
    rows: list                       # list[Trade]

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def empty(self) -> bool:
        return len(self.rows) == 0

    def df(self) -> pd.DataFrame:
        if not self.rows:
            return pd.DataFrame(columns=[
                "game_id", "date", "market", "home_team", "primary_team", "side",
                "entry_ts", "exit_ts", "entry_price", "payoff", "pnl", "entry_strike"])
        return pd.DataFrame([{
            "game_id": t.game_id, "date": t.date, "market": t.market,
            "home_team": t.home_team, "primary_team": t.primary_team, "side": t.side,
            "entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "entry_price": t.entry_price,
            "payoff": t.payoff, "pnl": t.pnl, "entry_strike": t.entry_strike,
        } for t in self.rows])


@dataclass
class FillResult:
    """Realistic-execution output for one entry (lab.execution)."""
    strike: float
    fill_mid: float                  # the REAL quoted price you pay, not 0.50
    half_spread: float
    fee: float

    @property
    def all_in_price(self) -> float:
        """Price actually paid to take the position (mid + half-spread + fee)."""
        return self.fill_mid + self.half_spread + self.fee


@dataclass
class GateResult:
    """One-call evaluation output (lab.evaluate)."""
    passed: bool
    cents_per_contract: float
    ci_lo: float
    ci_hi: float
    n: int
    n_games: int
    reasons: list = field(default_factory=list)
    walkforward: dict = field(default_factory=dict)   # month -> {n, cents, ci_lo}
    adversarial: dict = field(default_factory=dict)    # check -> {passed, detail}
    cost_sweep: dict = field(default_factory=dict)     # cost_c -> {cents, ci_lo, gate}
    governance: dict = field(default_factory=dict)     # DSR N / V[SR] + provenance sources


@dataclass
class Hypothesis:
    """A research hypothesis in the dynamic registry (lab.hypothesis)."""
    market: str
    mechanism: str                   # one-paragraph WHY it should persist
    signal_desc: str                 # the observable signal
    direction: str                   # pre-registered direction (1-bit)
    id: str = ""
    status: str = "open"             # open | running | done
    verdict: Optional[str] = None    # PROMOTE | PROMISING | NEEDS_DATA | DEAD
    results: dict = field(default_factory=dict)
    created: str = ""
    updated: str = ""

    def hash(self) -> str:
        """Stable dedupe key from the idea's identity (not its status/results)."""
        key = f"{self.market}|{self.mechanism.strip().lower()}|{self.direction.strip().lower()}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


def synthetic_panel(game_id: str = "syn", market: str = TOTAL, n: int = 48,
                    seed: int = 0, *, signal: float = 0.0, home_won: Optional[float] = None,
                    split: str = "train", start_ts: float = 1_700_000_000.0) -> Panel:
    """Deterministic synthetic Panel for tests/fixtures (no real data needed).

    ``signal`` injects a known, tradeable mispricing: the implied ``mid`` is
    biased AGAINST the eventual outcome by ``signal`` (in mid-units), so a
    strategy that bets toward the outcome has a known positive edge of ~``signal``
    at zero cost. ``signal=0`` is a calibrated null. This lets downstream units
    test known-signal / known-null behavior reproducibly.
    """
    rng = np.random.default_rng(seed)
    elapsed = np.linspace(0.0, 2820.0, n)
    minute_ts = start_ts + (elapsed / 60.0).astype(int) * 60.0
    # score paths
    total_path = np.clip(np.cumsum(np.abs(rng.normal(5, 2, n))), 0, None)
    margin_path = np.cumsum(rng.normal(0, 3, n))
    final_total = float(total_path[-1])
    final_margin = float(margin_path[-1])
    hw = float(final_margin > 0) if home_won is None else float(home_won)

    if market == WINNER:
        true = hw
        mid = np.clip(0.5 + 0.5 * margin_path / (np.abs(margin_path).max() + 1e-9), 0.02, 0.98)
        mid = mid - signal * (2 * true - 1)        # biased against outcome by `signal`
        ladder: dict = {}
    elif market == TOTAL:
        true = final_total
        mid = total_path * 2820.0 / np.clip(elapsed, 60, None)   # naive pace proj as a stand-in level
        mid = mid - signal * np.sign(final_total - mid + 1e-9) * 10.0
        strikes = np.round(np.linspace(final_total - 20, final_total + 20, 9))
        ladder = {float(s): np.clip(0.5 + 0.02 * (mid - s), 0.01, 0.99) for s in strikes}
    else:  # SPREAD
        true = final_margin
        mid = margin_path.copy()
        mid = mid - signal * np.sign(final_margin - mid + 1e-9) * 5.0
        strikes = np.round(np.linspace(final_margin - 12, final_margin + 12, 9))
        ladder = {float(s): np.clip(0.5 + 0.03 * (mid - s), 0.01, 0.99) for s in strikes}

    features = {
        "pace_ppm": total_path / np.clip(elapsed / 60.0, 1, None),
        "stale_min": np.zeros(n),
        "lead_changes": np.cumsum((np.diff(np.sign(margin_path), prepend=0) != 0).astype(float)),
    }
    return Panel(
        game_id=game_id, date="2025-11-01", market=market,
        home_team="HOM", away_team="AWY",
        minute_ts=minute_ts, elapsed_sec=elapsed, margin=margin_path, total=total_path,
        mid=np.asarray(mid, float), ladder=ladder, features=features,
        home_won=hw, final_total=final_total, final_margin=final_margin, split=split,
    )


def synthetic_panels(n_games: int = 40, market: str = TOTAL, seed: int = 0,
                     signal: float = 0.0, split: str = "train") -> list:
    """A list of synthetic Panels (distinct game_ids) for evaluator-level tests."""
    out = []
    for i in range(n_games):
        out.append(synthetic_panel(game_id=f"syn_{i}", market=market, seed=seed + i,
                                   signal=signal, split=split))
    return out
