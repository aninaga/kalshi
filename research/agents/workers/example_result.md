# Trial result

## Spec
- spec_name: p4_halftime_2to3_v1
- spec_hash: 7e3a9b2c44d1f5e6a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1
- features: margin, pm_implied_wp
- side: long_trailing

## Backtest
- trial_id: 142
- n_trades: 87
- pnl_net: 23.45
- sharpe_net: 0.32
- win_rate: 0.54
- gate_passed: false
- gate_reasons: ci_lo_below_zero, dsr_marginal

## Mechanism
Halftime adjustments produce mean-reverting flow as bettors overreact to first-half scoring noise; tightening the deficit band to 2-3 points drops the 1-point noise cell while keeping the bounce signal.

## Status
completed_ok

## Machine-readable summary
```json
{
  "spec_hash": "7e3a9b2c44d1f5e6a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1",
  "trial_id": 142,
  "gate_passed": false,
  "status": "completed_ok",
  "n_trades": 87,
  "pnl_net": 23.45,
  "sharpe_net": 0.32,
  "win_rate": 0.54,
  "gate_reasons": ["ci_lo_below_zero", "dsr_marginal"]
}
```
