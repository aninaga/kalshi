# meta / diagnostic + data-quality — run verdict

_UTC: 2026-06-09T08:54:29Z. Lane 5 (meta). Branch:
`claude/hedge-fund-strategy-analysis-jyXWB`. Agent: Opus 4.8, max effort.
No git commit/push (coordinator commits); no test split; gate not touched._

## Job 1 — LEAKAGE AUDIT (lab.audit across every shipped lab.signals function)

**Method.** Ran `lab.audit.audit_report` (dynamic no-lookahead perturbation test
+ static outcome-field source check) for every *live* signal in `lab.signals`,
on a real `train` panel from **each** market (winner n=181, total n=181, spread
n=181) and a synthetic adversarial fixture (n=120). Then re-ran the dynamic test
over **40 panels per market** for robustness. `calibration_gap(panel, realized)`
is correctly EXEMPT — it is a label-style API taking the realized outcome as an
explicit argument, not a one-arg live signal (per the auditor's documented
contract); `eda._residual_label` is likewise exempt offline label construction.

**Result — ALL PASS (no lookahead found).**

| signal | static source check | dynamic no-lookahead | per-market (40 panels ea.) |
|---|---|---|---|
| `pace_projection` | CLEAN | PASS (4 cuts) | winner/total/spread ALL PASS |
| `anchoring_gap` | CLEAN | PASS (4 cuts) | ALL PASS |
| `implied_level` | CLEAN | PASS (4 cuts) | ALL PASS |
| `staleness_min` | CLEAN | PASS (4 cuts) | ALL PASS |
| `rolling` (mid/total, w5/w10) | CLEAN | PASS (4 cuts) | ALL PASS |
| `zscore` (on mid / anchoring_gap) | CLEAN | PASS (4 cuts) | ALL PASS |
| `calibration_gap` | CLEAN | EXEMPT (label-style; not a live signal) | n/a |

- The only annotations on the dynamic report were informational
  `NOTE: source unavailable for '<lambda>'` notes on the lambda-wrapped
  `rolling`/`zscore` variants — that is the expected "could not statically
  verify a lambda; rely on the dynamic test" message, **not** a violation. The
  raw shipped functions all have inspectable source and are CLEAN statically.

**Auditor sanity check (confirmed it has teeth, no false-negatives):**
- A signal that reads `panel.final_total` flags on the **static** check and
  fails the **dynamic** check (passed=False). ✅
- A name-free dynamic leak (reversed cumsum — peeks at future bars with no
  outcome-field name) is caught by the **dynamic** perturbation test (passed=
  False) even though it passes the static grep. ✅

**Audit verdict: NO LOOKAHEAD in any shipped live signal across all three
markets. Nothing flagged. No false-positive on the legitimately-exempt label
constructors.**

## Job 2 — DIAGNOSTIC

**Fleet state at write time:** no `research/reports/alpha/ledger.jsonl` exists
yet and no peer `runs/<lane>_*.md` verdicts have landed (only README.md). The
diagnostic is therefore grounded in the durable historical record (ALPHA_FINDINGS,
DIAGNOSTIC_1, and the six FINDINGS docs). That record shows ≥3 consecutive
non-PROMOTE outcomes (0 PROMOTE; 1 PROMISING-uncertified spreads; 1 RETRACTED
totals-on-cost; rest DEAD) — the trigger condition is met.

**Written: `research/DIAGNOSTIC_2.md`.**

**Binding blocker (ranked):** **(b) the ~2–4¢ realistic cost floor × (c)
statistical power at one season of n**, compounding. NOT (d) a harness bug
(leakage audit clean; DIAGNOSTIC_1 cost/latency/split audit clean), NOT (e) gate
miscalibration in the false-positive direction (FP=0.0% at every n). (a) Market
efficiency correctly explains the dead *majority*; (b)×(c) explain why the one
real family (anchoring) still hasn't certified. Decisive new evidence: the
realistic-execution re-score flipped the certified totals edge from +6.21¢ PASS
(fabricated 0.50 fill) to −1.00¢ FAIL (real listed strike + half-spread + 2% PM
taker) — a *cost* error, not a leakage error.

**Recommendation: CONTINUE (scoped).** Change the *execution assumption* (test
**maker / limit fills** on the spreads+totals anchoring residual to avoid the
~3.4¢ taker round-trip), build the full-season SPREADS population (no direction
re-fit), mark the efficient families EXHAUSTED, and do NOT weaken the gate. A
PAUSE trigger is defined: if the anchoring family still nets ≤0¢ after maker-fill
re-scoring + full spread season, PAUSE (further darts only raise the DSR hurdle).

## Reproduce

```bash
python3 - <<'PY'
from research.lab import audit, signals, data
from research.lab.types import synthetic_panel
sig = {'pace_projection':signals.pace_projection,'anchoring_gap':signals.anchoring_gap,
       'implied_level':signals.implied_level,'staleness_min':signals.staleness_min,
       'rolling_mid5':lambda p:signals.rolling(p,'mid',5),
       'zscore_gap10':lambda p:signals.zscore(signals.anchoring_gap(p),10)}
for m in ('winner','total','spread'):
    for p in data.load_panels(m, split='train', limit=40):
        assert audit.audit_report(p, sig)['all_passed']
print('ALL PASS')
PY
```
