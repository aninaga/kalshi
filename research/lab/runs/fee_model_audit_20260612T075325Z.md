# fee_model_audit — 2026-06-12T07:53Z

**Lane**: fee_model_audit (AGENDA queued lane, executed by operator session)
**Verdict**: FEE-CORRECTED — no gate-verdict flips, but every PM-venue NBA
number was over-charged and the program's cost-floor rationale is restated.

## What was found

The repo carried **5+ divergent venue-fee implementations**. The research lab
(`research/lab/execution.py`, default for EVERY lab evaluation via
`REALISTIC`) charged a **flat 2% of notional on every Polymarket taker fill**
plus a legacy curve — a fee that **does not exist in the official schedule** —
with a docstring asserting omitting it "is exactly the error that falsely
certified totals." The arb line had already verified the official schedule on
2026-06-09 (`kalshi_arbitrage/mock_execution.py`) and removed the flat piece,
on a branch the research line never merged. The two sides of one repo
disagreed about the price of trading; the side that was wrong issued the
verdicts. Additionally `research/scripts/maker_fill_study.py` charged makers
either $0 (PM — correct) or the **full Kalshi taker fee** ("pessimistic"
contingency) — the real Kalshi NBA maker fee is a **quarter** of taker.

## Official schedules (sources + fetch dates)

- **Polymarket** (docs.polymarket.com/trading/fees, help.polymarket.com;
  2026-06-09 and 2026-06-12): taker = `shares x (bps/10000) x p x (1-p)`,
  per-category — geopolitics 0, **sports 300**, politics/finance/tech/
  mentions 400, econ/culture/weather/other 500, crypto 700. **Makers pay $0**
  and earn a 20–25% daily rebate of counterparty fees. Per-token live rates:
  `clob.polymarket.com/fee-rate`. Fee rollout is by market-creation date
  (sports markets created ≥ 2026-03-30) — the studied 2025-26 NBA season's PM
  markets were largely **fee-free in-period**; the forward-looking sports rate
  is what new verdicts should charge.
- **Kalshi** (kalshi.com/fee-schedule, help.kalshi.com; venue API
  `/trade-api/v2/series/<t>` fetched 2026-06-12): taker =
  `ceil_cents(0.07 x C x p x (1-p))`; maker = `ceil_cents(0.0175 x ...)` on
  `fee_type="quadratic_with_maker_fees"` series ONLY. **KXNBA: with-maker x1.0.
  KXBTCD / KXETHD / KXHIGHNY: plain quadratic (makers pay $0). KXINX: x0.5.**

## What changed in code

- NEW **`venue_fees.py`** (repo root, stdlib-only, shipped via py-modules):
  canonical schedules, per-series Kalshi params, legacy fee behind explicit
  `legacy_pm_taker_fee` / `FillModel(legacy_fees=True)`.
- Delegated: `kalshi_arbitrage/mock_execution.py::FeeModel`,
  `research/lab/execution.py::FillModel` (PM default now official **sports**),
  `research/harness/realistic_fills.py` (profile wrappers kept),
  `research/scripts/maker_fill_study.py` (official quarter-of-taker maker),
  `research/scripts/{totals,spread}_realistic.py` (+ `--legacy-fees` repro flag).
- **Golden tests**: `tests/test_venue_fees_golden.py` pins the published
  schedules (with sources/dates) + cross-implementation parity for every
  consumer. Lab fee tests rewritten to pin the official schedule.
- Repaired a silent merge break the audit surfaced: `nba_odds_study` was moved
  to `research/nba_odds_study` by the arb line (df4e093) while 17 research-line
  modules import the root name — root package is now a compatibility alias.

## Re-rate (analytical; exact — fee enters PnL additively)

Per-market NBA caches (`*_spread_all.pkl` / `*_total_all.pkl`) no longer exist
on disk (lane worktrees cleared), so end-to-end re-runs need a cache rebuild
(queued below). The fee delta is exact per trade regardless:
`delta(p) = legacy(p) − official_sports(p)`; means/CI endpoints shift by
`E[delta]` (±0.13c across the ATM entry band); **stability sub-gates
(season-split / parity CI overlap) are level-independent and unchanged.**

| memo | recorded | corrected | gate |
|---|---|---|---|
| Spreads anchoring TAKER (n=1107) | +0.77c, CI [−2.08, +3.60], OOS +1.27→+0.26 | **+1.92c**, CI [−0.93, +4.75], OOS +2.42→+1.41 (both halves +) | still FAIL (CI lo < 0; stability) |
| Spreads MAKER, PM (n=726) | +2.10c central / +2.13c best | unchanged — already official (PM maker $0; rebate uncredited) | still FAIL (CI/parity) |
| Spreads MAKER, Kalshi contingency | +0.12c (charged FULL taker ≈2.0c/lot) | **+1.10c** (1-lot, official 1c ceil) / **+1.66c** (batch, 0.44c) | still FAIL |
| Totals pace (RETRACTED, n=1107) | −1.00c, CI [−3.82, +1.90] | **+0.27c**, CI [−2.55, +3.17] | retraction STANDS; "execution-killed at −1c" softens to ~breakeven |
| Totals extremes (tails) | DEAD (also measurement artifact) | high-price legs were over-charged **9–22x** (1.96c vs 0.09–0.22c) | needs cache rebuild + true re-run |

**Not contaminated**: crypto + weather families (venue="kalshi" throughout —
their ledger rows record real `avg_fee` 1.0–1.8c), and both live paper books
(weather maker book registered `maker_fee_c: 0.0`, correct; crypto scan bills
real Kalshi taker).

## Restated cost floor (replaces PROGRAM_STATE §"cost floor")

- **Taker, PM sports**: 1.5c half-spread + 0.75c ATM fee ≈ **2.25c** all-in
  (was claimed ~3.3–3.4c). Tails: fee → ~0.1–0.2c (was ~2c).
- **Taker, Kalshi**: 1.5c + 1.75c ATM ≈ 3.25c (unchanged; real).
- **Maker fee floor ≈ 0**: PM $0 (+rebate); Kalshi NBA 0.44c batch / 1c 1-lot;
  Kalshi crypto/weather **$0** (plain-quadratic series). The binding maker
  costs are fill probability + adverse selection — exactly what the live fill
  probes measure — NOT fees.

## Follow-ups (queued)

1. **Cache rebuild + true re-run** of totals-extremes (and spreads/totals
   end-to-end confirmation of the analytical shift) — network job, hours;
   needs operator approval per data-quality discipline.
2. **Forward paper**: the spread maker residual (+2.10c PM / +1.66c Kalshi
   batch, OOS-stable both halves) remains REAL-but-uncertifiable at one
   season; paper-trading it frozen into 2026-27 is the standing capital/risk
   decision — now with the fee headwind roughly halved.
3. PM **historical-period fee-free** nuance: in-period 2025-26 NBA PM markets
   pre-date the sports fee rollout; if any forward expression trades PM
   markets created before 2026-03-30, charge their live per-token rate (often
   0) rather than the category rate.
