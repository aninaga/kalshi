"""ChatGPT-Pro / Codex usage-limit polling + a budget governor for autoresearch.

The user pays for a ChatGPT Pro (20x) account and drives GPT-5.5 through the
`codex exec` CLI. They previously built an `agent-limits` tool that reports live
quota usage as JSON. This module is the single, hardened place that:

  1. **Locates** the `agent-limits` binary (env var, PATH, or the known
     install path) so callers never hard-code it.
  2. **Polls** it (`agent-limits status --json`) and parses the nested codex /
     claude window shapes into a flat, typed `LimitsSnapshot`.
  3. **Governs** the autoresearch loop: given caps + reserves, decides how many
     GPT-5.5 workers it is safe to spawn this wave, and when to pause-until-reset.

The agent-limits JSON shape (verified against foreman.py, 2026-05):

    {"agents": {
        "codex":  {"limits": [{"id": "codex",
                     "windows": [{"name": "primary",   "used_percent": 41.0, "resets_at_epoch": ...},
                                 {"name": "secondary", "used_percent": 88.0, "resets_at_epoch": ...}]}]},
        "claude": {"limits": [{"name": "five_hour", "used_percent": 12.0}, ...]}}}

`primary` = rolling 5h window, `secondary` = rolling 7d window. A missing /
None `used_percent` means the upstream read was stale — we surface that as
`has_data=False` so the governor treats it as UNKNOWN (optimistic) rather than
wrongly assuming the bucket is exhausted (the model's own 429s are the backstop).

CLI::

    python -m research.agents.usage.limits            # human summary
    python -m research.agents.usage.limits --json      # machine snapshot
    python -m research.agents.usage.limits --plan 4    # governor decision for base=4
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# Known install path of the user's tool (from foreman.py). Env var wins; then
# PATH; then this fallback. Kept as a list so future machines can be added.
_KNOWN_PATHS = [
    Path.home() / "Documents/Codex/2026-05-28/can-you-build-some-tool-that/bin/agent-limits",
]
_ENV_VAR = "AGENT_LIMITS_BIN"

# Codex bucket id in the agent-limits JSON, and the model it maps to.
CODEX_BUCKET = "codex"          # gpt-5.5
GPT55_MODEL = "gpt-5.5"


def find_agent_limits() -> Optional[str]:
    """Locate the agent-limits binary. Returns an absolute path or None."""
    env = os.environ.get(_ENV_VAR)
    if env and Path(env).exists():
        return env
    on_path = shutil.which("agent-limits")
    if on_path:
        return on_path
    for p in _KNOWN_PATHS:
        if p.exists():
            return str(p)
    return None


@dataclass
class Window:
    used_percent: Optional[float]
    resets_at_epoch: Optional[float]

    @property
    def known(self) -> bool:
        return self.used_percent is not None


@dataclass
class LimitsSnapshot:
    """Typed view of one agent-limits read. All percents are 0..100."""
    available: bool                 # did the tool run and parse at all?
    gpt55_5h: Window
    gpt55_7d: Window
    claude_5h_used: Optional[float]
    raw: dict
    error: Optional[str] = None

    @property
    def gpt55_has_data(self) -> bool:
        return self.gpt55_5h.known or self.gpt55_7d.known

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "gpt55_5h_used": self.gpt55_5h.used_percent,
            "gpt55_7d_used": self.gpt55_7d.used_percent,
            "gpt55_7d_reset_epoch": self.gpt55_7d.resets_at_epoch,
            "claude_5h_used": self.claude_5h_used,
            "error": self.error,
        }


def _parse_codex(raw: dict) -> tuple[Window, Window]:
    five = Window(None, None)
    seven = Window(None, None)
    for b in (raw.get("agents", {}).get("codex", {}) or {}).get("limits", []) or []:
        if b.get("id") not in (CODEX_BUCKET, None):
            continue
        for w in b.get("windows", []) or []:
            up = w.get("used_percent")
            up = float(up) if up is not None else None
            if w.get("name") == "primary":
                five = Window(up, w.get("resets_at_epoch"))
            elif w.get("name") == "secondary":
                seven = Window(up, w.get("resets_at_epoch"))
    return five, seven


def _parse_claude_5h(raw: dict) -> Optional[float]:
    cl = raw.get("agents", {}).get("claude", {}) or {}
    for e in (cl.get("limits") or []):
        if not isinstance(e, dict):
            continue
        nm = e.get("name") or e.get("label")
        if nm in ("five_hour", "primary", "5h") and e.get("used_percent") is not None:
            return float(e["used_percent"])
        for w in (e.get("windows") or []):  # nested-shape fallback
            if (w.get("name") in ("five_hour", "primary", "5h")) and w.get("used_percent") is not None:
                return float(w["used_percent"])
    return None


def parse_snapshot(raw: dict) -> LimitsSnapshot:
    """Pure parser — unit-testable without the binary."""
    five, seven = _parse_codex(raw)
    return LimitsSnapshot(
        available=bool(raw),
        gpt55_5h=five, gpt55_7d=seven,
        claude_5h_used=_parse_claude_5h(raw),
        raw=raw,
    )


def poll(timeout_sec: int = 60) -> LimitsSnapshot:
    """Run agent-limits and parse. Never raises; returns available=False on error."""
    binp = find_agent_limits()
    if not binp:
        return LimitsSnapshot(False, Window(None, None), Window(None, None), None, {},
                              error=f"agent-limits not found (set ${_ENV_VAR} or add to PATH)")
    try:
        out = subprocess.run([binp, "status", "--json"], capture_output=True,
                             text=True, timeout=timeout_sec)
        raw = json.loads(out.stdout) if out.stdout.strip() else {}
        snap = parse_snapshot(raw)
        if not snap.available:
            snap.error = f"agent-limits returned empty/unparseable output (rc={out.returncode})"
        return snap
    except Exception as exc:  # noqa: BLE001
        return LimitsSnapshot(False, Window(None, None), Window(None, None), None, {},
                              error=f"agent-limits read failed: {exc}")


# --------------------------------------------------------------------------- #
# Budget governor
# --------------------------------------------------------------------------- #

@dataclass
class GovernorPolicy:
    weekly_cap_pct: float = 90.0      # don't push gpt-5.5 7d window past this
    five_hour_cap_pct: float = 88.0   # nor the 5h window past this
    soft_throttle_pct: float = 70.0   # above this on 7d, halve workers
    min_workers: int = 1
    max_workers: int = 4


@dataclass
class GovernorDecision:
    gpt55_workers: int
    reason: str
    sleep_until_epoch: Optional[float]   # set iff fully tapped → pause
    snapshot: dict


def decide_workers(snap: LimitsSnapshot, base_workers: int,
                   policy: GovernorPolicy = GovernorPolicy()) -> GovernorDecision:
    """How many GPT-5.5 workers is it safe to spawn this wave?

    Philosophy (matches foreman): UNKNOWN usage → optimistic (run base; the
    model's own 429s are the real backstop). KNOWN-and-over-cap → pause until
    the 7d window resets. KNOWN-and-high → soft-throttle to half.
    """
    base = max(0, min(base_workers, policy.max_workers))
    if not snap.available:
        return GovernorDecision(base, f"limits unavailable ({snap.error}) → optimistic base={base}",
                                None, snap.to_dict())
    if not snap.gpt55_has_data:
        return GovernorDecision(base, "gpt-5.5 usage unreadable (stale) → optimistic base", None,
                                snap.to_dict())

    w7d = snap.gpt55_7d.used_percent
    w5h = snap.gpt55_5h.used_percent
    over_7d = w7d is not None and w7d >= policy.weekly_cap_pct
    over_5h = w5h is not None and w5h >= policy.five_hour_cap_pct
    if over_7d or over_5h:
        which = f"7d={w7d:.0f}%" if over_7d else f"5h={w5h:.0f}%"
        reset = snap.gpt55_7d.resets_at_epoch or (time.time() + 1800)
        return GovernorDecision(0, f"gpt-5.5 tapped ({which} ≥ cap) → pause until reset",
                                reset, snap.to_dict())
    if w7d is not None and w7d >= policy.soft_throttle_pct:
        n = max(policy.min_workers, base // 2)
        return GovernorDecision(n, f"gpt-5.5 7d={w7d:.0f}% high → throttle to {n}", None,
                                snap.to_dict())
    return GovernorDecision(base, f"gpt-5.5 7d={w7d:.0f}% 5h={w5h:.0f}% → run base={base}", None,
                            snap.to_dict())


def _fmt(v) -> str:
    return f"{v:.0f}%" if isinstance(v, (int, float)) else "?"


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit the parsed snapshot as JSON")
    ap.add_argument("--plan", type=int, metavar="BASE", help="print governor decision for BASE workers")
    a = ap.parse_args(argv)
    snap = poll()
    if a.json:
        print(json.dumps(snap.to_dict(), indent=2)); return 0
    if not snap.available:
        print(f"agent-limits: UNAVAILABLE — {snap.error}", file=sys.stderr)
    else:
        print(f"GPT-5.5  5h={_fmt(snap.gpt55_5h.used_percent)}  "
              f"7d={_fmt(snap.gpt55_7d.used_percent)}   "
              f"Claude 5h={_fmt(snap.claude_5h_used)}")
    if a.plan is not None:
        d = decide_workers(snap, a.plan)
        print(f"plan(base={a.plan}): spawn {d.gpt55_workers} gpt-5.5 workers — {d.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
