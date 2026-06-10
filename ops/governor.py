"""Throughput governor — ramp consumption until the meters push back.

Principle: an unused rate-limit window expires worthless. If the trailing-24h
peak of a vendor's 5h window never approaches its defer cap, the desk is
under-consuming paid capacity and should scale UP (bigger fleets, more
concurrent lanes). If deferrals start happening, scale back down. The meters
(vendor ground truth, polled every 10 min into usage_history.jsonl) close the
loop — no static fleet sizes anywhere.

The apex consults codex_policy() when sizing scout/verify fleets; the
controller consults it for per-vendor lane concurrency. Local resource cap:
codex agents are network-bound (~100-200MB each), so a 16GB machine tops out
around 12 concurrent before memory pressure, regardless of budget headroom.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

HISTORY = Path.home() / ".kalshi_fund/usage_history.jsonl"
LOCAL_MAX_CODEX = 12          # M1 Pro 16GB resource ceiling
CODEX_DEFER_5H = 85.0         # mirror of controller DEFER_PCT


def _trailing(hours: float = 24.0) -> list:
    if not HISTORY.exists():
        return []
    cut = time.time() - hours * 3600
    rows = []
    for raw in HISTORY.read_text().splitlines():
        try:
            r = json.loads(raw)
            if r.get("ts", 0) >= cut:
                rows.append(r)
        except Exception:
            continue
    return rows


def codex_policy() -> dict:
    """Recommended codex fleet size + lane concurrency from observed headroom.

    Ratchet: scale with how far the trailing-24h PEAK stayed below the defer
    cap. Peak (not mean) — bursts are what hit limits. Few samples -> stay
    conservative (cold start).
    """
    rows = _trailing(24.0)
    peaks = [r["codex_5h"] for r in rows if r.get("codex_5h") is not None]
    n = len(peaks)
    peak = max(peaks) if peaks else 0.0
    wk = max((r.get("codex_wk") or 0.0) for r in rows) if rows else 0.0

    if n < 6:                       # <1h of data: conservative default
        fleet, why = 6, f"cold start ({n} samples)"
    elif wk >= 80.0:
        fleet, why = 2, f"weekly at {wk:.0f}% — conserve"
    elif peak < 10.0:
        fleet, why = LOCAL_MAX_CODEX, f"24h peak {peak:.0f}% — far under cap, max ramp"
    elif peak < 25.0:
        fleet, why = 10, f"24h peak {peak:.0f}% — ramping"
    elif peak < 50.0:
        fleet, why = 8, f"24h peak {peak:.0f}% — healthy headroom"
    elif peak < 70.0:
        fleet, why = 5, f"24h peak {peak:.0f}% — nearing cap"
    else:
        fleet, why = 3, f"24h peak {peak:.0f}% — close to defer cap, easing"

    fleet = min(fleet, LOCAL_MAX_CODEX)
    return {"codex_fleet_size": fleet,
            "codex_lane_concurrency": max(2, fleet // 2),
            "trailing_24h_peak_5h_pct": round(peak, 1),
            "trailing_24h_peak_wk_pct": round(wk, 1),
            "samples": n, "reason": why}


if __name__ == "__main__":
    print(json.dumps(codex_policy(), indent=2))
