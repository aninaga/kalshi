"""Budget governor — portfolio allocation over two renewable budget streams.

DESIGN (operator-set, 2026-06-10)
---------------------------------
This is an allocation problem, not throttling. Two budgets (Claude-week,
Codex-week) drain to worthless at known resets; 5h windows are RATE limits on
top, not budgets. All consumption is converted to ONE CURRENCY — API-dollar
equivalents — under the operator's assumption that subscription window
consumption correlates with API pricing (Fable 2.0x Opus is published; Sonnet
0.2x, Haiku 0.04x from the API price sheet; codex credits at $/credit).

Principles:
  1. ONE CURRENCY  — unit costs are MEASURED (EWMA over meter deltas and
     lane runtimes), never assumed for long.
  2. SENIORITY     — the Claude week is shared: operator > apex coordination
     > Claude-only lanes > vendor-flexible work. The desk spends only the
     FORECAST SURPLUS: remaining − E[operator demand to reset] − apex reserve.
     Vendor-flexible work never touches Claude while codex has headroom.
  3. PACE-TO-RESET — codex has one claimant (the desk), so spend it to exhaust
     exactly at reset: concurrency = remaining-rate / measured per-agent rate.
  4. SHADOW PRICES — floors never degrade (defer or shift vendor instead);
     above floors the governor emits prices, not mandates, and the apex makes
     priced choices (e.g. Fable-vs-Opus apex hours).

Estimator honesty: meters are 1%-granular and attribution is inferential, so
everything runs through EWMAs with cold-start priors and hard clamps, with
`basis` fields saying which numbers are measured vs assumed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STATE_DIR = Path.home() / ".kalshi_fund"
HISTORY = STATE_DIR / "usage_history.jsonl"
LANES = STATE_DIR / "lane_history.jsonl"
CACHE = STATE_DIR / "usage_cache.json"
EST = STATE_DIR / "governor_estimates.json"   # EWMA persistence

# ---- currency ----------------------------------------------------------- #
# API $ / Mtok (out-weighted blend), normalized below; Fable 2x is published.
CLAUDE_MODEL_X = {"fable": 2.0, "opus": 1.0, "sonnet": 0.2, "haiku": 0.04}
CODEX_USD_PER_CREDIT = 0.008          # ~$8 per 1k credits (rate-card derived)

# cold-start priors (replaced by EWMA as data arrives)
PRIOR = {
    "claude_week_usd": 2000.0,        # $-equiv size of the Max20x week
    "codex_week_usd": 1500.0,         # $-equiv size of the Pro20x week
    "codex_agent_usd_hr": 0.9,        # measured tonight: ~$0.35/25min task
    "operator_usd_hr_active": 60.0,   # operator's claude.ai burn when active
    "apex_usd_hr": {"opus": 90.0, "fable": 180.0},
}
APEX_RESERVE_HR = 6.0                 # always keep >= this many Opus apex hours
LOCAL_MAX_CODEX = 12                  # 16GB ceiling
EWMA_A = 0.25

SEAT_FLOOR = {"apex": "opus", "judgment": "fable", "analyst": "opus",
              "scout": "sonnet", "ops": "haiku"}


def _rows(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text().splitlines():
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


def _cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def _est() -> dict:
    try:
        return json.loads(EST.read_text())
    except Exception:
        return {}


def _save_est(e: dict) -> None:
    tmp = EST.with_suffix(".tmp")
    tmp.write_text(json.dumps(e, indent=1))
    tmp.replace(EST)


def _ewma(key: str, observed: float | None, prior: float) -> float:
    e = _est()
    cur = e.get(key, prior)
    if observed is not None and observed > 0:
        cur = (1 - EWMA_A) * cur + EWMA_A * observed
        e[key] = cur
        e[f"{key}_n"] = e.get(f"{key}_n", 0) + 1
        _save_est(e)
    return cur


# ---- estimators ---------------------------------------------------------- #
def _week_usd(vendor: str) -> tuple[float, str]:
    """$-size of a vendor's weekly budget: d($-equiv burn)/d(weekly %) x 100.
    Claude $-burn comes from ccusage block history; codex from credit math."""
    snaps = [r for r in _rows(HISTORY) if r.get("ts", 0) > time.time() - 48 * 3600]
    key = "claude_wk" if vendor == "claude" else "codex_wk"
    obs = None
    if len(snaps) >= 2 and vendor == "claude":
        # pair window-% rises with measured block-$ rises in the same spans
        dusd = dpct = 0.0
        for a, b in zip(snaps, snaps[1:]):
            if None in (a.get(key), b.get(key), a.get("claude_block_usd"),
                        b.get("claude_block_usd")):
                continue
            dp = b[key] - a[key]
            du = b["claude_block_usd"] - a["claude_block_usd"]
            if dp > 0 and du > 0:
                dpct += dp
                dusd += du
        if dpct >= 3.0:
            obs = dusd / dpct * 100.0
    val = _ewma(f"{vendor}_week_usd", obs, PRIOR[f"{vendor}_week_usd"])
    n = _est().get(f"{vendor}_week_usd_n", 0)
    return val, ("measured" if n else "prior")


def _codex_agent_usd_hr() -> tuple[float, str]:
    cut = time.time() - 24 * 3600
    snaps = [r for r in _rows(HISTORY) if r.get("ts", 0) >= cut
             and r.get("codex_wk") is not None]
    obs = None
    if len(snaps) >= 2:
        dpct = snaps[-1]["codex_wk"] - snaps[0]["codex_wk"]
        agent_h = sum(r.get("runtime_s", 0) for r in _rows(LANES)
                      if r.get("vendor") == "codex"
                      and r.get("ts", 0) >= cut) / 3600.0
        if dpct > 0 and agent_h > 0.1:
            week_usd, _ = _week_usd("codex")
            obs = (dpct / 100.0) * week_usd / agent_h
    val = _ewma("codex_agent_usd_hr", obs, PRIOR["codex_agent_usd_hr"])
    n = _est().get("codex_agent_usd_hr_n", 0)
    return val, ("measured" if n else "prior")


def _operator_forecast_usd(hours_left: float) -> tuple[float, str]:
    """E[operator $-demand until weekly reset]: learned hourly activity
    profile x $/active-hour, realtime-floored when active now."""
    try:
        from ops import usage_profile
    except ModuleNotFoundError:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from ops import usage_profile
    week_usd, _ = _week_usd("claude")
    # expected active-hours: integrate the demand profile over hours to reset
    t = time.time()
    exp_active = 0.0
    for h in range(int(min(hours_left, 168))):
        exp_active += usage_profile.demand_now(t + h * 3600)["demand"]
    usd_hr = _ewma("operator_usd_hr_active", None,
                   PRIOR["operator_usd_hr_active"])
    fc = exp_active * usd_hr
    now = usage_profile.demand_now()
    if now.get("realtime_user_active"):
        fc = max(fc, 0.35 * week_usd * min(hours_left, 24) / 24)
    return fc, f"{exp_active:.0f} expected active-h x ${usd_hr:.0f}/h"


# ---- the policy ----------------------------------------------------------- #
def policy() -> dict:
    c = _cache()
    cdx, clp = c.get("codex") or {}, c.get("claude_plan") or {}
    now = time.time()

    # ---------- codex: sole claimant, pace to reset ----------
    wk = cdx.get("weekly_pct") or 0.0
    fh = cdx.get("five_hour_pct") or 0.0
    resets = cdx.get("weekly_resets_at")
    h_left = max((resets - now) / 3600.0, 1.0) if resets else 84.0
    week_usd, wk_basis = _week_usd("codex")
    agent_usd, ag_basis = _codex_agent_usd_hr()
    remaining_usd = (1 - wk / 100.0) * week_usd * 0.9   # keep 10% reserve
    target_rate = remaining_usd / h_left                # $/h to land at reset
    fleet = int(max(1, min(LOCAL_MAX_CODEX, round(target_rate / agent_usd))))
    rate_note = ""
    if fh >= 70.0:                                      # 5h speed limit
        fleet = max(1, fleet // 2)
        rate_note = f"; 5h at {fh:.0f}% -> halved"
    codex = {
        "fleet_size": fleet,
        "lane_concurrency": max(2, min(fleet, 6)),
        "weekly_pct": wk, "hours_to_reset": round(h_left, 1),
        "remaining_usd_equiv": round(remaining_usd),
        "target_usd_hr": round(target_rate, 1),
        "agent_usd_hr": round(agent_usd, 2),
        "basis": {"week_size": wk_basis, "agent_cost": ag_basis},
        "reason": (f"pace-to-reset: ${remaining_usd:.0f} / {h_left:.0f}h = "
                   f"${target_rate:.1f}/h at ${agent_usd:.2f}/agent-h"
                   f" -> {fleet} agents{rate_note}"),
    }

    # ---------- claude: shared budget, desk gets the surplus ----------
    cwk = clp.get("weekly_pct") or 0.0
    cfh = clp.get("five_hour_pct") or 0.0
    cresets = clp.get("weekly_resets_at")
    if isinstance(cresets, str):
        import datetime as dt
        ch_left = max((dt.datetime.fromisoformat(cresets).timestamp() - now)
                      / 3600.0, 1.0)
    else:
        ch_left = 84.0
    cweek_usd, cwk_basis = _week_usd("claude")
    cremaining = (1 - cwk / 100.0) * cweek_usd
    op_fc, op_basis = _operator_forecast_usd(ch_left)
    apex_usd = _est().get("apex_usd_hr", PRIOR["apex_usd_hr"])
    apex_reserve = APEX_RESERVE_HR * apex_usd["opus"]
    surplus = cremaining - op_fc - apex_reserve
    opus_lane_usd = 5.0                                  # ~measured tonight
    claude = {
        "weekly_pct": cwk, "five_hour_pct": cfh,
        "hours_to_reset": round(ch_left, 1),
        "remaining_usd_equiv": round(cremaining),
        "operator_forecast_usd": round(op_fc),
        "operator_forecast_basis": op_basis,
        "apex_reserve_usd": round(apex_reserve),
        "desk_surplus_usd": round(surplus),
        "desk_surplus_lanes": max(0, int(surplus / opus_lane_usd)),
        "desk_may_spend": surplus > 0 and cfh < 80.0,
        "shadow_prices_usd_hr": {
            "apex_opus": apex_usd["opus"],
            "apex_fable": apex_usd["fable"],
            "analyst_lane": opus_lane_usd,
        },
        "seat_floors": SEAT_FLOOR,
        "basis": {"week_size": cwk_basis},
        "reason": (f"${cremaining:.0f} left − ${op_fc:.0f} operator forecast − "
                   f"${apex_reserve:.0f} apex reserve = ${surplus:.0f} surplus"
                   + ("" if surplus > 0 else " — desk locked out of claude")),
    }

    return {"codex": codex, "claude": claude, "ts": round(now),
            "seniority": ["operator", "apex", "claude-only lanes",
                          "vendor-flexible work"],
            "currency": "API-$-equivalent (operator's pricing-correlation "
                        "assumption; Fable 2x published)"}


# back-compat shims for controller call sites
def codex_policy() -> dict:
    p = policy()["codex"]
    return {"codex_fleet_size": p["fleet_size"],
            "codex_lane_concurrency": p["lane_concurrency"], **p}


def claude_desk_allowed() -> bool:
    return policy()["claude"]["desk_may_spend"]


if __name__ == "__main__":
    print(json.dumps(policy(), indent=2))
