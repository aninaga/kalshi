# Model bake-off — Opus 4.8 vs Sonnet 4.6 vs Fable 5 as analyst agents

_Date: 2026-06-09. One identical assignment per model (originate + falsify up
to two pre-registered hypotheses on the weather family), identical prompts,
shared registry with claims, rubric fixed before launch (discipline / rigor /
throughput / verifiability). n=1 trial per model on one family — a structured
observation, not a benchmark._

## Results

| | Opus 4.8 | Sonnet 4.6 | Fable 5 |
|---|---|---|---|
| Wall clock | 31 min | 135 min | 36 min |
| Tokens / tool calls | 83k / 39 | 153k / 118 | 95k / 34 |
| Verdicts | 1 DEAD + reasoned decline | 2 DEAD | 2 DEAD |
| Run files | `opus_weather_195944Z` | `sonnet_weather_214358Z` | `fable_weather_200558Z/200559Z` |
| Standout | Deepest decomposition: fade edge is real (+3.04¢ gross) and exactly cost-eaten (3.24¢); declined hypothesis 2 because the wings aren't tradeable through the ATM snap — judgment over throughput | Broadest EDA artifact catalog: degenerate open book, late-day staleness artifact, explicitly REFUSED a look-ahead cross-day signal | Found a real substrate hazard (snap-flip: FillModel re-snaps at i+1; 22.6% of unguarded band fills executed inverted exposure), guarded it non-look-ahead, re-scored; independently replicated Opus's fade numbers to the cent |
| Wobble | none observed | registered H2 against its own EDA (window already shown calibrated) — a predictably-dead dart that raised the family N; 4× the wall clock of the others | none observed |

All six verdicts agree with each other and with the family's structural story
(gross inefficiencies 1–3¢, all-in taker costs ~2.5–3.2¢). No model
manufactured a positive; the gate + pre-registration discipline held under
all three — the harness is model-robust.

## Read

- **Fable 5: best information per minute.** Two clean verdicts, the only
  substrate bug find, and an independent replication, at Opus-class speed.
  Default for standing analyst lanes.
- **Opus 4.8: best judgment per decision.** The principled decline and the
  exact cost-attribution are director/adversary-grade reasoning. Use for
  red-team, consolidation, and data-agent roles where one decision matters
  more than coverage.
- **Sonnet 4.6: honest but expensive here.** Real EDA contributions and a
  genuinely clean H2 falsification, at 4× wall clock with one wasted dart.
  Adequate for EDA/data-quality sweeps; weaker as a hypothesis economist.

## Action items raised

1. **Substrate fix (confirmed by code read):** allow a strategy to PIN the
   strike its signal evaluated (optional, default = current re-snap) so
   band-conditioned strategies can't execute inverted exposure
   (`research/lab/execution.py` FillModel.fill).
2. Steering note for future weather lanes: within-event microstructure is
   closed (6 mechanisms, all DEAD vs the ~3¢ cost floor). Next information
   has to be exogenous (the `forecast_high_f` feature, in flight).
