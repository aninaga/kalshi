"""fee_audit_rerate — exact analytical re-rate of NBA verdicts onto official fees.

WHY ANALYTICAL: the per-market NBA evaluation caches (*_spread_all.pkl /
*_total_all.pkl) no longer exist on disk (they lived in since-cleared lane
worktrees), so the evaluators cannot re-run end-to-end without a multi-hour
network rebuild. But the fee enters each trade's PnL ADDITIVELY:

    pnl_i = payoff_i - fill_mid_i - half_spread - fee(all_in_i)

so switching the fee schedule shifts each trade's PnL by exactly

    delta(p_i) = legacy_pm_taker_fee(all_in_i) - official_fee(all_in_i)  >= 0

and the memo-recorded mean and bootstrap CI endpoints shift by E[delta(p)]
(the CI WIDTH is unchanged up to the tiny variance of delta over the entry
band — reported below as a sensitivity range). Stability/parity sub-gate
failures are level-independent and unchanged.

Verified memo inputs (see each memo for provenance):
- spreads taker:  research/lab/runs/exec_makerfill_20260609T091134Z.md
                  n=1107, net +0.77c, CI [-2.08, +3.60], OOS +1.27 -> +0.26;
                  research/lab/runs/spread_adversary_20260609T085637Z.md
                  (avg price paid 0.520; lab bills fee at all_in = fill + 1.5c).
- spreads maker:  same memo; PM rows already charge $0 maker (officially
                  correct; 20-25% rebate not credited). "Kalshi fee" row
                  charged the FULL taker fee per 1-lot (ceil -> ~2c) and
                  collapsed +2.10c -> +0.12c. Official NBA maker
                  (quadratic_with_maker_fees) = 0.0175 factor.
- totals pace:    research/TOTALS_REFINE_FINDINGS.md
                  n=1107, central -1.00c, CI [-3.82, +1.90], real listed
                  strike quoted ~0.556 -> all_in ~0.571.
- totals extremes: research/TOTALS_EXTREMES_FINDINGS.md — band p<0.10 /
                  p>0.90; both legs transact at the HIGH price side.

Run:  python3 -m research.scripts.fee_audit_rerate
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import venue_fees as vf  # noqa: E402

C = 100.0  # report fees in cents/contract


def legacy_c(p: float) -> float:
    return vf.legacy_pm_taker_fee(p, 1.0) * C


def official_sports_c(p: float) -> float:
    return vf.pm_taker_fee(p, 1.0, category="sports") * C


def delta_c(p: float) -> float:
    return legacy_c(p) - official_sports_c(p)


def kalshi_maker_1lot_c(p: float) -> float:
    return vf.kalshi_maker_fee(p, 1.0) * C


def kalshi_maker_batch_c(p: float) -> float:
    # amortized per-contract at batch size 100 (the ceil is batch-level)
    return vf.kalshi_maker_fee(p, 100.0) * C / 100.0


def main() -> int:
    rows: list[str] = []

    def emit(line: str) -> None:
        print(line)
        rows.append(line)

    emit("FEE AUDIT RE-RATE (analytical, exact per-trade fee delta)")
    emit("official PM sports = 300bps parabolic; legacy = flat 2% notional + old curve")
    emit("")

    # ---- delta(p) over the ATM entry band (sensitivity) -------------------
    band = [0.48, 0.50, 0.52, 0.5366, 0.55, 0.571, 0.60]
    emit("delta(p) = legacy - official_sports, cents/contract:")
    for p in band:
        emit(f"  all_in={p:0.4f}:  legacy {legacy_c(p):5.2f}c  "
             f"official {official_sports_c(p):5.2f}c  delta {delta_c(p):+5.2f}c")
    emit("")

    # ---- 1. Spreads anchoring TAKER ---------------------------------------
    # all_in = avg fill 0.5216 + 1.5c half-spread (lab bills fee at all_in)
    all_in = 0.5216 + 0.015
    d = delta_c(all_in)
    lo, hi = delta_c(0.50), delta_c(0.58)   # entry-band sensitivity
    emit("1) SPREADS anchoring TAKER (n=1107, memo exec_makerfill/spread_adversary)")
    emit(f"   recorded: net +0.77c, CI [-2.08, +3.60], OOS +1.27 -> +0.26 (FAIL)")
    emit(f"   fee delta at all_in={all_in:0.4f}: {d:+0.2f}c  (band {lo:+0.2f}..{hi:+0.2f})")
    emit(f"   corrected: net {0.77 + d:+0.2f}c, CI [{-2.08 + d:+0.2f}, {3.60 + d:+0.2f}],"
         f" OOS {1.27 + d:+0.2f} -> {0.26 + d:+0.2f}")
    emit("   gate: still FAIL (CI lo < 0; stability sub-gates level-independent)")
    emit("")

    # ---- 2. Spreads anchoring MAKER ----------------------------------------
    limit = 0.5216 - 0.015   # posts 1.5c inside the quote
    legacy_maker_1lot = vf.kalshi_taker_fee(limit, 1.0) * C   # what the memo charged
    off_1lot = kalshi_maker_1lot_c(limit)
    off_batch = kalshi_maker_batch_c(limit)
    emit("2) SPREADS anchoring MAKER (n=726 fills, same memo)")
    emit("   PM rows (+2.10c central / +2.13c best) ALREADY official: PM maker = $0")
    emit("   (rebate 20-25% of counterparty fee additionally uncredited).")
    emit(f"   'Kalshi fee' contingency charged FULL taker at 1-lot:"
         f" {legacy_maker_1lot:0.2f}c -> net +0.12c")
    emit(f"   official NBA maker (quadratic_with_maker_fees, 0.0175):"
         f" 1-lot {off_1lot:0.2f}c -> net {2.10 - off_1lot:+0.2f}c;"
         f" batch-amortized {off_batch:0.2f}c -> net {2.10 - off_batch:+0.2f}c")
    emit("   gate: still FAIL on by-game CI / parity at n=726 (level-independent)")
    emit("")

    # ---- 3. Totals pace RETRACTION ------------------------------------------
    all_in_t = 0.556 + 0.015
    dt = delta_c(all_in_t)
    emit("3) TOTALS pace (RETRACTED memo, n=1107, central half-spread 1.5c)")
    emit(f"   recorded: net -1.00c, CI [-3.82, +1.90] (FAIL at every spread)")
    emit(f"   fee delta at all_in={all_in_t:0.3f}: {dt:+0.2f}c")
    emit(f"   corrected: net {-1.00 + dt:+0.2f}c, CI [{-3.82 + dt:+0.2f}, {1.90 + dt:+0.2f}]")
    emit("   verdict: retraction STANDS (no positive edge) but the 'execution-"
         "killed at -1c' framing softens to ~breakeven at honest costs.")
    emit("")

    # ---- 4. Totals extremes (tail family) ----------------------------------
    emit("4) TOTALS extremes (tail family, band p<0.10 / p>0.90)")
    for p in (0.92, 0.95, 0.97):
        emit(f"   high-price leg all_in={p:0.2f}: legacy {legacy_c(p):0.2f}c vs"
             f" official {official_sports_c(p):0.2f}c  (delta {delta_c(p):+0.2f}c,"
             f" {legacy_c(p)/max(official_sports_c(p),1e-9):0.1f}x overcharge)")
    p = 0.05
    emit(f"   low-price leg all_in={p:0.2f}: legacy {legacy_c(p):0.2f}c vs"
         f" official {official_sports_c(p):0.2f}c (legacy slightly UNDER-charged)")
    emit("   verdict: family needs cache rebuild + true re-run before any claim —")
    emit("   the original DEAD also cited a measurement artifact, which fees do not cure.")
    emit("")

    emit("NOTE: Kalshi-venue families (crypto, weather) charged real Kalshi taker")
    emit("fees throughout — their verdicts are NOT fee-contaminated. Their Kalshi")
    emit("series are plain 'quadratic' (taker-only): future MAKER variants pay $0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
