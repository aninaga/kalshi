"""Real usage meters for both model vendors.

codex  — OpenAI writes true rate-limit snapshots (used_percent for the 5h and
         weekly windows, reset timestamps) into every session rollout file.
         We read the newest one. This is the vendor's own number, not an estimate.

claude — Anthropic exposes no plan-percent locally; the honest measurable is
         per-5h-block token/cost data parsed from ~/.claude transcripts via
         ccusage (API-equivalent $ as burn proxy + tokens by model).

Both are slow/IO-ish, so the controller refreshes them on a timer into a cache
file; the HTTP handler only ever reads the cache.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

CODEX_SESSIONS = Path.home() / ".codex/sessions"
CACHE = Path.home() / ".kalshi_fund/usage_cache.json"
NODE_BIN = Path.home() / ".nvm/versions/node/v20.19.2/bin"


def codex_limits() -> dict:
    """Newest rate_limits snapshot across recent codex session files."""
    files = sorted(CODEX_SESSIONS.glob("*/*/*/*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    for f in files:
        best = None
        try:
            with open(f, "rb") as fh:
                for raw in fh:
                    if b'"rate_limits"' in raw:
                        best = raw
        except OSError:
            continue
        if not best:
            continue
        try:
            obj = json.loads(best)
        except Exception:
            continue
        # rate_limits nests somewhere in the event payload; walk for it
        def find(o):
            if isinstance(o, dict):
                if "primary" in o and "secondary" in o and "used_percent" in str(o):
                    return o
                for v in o.values():
                    r = find(v)
                    if r:
                        return r
            elif isinstance(o, list):
                for v in o:
                    r = find(v)
                    if r:
                        return r
            return None
        rl = find(obj)
        if rl:
            pri, sec = rl.get("primary") or {}, rl.get("secondary") or {}
            return {
                "source": f.name,
                "five_hour_pct": pri.get("used_percent"),
                "five_hour_resets_at": pri.get("resets_at"),
                "weekly_pct": sec.get("used_percent"),
                "weekly_resets_at": sec.get("resets_at"),
            }
    return {"error": "no rate_limits snapshot found"}


def claude_block() -> dict:
    """Active 5h block measured from local transcripts (ccusage)."""
    try:
        out = subprocess.run(
            ["npx", "-y", "ccusage@latest", "blocks", "--json"],
            capture_output=True, text=True, timeout=180,
            env={"PATH": f"{NODE_BIN}:/usr/local/bin:/usr/bin:/bin"},
        ).stdout
        blocks = json.loads(out).get("blocks", [])
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)[:120]}
    active = [b for b in blocks if b.get("isActive")]
    if not active:
        return {"active": False}
    b = active[0]
    toks = b.get("tokenCounts", {})
    return {
        "active": True,
        "block_started": b.get("startTime"),
        "block_ends": b.get("endTime"),
        "models": b.get("models"),
        "input_tokens": toks.get("inputTokens"),
        "output_tokens": toks.get("outputTokens"),
        "cache_read": toks.get("cacheReadInputTokens"),
        "api_equiv_usd": round(b.get("costUSD") or 0, 2),
        "burn_usd_per_hr": round((b.get("burnRate") or {}).get("costPerHour") or 0, 2)
        if isinstance(b.get("burnRate"), dict) else None,
    }


def claude_limits() -> dict:
    """Anthropic's own plan meter — the same numbers /usage shows in-app.

    Reads the Claude Code OAuth token from the macOS Keychain (stays local;
    the only request goes to api.anthropic.com).
    """
    import urllib.request
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s",
             "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10).stdout
        tok = json.loads(raw)
        tok = (tok.get("claudeAiOauth") or tok)["accessToken"]
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {tok}",
                     "anthropic-beta": "oauth-2025-04-20"})
        d = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)[:120]}
    out = {}
    for k, label in (("five_hour", "five_hour"), ("seven_day", "weekly"),
                     ("seven_day_opus", "weekly_opus"),
                     ("seven_day_sonnet", "weekly_sonnet")):
        v = d.get(k)
        if isinstance(v, dict):
            out[f"{label}_pct"] = v.get("utilization")
            out[f"{label}_resets_at"] = v.get("resets_at")
    return out


def refresh_cache() -> dict:
    data = {"codex": codex_limits(), "claude": claude_block(),
            "claude_plan": claude_limits(), "ts": time.time()}
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(CACHE)
    return data


def read_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            pass
    return {"codex": {}, "claude": {}, "ts": None}


if __name__ == "__main__":
    print(json.dumps(refresh_cache(), indent=2))
