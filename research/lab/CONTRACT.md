# `research/lab/` interface contract (build against this; it is on the base branch)

All shared dataclasses live in `research.lab.types` (already merged). Implement
your unit's module with EXACTLY these public signatures so the parallel units
compose once merged. Your tests must pass using only `research.lab.types` +
synthetic fixtures + your own module (mock any sibling lab module you call).

Shared types (from `research.lab.types`): `Panel, Trade, Trades, FillResult,
GateResult, Hypothesis, synthetic_panel(...), synthetic_panels(...)`, and market
constants `WINNER, TOTAL, SPREAD`.

## lab/data.py  (Unit 1)
```
def load_panels(market: str, split: str | None = None, *,
                start: str = "2025-10-21", end: str = "2026-06-08",
                limit: int | None = None) -> list[Panel]
    # cache-only: read cached pkls via nba_odds_study; build one Panel per game.
    # Skip games whose cache is absent. `split` filters via market_data/splits.json
    # ("train"/"val"/"test"/"nontest"/None). NEVER load test unless explicitly asked.
def load_panel(game: dict, market: str) -> Panel | None
def available(market: str) -> int     # count of cached games for a market
```

## lab/execution.py  (Unit 2)
```
@dataclass
class FillModel:
    half_spread: float = 0.015           # prob units (1.5c)
    venue: str = "polymarket"            # fee schedule
    def fill(self, panel: Panel, ts: float, side: str) -> FillResult
        # snap to nearest LISTED strike in panel.ladder; fill at its REAL quoted
        # prob (interp at ts), NOT 0.50; add half_spread; add taker fee.
    def fee(self, price: float, size: float = 1.0) -> float   # PM 2% + curve / Kalshi
def cost_sweep(values=(0.0,0.01,0.015,0.02,0.025)) -> list[FillModel]
REALISTIC: FillModel  # module-level default instance
```

## lab/signals.py  (Unit 3) — pure functions over a Panel, return np.ndarray (len n)
```
def pace_projection(panel) -> np.ndarray          # total*2880/elapsed (TOTAL market)
def anchoring_gap(panel) -> np.ndarray            # projection - panel.mid
def implied_level(panel) -> np.ndarray            # = panel.mid (convenience)
def calibration_gap(panel, realized) -> np.ndarray
def staleness_min(panel) -> np.ndarray            # minutes since panel.mid last changed
def rolling(panel, key: str, window: int) -> np.ndarray
def zscore(x: np.ndarray, window: int) -> np.ndarray
```

## lab/strategy.py  (Unit 4)
```
@dataclass
class Strategy:
    name: str
    entry: Callable[[Panel], np.ndarray]   # bool mask over minutes; first True per game fires
    side:  Callable[[Panel, int], str]     # bar index -> side label ("over"/"long_home"/...)
    exit:  str | Callable = "settlement"   # "settlement" or a bar-selector
    entry_latency_min: float = 1.0         # honest i+1 fill (built in)
    max_stale_min: float = 2.0             # freshness guard (built in)
    min_elapsed: float = 600.0
    max_elapsed: float = 2520.0
    def run(self, panels: list[Panel], fill_model=None) -> Trades
        # one trade/game at first qualifying bar; enter at ts+latency via fill_model
        # (REALISTIC default); payoff = settlement vs the filled strike/side.
```

## lab/evaluate.py  (Unit 5)
```
def evaluate(trades: Trades, *, fill_model=None,
             cost_sweep=(0.0,0.01,0.02,0.03,0.04),
             walkforward: bool = True, adversarial: bool = True) -> GateResult
    # wraps research.scorer.promotion_gate.evaluate_trial; computes cost sweep,
    # monthly walk-forward, and the 4 adversarial checks
    # (staleness, estimator_bias_vs_unconditional, concentration, oos).
```

## lab/hypothesis.py  (Unit 6) — append-only JSONL registry
```
DEFAULT_PATH = "research/reports/alpha/hypotheses.jsonl"   # gitignored data path
def register(h: Hypothesis, path=DEFAULT_PATH) -> Hypothesis  # assigns id=h.hash(); dedupes
def claim(hyp_id: str, agent: str, path=...) -> Hypothesis     # status->running
def update(hyp_id: str, *, verdict=None, results=None, status=None, path=...) -> Hypothesis
def query(status=None, market=None, path=...) -> list[Hypothesis]
def open_hypotheses(path=...) -> list[Hypothesis]              # status=="open"
def seed_defaults(path=...) -> None    # optional: seed a few starter ideas if empty
```

## lab/session.py  (Unit 7) — the ergonomic façade
```
@dataclass
class Lab:
    fill_model: FillModel = REALISTIC
    def load(self, market, split=None, **kw) -> list[Panel]
    def strategy(self, name, entry, side, **kw) -> Strategy
    def evaluate(self, trades, **kw) -> GateResult
    def backtest(self, strategy, market, split="nontest") -> GateResult   # load+run+evaluate
    def register(self, hypothesis) -> Hypothesis
    def open_hypotheses(self) -> list[Hypothesis]
def lab() -> Lab    # default session
```

## lab/cli.py  (Unit 8)
```
python -m research.lab.cli backtest <strategy_module:STRATEGY> --market M --split nontest
python -m research.lab.cli hypotheses [list|new|claim <id>]
python -m research.lab.cli demo     # run a synthetic-fixture backtest end to end
# argparse entrypoint; importable main(argv) -> int
```

## lab/analyst.py  (Unit 9)
```
def run_analyst(brief: str | None = None, *, max_ideas=3, market=None, budget_aware=True) -> dict
    # pulls OPEN hypotheses from lab.hypothesis (or generates seeds), hands the agent
    # the toolkit, logs trials, updates verdicts. Budget-aware via agents.usage.limits.
    # (The actual idea-generation is the AGENT's; this is the harness/loop around it.)
```

## Units 10–15 (docs / migrations / rewire)
- 10 `agents/workers/analyst_prompt.md` — analyst brief pointing at this contract + README; grants autonomy.
- 11 rewire `agents/usage/edge_hunt_loop.py` + `agents/orchestrator/run.py` to pull directions from `lab.hypothesis` (replace hard-coded DIRECTIONS / DIRECTION_ROTATION).
- 12 `lab/strategies/anchoring.py` — totals + spreads anchoring as `Strategy`s (realistic exec default).
- 13 `lab/strategies/calibration.py` — calibration / favorite-longshot / totals-extremes as `Strategy`s.
- 14 `lab/strategies/reactions.py` — sub-latency / fair-value-gap / term-structure / CLV as `Strategy`s.
- 15 `lab/README.md` — "how to be an analyst here" platform guide.

## Hard rules (all units)
- NBA research only; never touch the arb bot (`kalshi_arbitrage/`, root `*.py`).
- Realistic execution is the DEFAULT — never assume a 0.50 fill (the artifact that
  falsely certified totals; see `research/TOTALS_REFINE_FINDINGS.md`).
- Never read the test split; never weaken the promotion gate.
- New files only, except Unit 11 (which edits the two named files). Match repo style.
