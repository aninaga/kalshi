"""totals_power_measure — statistical-POWER / efficiency measurement for the
NBA TOTAL market's pace-anchoring CONTINUATION residual (lane: total/efficiency).

Mirrors research/lab/runs/spreads_power_*.md for totals. NOT an origination: it
takes the known totals continuation anchoring residual AS-IS (the pre-registered
direction from ALPHA_FINDINGS / TOTALS_REFINE_FINDINGS: thresh=6, min_elapsed=600,
max_stale_min=2, continuation) at REALISTIC fills (lab.execution.REALISTIC) and:

  1. available-n table for the TOTAL market (cached, nontest, by month)
  2. honesty check: lab.audit no-lookahead leakage + estimator-bias-vs-unconditional
     (hunt the retracted 0.50-fill / look-ahead artifact specifically)
  3. maximal-nontest-population gate (cents/ct, bootstrap CI, cost sweep, OOS)
  4. power: required n for the bootstrap CI lower bound to clear 0 (bare + with the
     season/parity stability sub-gates), and whether that n is attainable.

No git, no test split, no direction re-fit, no new knobs, no 0.50 fill.
"""
from __future__ import annotations

import sys
from collections import Counter

import numpy as np

sys.path.insert(0, "/home/user/kalshi")

from research.lab import audit, data, evaluate
from research.lab.execution import REALISTIC
from research.lab.strategies.anchoring import totals_anchoring
from research.lab.types import TOTAL

LEDGER = "research/reports/alpha/ledger.jsonl"


def main() -> None:
    # ---- 1. available-n table (cache-only; no network) -------------------
    all_games = data._cached_games(TOTAL)
    predicate, split_of = data._split_filter(None)        # None => excludes test
    pred_nontest, _ = data._split_filter("nontest")
    pred_train, _ = data._split_filter("train")
    pred_val, _ = data._split_filter("val")
    pred_test, _ = data._split_filter("test")

    def gid(g):
        return data._gid_of(g)

    n_all = len(all_games)
    n_train = sum(1 for g in all_games if pred_train(gid(g)))
    n_val = sum(1 for g in all_games if pred_val(gid(g)))
    n_test = sum(1 for g in all_games if pred_test(gid(g)))
    n_nontest = sum(1 for g in all_games if pred_nontest(gid(g)))
    n_unknown = sum(1 for g in all_games if split_of(gid(g)) == "unknown"
                    and pred_nontest(gid(g)))

    nontest_games = [g for g in all_games if pred_nontest(gid(g))]
    by_month = Counter(g["date"][:7] for g in nontest_games)

    print("=" * 70)
    print("1. AVAILABLE-N (TOTAL market, cache-only, no network)")
    print("=" * 70)
    print(f"cached total pkls (all splits):            {n_all}")
    print(f"  train:                                   {n_train}")
    print(f"  val:                                     {n_val}")
    print(f"  test (EXCLUDED, never loaded):           {n_test}")
    print(f"  unknown (pre-split-lock, kept nontest):  {n_unknown}")
    print(f"  NONTEST (train+val+unknown):             {n_nontest}")
    print("nontest cached pkls by month:")
    for m in sorted(by_month):
        print(f"    {m}: {by_month[m]}")

    # ---- build the maximal nontest panel population (once) ---------------
    panels = data.load_panels(TOTAL, split="nontest")
    n_panels = len(panels)
    print(f"nontest pkls that build a usable Panel:      {n_panels}")

    # ---- 2a. leakage audit (lab.audit) on the AS-IS strategy -------------
    print()
    print("=" * 70)
    print("2a. LEAKAGE AUDIT (lab.audit no-lookahead) on AS-IS totals strategy")
    print("=" * 70)
    strat = totals_anchoring(thresh=6, min_elapsed=600.0, max_elapsed=2520.0,
                             max_stale_min=2.0, continuation=True)
    # static source check of the live callables (entry + side closures)
    src_entry = audit.audit_callable_source(strat.entry)
    src_side = audit.audit_callable_source(strat.side)
    print(f"static source check entry: {src_entry or 'CLEAN (no outcome-field refs)'}")
    print(f"static source check side : {src_side or 'CLEAN (no outcome-field refs)'}")
    # dynamic no-lookahead perturbation test on a sample of real panels
    sample = panels[:: max(1, n_panels // 40)][:40]
    dyn_fail = 0
    example = None
    for p in sample:
        # audit_signal_no_lookahead returns a dict {passed, violations, ...}
        r = audit.audit_signal_no_lookahead(strat.entry, p)
        if not r["passed"]:
            dyn_fail += 1
            if example is None and r["violations"]:
                example = r["violations"][0]
    print(f"dynamic no-lookahead perturbation (entry mask) on {len(sample)} "
          f"real panels: {'CLEAN' if dyn_fail == 0 else f'{dyn_fail} LEAKED'}")
    if example:
        print(f"  example violation: {example}")
    # also audit the underlying live signals individually
    from research.lab import signals as _sig
    rep = audit.audit_report(sample[0], {
        "pace_projection": _sig.pace_projection,
        "anchoring_gap": _sig.anchoring_gap,
        "staleness_min": _sig.staleness_min,
        "implied_level": _sig.implied_level,
    })
    print(f"  underlying signals all_passed={rep['all_passed']} "
          f"(failed={rep['failed_signals']})")

    # ---- run the AS-IS strategy with REALISTIC fills ---------------------
    trades = strat.run(panels, fill_model=REALISTIC)
    df = trades.df()
    n_tr = len(df)
    print(f"\nAS-IS realistic trades fired (one/qualifying game): {n_tr}")

    pnl = df["pnl"].to_numpy(float)
    payoff = df["payoff"].to_numpy(float)
    entry_price = df["entry_price"].to_numpy(float)
    fin = np.isfinite(pnl) & np.isfinite(payoff) & np.isfinite(entry_price)
    pnl, payoff, entry_price = pnl[fin], payoff[fin], entry_price[fin]
    n_eff = len(pnl)

    win_rate = float((payoff > 0.5).mean())
    avg_fill = float(entry_price.mean())
    mean_pnl_c = float(pnl.mean() * 100.0)
    std_pnl = float(pnl.std(ddof=1))
    gross_payoff_minus_mid = float((payoff - entry_price).mean() * 100.0)

    # ---- 2b. estimator-bias-vs-unconditional ----------------------------
    print()
    print("=" * 70)
    print("2b. ESTIMATOR-BIAS-VS-UNCONDITIONAL (honesty: fair strike snap?)")
    print("=" * 70)
    print(f"win rate (payoff>0.5):              {win_rate:.4f}")
    print(f"avg realistic fill mid paid:        {avg_fill:.4f}")
    print(f"|win - fill| estimator-bias gap:    {abs(win_rate - avg_fill):.4f}")
    print(f"gross payoff-mid (true tradeable):  {gross_payoff_minus_mid:+.2f}c")
    print(f"realistic net mean pnl:             {mean_pnl_c:+.2f}c/ct")
    print(f"per-trade std (hold-to-settle 0/1): {std_pnl:.4f}")

    # ---- 2 (gate). maximal-population gate via lab.evaluate --------------
    print()
    print("=" * 70)
    print("2(gate). MAXIMAL-POPULATION GATE (lab.evaluate, ledger-aware DSR)")
    print("=" * 70)
    gr = evaluate.evaluate(trades, ledger_path=LEDGER,
                           cost_sweep=(0.0, 0.01, 0.015, 0.02, 0.025))
    print(f"n={gr.n} n_games={gr.n_games} passed={gr.passed}")
    print(f"cents/ct={gr.cents_per_contract:+.2f}  "
          f"CI=[{gr.ci_lo:+.2f}, {gr.ci_hi:+.2f}]")
    print(f"reasons: {gr.reasons}")
    print("cost sweep (extra cents):")
    for c, v in sorted(gr.cost_sweep.items()):
        print(f"    +{c:>5}c: net={v['cents']:+.2f}c  CIlo={v['ci_lo']:+.2f}  "
              f"gate={v['gate']}")
    print("adversarial:")
    for k, v in gr.adversarial.items():
        print(f"    {k}: passed={v['passed']}  {v['detail']}")
    print("monthly walk-forward:")
    for m, v in sorted(gr.walkforward.items()):
        print(f"    {m}: n={v['n']:>4} net={v['cents']:+.2f}c CIlo={v['ci_lo']:+.2f} "
              f"gate={v['gate']}")

    # ---- 3. power / required-n estimate ----------------------------------
    print()
    print("=" * 70)
    print("3. POWER / REQUIRED-N (CI lower bound > 0)")
    print("=" * 70)
    mu = float(pnl.mean())
    sigma = float(pnl.std(ddof=1))
    snr = mu / sigma if sigma else float("nan")
    print(f"mu (per-trade prob)={mu:.5f}  sigma={sigma:.4f}  SNR=mu/sigma={snr:.4f}")
    half_w_c = float(gr.cents_per_contract - gr.ci_lo)   # bootstrap half-width, cents
    print(f"observed bootstrap CI half-width @ n={n_eff}: {half_w_c:.2f}c")
    # min detectable edge at current n (bare 95%): the half-width
    print(f"min detectable edge at n={n_eff} (bare 95% CI): +/-{half_w_c:.2f}c")
    if mu > 0:
        n_bare = (1.96 * sigma / mu) ** 2
        n_subgate = 2.0 * n_bare    # season-half x parity ~ each half needs sig => ~2x
        print(f"required n bare (CI lo>0): n > (1.96*sigma/mu)^2 = {n_bare:,.0f}")
        print(f"required n with season/parity sub-gates (~2x): {n_subgate:,.0f}")
    else:
        print("mu <= 0: NO finite n closes the CI above 0 (edge non-positive net).")
        # report breakeven gross needed
        print("the realistic NET edge is non-positive => not a power problem, "
              "an efficiency/cost problem.")
    # zero-half-spread (fee-only) variant — the most-optimistic still-honest
    # fill (TOTALS_REFINE +0.53c). Gives a required-n even when central nets <0.
    from research.lab.execution import FillModel
    fee_only = FillModel(half_spread=0.0, venue=REALISTIC.venue)
    tr0 = strat.run(panels, fill_model=fee_only)
    p0 = tr0.df()["pnl"].to_numpy(float)
    p0 = p0[np.isfinite(p0)]
    mu0, sig0 = float(p0.mean()), float(p0.std(ddof=1))
    print(f"\n[zero-half-spread / fee-only variant] net={mu0 * 100:+.2f}c "
          f"sigma={sig0:.4f} n={len(p0)}")
    if mu0 > 0:
        n0 = (1.96 * sig0 / mu0) ** 2
        print(f"  required n bare (fee-only): {n0:,.0f}; with sub-gates ~2x: "
              f"{2 * n0:,.0f}")
    else:
        print("  fee-only net also <= 0: no finite n.")

    print(f"\nentire 2025-26 nontest season trades available: {n_eff}")
    print(f"all cached total games incl. test:               {n_all}")
    print("(Kalshi/PM NBA markets launched 2025-26: NO prior seasons exist.)")


if __name__ == "__main__":
    main()
