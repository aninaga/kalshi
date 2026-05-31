# Research — automated hedge-fund attempt

This directory is a **separate project** from the Kalshi–Polymarket arbitrage
bot (which lives in the top-level `kalshi_arbitrage/` package). It is the NBA
quantitative-research / strategy-backtesting effort.

**Separation rule:** the arb bot (`kalshi_arbitrage/`) must never import from
`research/`. This directory may use shared market-data utilities but stands on
its own.

## Layout

| Path | Purpose |
|------|---------|
| `nba_odds_study/` | NBA data ingestion + analysis package (uses relative imports internally) |
| `harness/` | Strategy backtester — replay, realistic fills, cost profiles |
| `scorer/` · `promotion/` | Statistical gates (bootstrap CIs) and promotion pipeline |
| `registry/` · `lake/` | Result registry (SQLite) and historical data lake (parquet/DuckDB) |
| `agents/` | Research worker pool |
| `scripts/` | Study CLIs |

> Arb-bot *validation* tooling (matching backtest, paper analysis, live-pilot
> readiness) is **not** here — it lives with the bot under
> `kalshi_arbitrage/validation/`.

## Running the study scripts

The scripts import `research.nba_odds_study`, so run them as modules from the
repository root:

```bash
python -m research.scripts.analyze_nba_game
python -m research.scripts.study_players
python -m research.scripts.study_substitutions
```
