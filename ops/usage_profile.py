"""Learned user-demand profile — the desk yields to the human.

The operator uses the Claude budget personally throughout the day (claude.ai
and Claude Code share the same plan windows), more in the evening, less during
work hours. Background lanes should soak up budget the user won't use (an
unused 5h window expires worthless) and get out of the way when the user is
typically — or actually — active.

Two signals, blended:

1. LEARNED hourly profile: usage_history.jsonl rows where claude_5h ROSE while
   no desk lane was running (lane_history.jsonl) are attributed to the USER.
   Aggregated by hour-of-week into a demand score 0..1. Needs days of data to
   mean anything, so it blends in proportionally to sample size.

2. SEED schedule (operator-stated, used while the profile is cold): weekday
   work hours light, evenings heavy, overnight ~zero, weekends moderate-high.

Plus a REAL-TIME override: if the latest snapshot shows the Claude window
rising >2.5pts in 10 min with no desk lane running, the user is active RIGHT
NOW -> demand floors at 0.9 for the next 45 minutes regardless of profile.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STATE_DIR = Path.home() / ".kalshi_fund"
HISTORY = STATE_DIR / "usage_history.jsonl"
LANES = STATE_DIR / "lane_history.jsonl"

# hour-of-day -> seed demand, weekday (Mon-Fri)
_SEED_WEEKDAY = {**{h: 0.05 for h in range(0, 7)},   # asleep
                 7: 0.5, 8: 0.6,                      # morning use
                 **{h: 0.2 for h in range(9, 17)},    # at work — light
                 17: 0.6, 18: 0.8, 19: 0.8, 20: 0.9, 21: 0.9, 22: 0.8, 23: 0.4}
_SEED_WEEKEND = {**{h: 0.05 for h in range(0, 8)},
                 **{h: 0.6 for h in range(8, 11)},
                 **{h: 0.7 for h in range(11, 24)}}


def _seed(hour: int, weekday: int) -> float:
    return (_SEED_WEEKDAY if weekday < 5 else _SEED_WEEKEND).get(hour, 0.4)


def _learned() -> dict:
    """hour-of-week -> (user_attributed_rises, samples). Cheap full scan; the
    files are ~144 and ~tens of rows/day."""
    lanes = []
    if LANES.exists():
        for raw in LANES.read_text().splitlines():
            try:
                r = json.loads(raw)
                lanes.append((r["ts"] - r.get("runtime_s", 0), r["ts"]))
            except Exception:
                continue

    def desk_active(t0, t1):
        return any(a <= t1 and b >= t0 for a, b in lanes)

    out = {}
    prev = None
    if HISTORY.exists():
        for raw in HISTORY.read_text().splitlines():
            try:
                r = json.loads(raw)
            except Exception:
                continue
            if prev and r.get("claude_5h") is not None \
                    and prev.get("claude_5h") is not None \
                    and r["ts"] - prev["ts"] < 1200:
                rise = r["claude_5h"] - prev["claude_5h"]
                lt = time.localtime(prev["ts"])
                key = lt.tm_wday * 24 + lt.tm_hour
                hits, n = out.get(key, (0, 0))
                user_rise = rise > 1.0 and not desk_active(prev["ts"], r["ts"])
                out[key] = (hits + (1 if user_rise else 0), n + 1)
            prev = r
    return out


def demand_now(now: float | None = None) -> dict:
    """Blended user-demand score for the current hour + realtime override."""
    t = time.localtime(now or time.time())
    key = t.tm_wday * 24 + t.tm_hour
    seed = _seed(t.tm_hour, t.tm_wday)

    learned = _learned()
    hits, n = learned.get(key, (0, 0))
    blend_w = min(n / 12.0, 1.0)          # ~12 snapshots/hour-of-week ≈ 3 weeks
    learned_score = (hits / n) if n else 0.0
    demand = (1 - blend_w) * seed + blend_w * learned_score

    # realtime override from the last two snapshots
    realtime = False
    if HISTORY.exists():
        rows = HISTORY.read_text().splitlines()[-3:]
        snaps = []
        for raw in rows:
            try:
                snaps.append(json.loads(raw))
            except Exception:
                pass
        if len(snaps) >= 2:
            a, b = snaps[-2], snaps[-1]
            if (b.get("claude_5h") is not None and a.get("claude_5h") is not None
                    and time.time() - b["ts"] < 45 * 60
                    and b["claude_5h"] - a["claude_5h"] > 2.5):
                lanes_recent = False
                if LANES.exists():
                    for raw in LANES.read_text().splitlines()[-10:]:
                        try:
                            r = json.loads(raw)
                            if r["ts"] >= a["ts"]:
                                lanes_recent = True
                        except Exception:
                            pass
                if not lanes_recent:
                    realtime = True
                    demand = max(demand, 0.9)

    return {"demand": round(demand, 2), "seed": seed,
            "learned_score": round(learned_score, 2), "samples": n,
            "blend_weight": round(blend_w, 2), "realtime_user_active": realtime}


def claude_defer_thresholds(now: float | None = None) -> dict:
    """Dynamic apex-reserve: high demand -> big reserve (defer early); low
    demand -> soak the window (an unused 5h allocation expires worthless).
    Weekly stays conservative — it is the scarce, slow-recovering resource."""
    d = demand_now(now)
    five_hour = max(40.0, min(85.0, 85.0 - 45.0 * d["demand"]))
    weekly = max(70.0, min(85.0, 85.0 - 15.0 * d["demand"]))
    return {"five_hour": round(five_hour, 1), "weekly": round(weekly, 1), **d}


if __name__ == "__main__":
    print(json.dumps(claude_defer_thresholds(), indent=2))
