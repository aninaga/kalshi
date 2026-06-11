"""Fund controller daemon — the single always-on process of the desk.

Runs the localhost dashboard (http://localhost:8777) and owns every fund
process: analyst lanes (codex exec / scripts), nightly data top-ups, and the
caffeinate sleep-blocker. One state machine, two buttons:

    RUN    -> caffeinate on, queue drains (max N concurrent lanes), top-ups fire
    PAUSE  -> SIGTERM every child, caffeinate released, laptop can sleep

Design rules:
  * Models are cloud-side; this box only conducts. Memory budget ~tens of MB.
  * Controller manages ONLY processes it spawned (never adopts strangers).
  * Lanes are idempotent by program convention; killing one loses at most its
    in-flight run, never committed state.
  * State lives OUTSIDE iCloud-synced paths (~/.kalshi_fund), code in the repo.

Run:  python3 ops/controller.py            (foreground; launchd keeps it alive)
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ops import usage_meter  # noqa: E402
STATE_DIR = Path.home() / ".kalshi_fund"
LANE_DIR = STATE_DIR / "lanes"
STATE_PATH = STATE_DIR / "state.json"
QUEUE_PATH = STATE_DIR / "queue.json"
LOG_PATH = STATE_DIR / "controller.log"
LEDGER = REPO / "research/reports/alpha/ledger.jsonl"
WEATHER_CACHE = REPO / "market_data/weather/_cache"
CRYPTO_CACHE = REPO / "market_data/crypto/_cache"
PORT = 8777
MAX_CONCURRENT_LANES = 2
TOPUP_HOUR_LOCAL = 3          # nightly data top-up at 03:00
PYTHON = sys.executable

# Machine-resident apex beat: a headless orchestration session every 5h that
# survives chat-session death (the 2026-06-10 failure mode: session-only cron
# meant a full day of no grading/launching). Spawned like any child — PAUSE
# kills it, the meters bill it, the beat log records it.
APEX_BEAT = (18000, "apex_beat",
             f"/Users/anirudh/.local/bin/claude -p \"$(cat {REPO}/ops/apex_beat_prompt.md)\" "
             "--dangerously-skip-permissions --model opus")

# Recurring paper-trading cadence (only while RUNNING; all passes idempotent).
PAPER_JOBS = [
    (3600, "paper_vintages", f"{PYTHON} -m research.scripts.update_forecast_vintages"),
    (900, "paper_open", f"{PYTHON} -m research.lab.paper --open --book weather_maker_v1 --strategy forecast_gap_maker"),
    (900, "paper_mark", f"{PYTHON} -m research.lab.paper --mark --book weather_maker_v1"),
    (3600, "paper_settle", f"{PYTHON} -m research.lab.paper --settle --book weather_maker_v1"),
]

TOPUP_CMDS = [
    f"{PYTHON} -m research.lab.providers.weather --build --cities NYC,CHI,MIA,AUS,DEN --max-events 250",
    f"{PYTHON} -m research.lab.providers.crypto --build --assets BTC --max-events 800 --stride 2 --shard 0 --nshards 1",
    f"{PYTHON} -m research.lab.providers.crypto --build --assets ETH --max-events 200 --stride 8 --shard 0 --nshards 1",
]

_lock = threading.RLock()


def log(msg: str) -> None:
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def _default_state() -> dict:
    return {"mode": "paused", "since": time.time(), "caffeinate_pid": None,
            "children": {}, "last_topup": None}


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return _default_state()


def save_state(st: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE_PATH)


def load_queue() -> list:
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text())
        except Exception:
            pass
    return []


def save_queue(q: list) -> None:
    tmp = QUEUE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1))
    tmp.replace(QUEUE_PATH)


STATE = load_state()


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Process management (children only)
# --------------------------------------------------------------------------- #
def spawn(lane_id: str, cmd: str, cwd: str | None = None, kind: str = "lane") -> int:
    LANE_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LANE_DIR / f"{lane_id}.log", "ab")
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd or str(REPO),
                            stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True)
    with _lock:
        STATE["children"][lane_id] = {
            "pid": proc.pid, "cmd": cmd, "kind": kind,
            "started": time.time(), "cwd": cwd or str(REPO)}
        save_state(STATE)
    log(f"spawned {kind} {lane_id} pid={proc.pid}")
    return proc.pid


def terminate_children() -> int:
    n = 0
    with _lock:
        for lane_id, ch in list(STATE["children"].items()):
            if _alive(ch["pid"]):
                try:
                    os.killpg(os.getpgid(ch["pid"]), signal.SIGTERM)
                    n += 1
                except OSError:
                    pass
            STATE["children"].pop(lane_id, None)
        save_state(STATE)
    return n


def reap() -> None:
    """Remove finished children from the registry; mark queue items done."""
    with _lock:
        q = load_queue()
        changed = False
        for lane_id, ch in list(STATE["children"].items()):
            if not _alive(ch["pid"]):
                STATE["children"].pop(lane_id)
                for item in q:
                    if item["id"] == lane_id and item.get("status") == "running":
                        item["status"] = "done"
                        item["finished"] = time.time()
                        changed = True
                with open(STATE_DIR / "lane_history.jsonl", "a") as fh:
                    fh.write(json.dumps({
                        "ts": round(time.time()), "id": lane_id,
                        "kind": ch["kind"], "vendor": ch.get("vendor", "none"),
                        "runtime_s": round(time.time() - ch["started"]),
                        "cmd": ch["cmd"][:160]}) + "\n")
                log(f"reaped {lane_id}")
        if changed:
            save_queue(q)
        save_state(STATE)


# --------------------------------------------------------------------------- #
# RUN / PAUSE
# --------------------------------------------------------------------------- #
def set_running() -> None:
    with _lock:
        if STATE["mode"] == "running":
            return
        STATE["mode"] = "running"
        STATE["since"] = time.time()
        if not _alive(STATE.get("caffeinate_pid")):
            p = subprocess.Popen(["caffeinate", "-dims"], start_new_session=True)
            STATE["caffeinate_pid"] = p.pid
        save_state(STATE)
    log("mode -> RUNNING")


def set_paused() -> None:
    with _lock:
        STATE["mode"] = "paused"
        STATE["since"] = time.time()
        n = 0
        cp = STATE.get("caffeinate_pid")
        if _alive(cp):
            try:
                os.kill(int(cp), signal.SIGTERM)
            except OSError:
                pass
        STATE["caffeinate_pid"] = None
        save_state(STATE)
        # mark running queue items back to queued so they relaunch on RUN
        q = load_queue()
        for item in q:
            if item.get("status") == "running":
                item["status"] = "queued"
        save_queue(q)
    n = terminate_children()
    # also sweep apex-launched codex agents (quarantines under /tmp) so PAUSE
    # really means "safe to close the laptop"
    n_ext = 0
    for key, ch in _external_codex().items():
        if str(ch.get("cwd", "")).startswith(("/tmp/codex", "/private/tmp/codex")):
            try:
                os.killpg(os.getpgid(ch["pid"]), signal.SIGTERM)
                n_ext += 1
            except OSError:
                try:
                    os.kill(ch["pid"], signal.SIGTERM)
                    n_ext += 1
                except OSError:
                    pass
    log(f"mode -> PAUSED (terminated {n} children, {n_ext} apex codex agents)")


# --------------------------------------------------------------------------- #
# Scheduler loop
# --------------------------------------------------------------------------- #
_usage_next = [0.0]


def scheduler() -> None:
    while True:
        try:
            reap()
            if time.time() >= _usage_next[0]:
                _usage_next[0] = time.time() + 600
                threading.Thread(target=usage_meter.refresh_cache,
                                 daemon=True).start()
            _track_externals()
            _sweep_zombies()
            with _lock:
                running_mode = STATE["mode"] == "running"
            if running_mode:
                _maybe_launch_lanes()
                _maybe_topup()
                _maybe_paper()
        except Exception as exc:  # noqa: BLE001
            log(f"scheduler error: {exc!r}")
        time.sleep(10)


# Cross-vendor load policy. Same-tier balancing, never quality degradation:
# when one vendor's window runs hot, work shifts to (or waits for) the other.
# Claude thresholds are deliberately LOWER — an APEX RESERVE: background lanes
# must leave headroom so the operator/apex session can always coordinate.
# Codex thresholds are high because nothing else competes for that budget.
# Fable counts 2x against the Claude window (promo through 2026-06-22), which
# is why Claude depletes fastest and why the reserve exists.
from ops import usage_profile  # noqa: E402

# Codex static; Claude thresholds are DYNAMIC — usage_profile learns the
# operator's personal usage rhythm (claude.ai + Claude Code share windows)
# and shrinks/grows the apex reserve accordingly: soak the window when the
# operator is typically away, yield hard when they are (or go) active.
DEFER_PCT = {
    "codex": {"five_hour": 85.0, "weekly": 90.0},
}


def _defer_table(vendor: str) -> dict:
    if vendor == "claude":
        t = usage_profile.claude_defer_thresholds()
        return {"five_hour": t["five_hour"], "weekly": t["weekly"]}
    return DEFER_PCT.get(vendor, {})


def _vendor_pct(vendor: str) -> dict:
    u = usage_meter.read_cache()
    src = u.get("codex") if vendor == "codex" else u.get("claude_plan")
    return src if isinstance(src, dict) else {}


def _vendor_throttled(vendor: str) -> str | None:
    """Return a reason string if this vendor's budget disallows a launch.
    Claude: seniority/surplus decision from the governor (operator > apex >
    desk). Codex: 5h/weekly static caps (pacing handles the rest)."""
    if vendor == "claude":
        from ops import governor
        cl = governor.policy()["claude"]
        if not cl["desk_may_spend"]:
            return f"claude: no desk surplus ({cl['reason']})"
        return None
    table = _defer_table(vendor)
    if not table:
        return None
    src = _vendor_pct(vendor)
    for win, cap in table.items():
        pct = src.get(f"{win}_pct")
        if pct is not None and pct >= cap:
            return f"{vendor} {win} at {pct:.0f}% >= {cap:.0f}% cap (defer)"
    return None


def pick_vendor() -> str:
    """For vendor-agnostic work: route to the cooler vendor (headroom-weighted,
    Claude's dynamic apex reserve already priced into its thresholds)."""
    head = {}
    for v in ("codex", "claude"):
        if _vendor_throttled(v):
            head[v] = -1.0
            continue
        src = _vendor_pct(v)
        table = _defer_table(v)
        head[v] = min(cap - (src.get(f"{w}_pct") or 0.0)
                      for w, cap in table.items())
    return max(head, key=head.get)


def _maybe_launch_lanes() -> None:
    from ops import governor
    codex_cap = governor.codex_policy()["codex_lane_concurrency"]
    with _lock:
        active_by_vendor = {}
        for c in STATE["children"].values():
            if c["kind"] == "lane" and _alive(c["pid"]):
                v = c.get("vendor", "none")
                active_by_vendor[v] = active_by_vendor.get(v, 0) + 1
        total = sum(active_by_vendor.values())
        if total >= MAX_CONCURRENT_LANES + codex_cap:
            return
        q = load_queue()
        for item in q:
            if item.get("status") == "queued" and item.get("enabled", True):
                vend = item.get("vendor", "none")
                cap = codex_cap if vend == "codex" else MAX_CONCURRENT_LANES
                if active_by_vendor.get(vend, 0) >= cap:
                    continue
                reason = _vendor_throttled(vend)
                if reason:
                    if item.get("deferred_reason") != reason:
                        item["deferred_reason"] = reason
                        save_queue(q)
                        log(f"lane {item['id']} deferred: {reason}")
                    continue
                item.pop("deferred_reason", None)
                item["status"] = "running"
                item["started"] = time.time()
                save_queue(q)
                spawn(item["id"], item["cmd"], cwd=item.get("cwd"), kind="lane")
                return


_ext_seen: dict = {}


def _track_externals() -> None:
    """Log apex-launched codex agents into lane_history when they finish, so
    the governor can measure weekly-budget burn per codex agent-hour."""
    now_ext = _external_codex()
    for key, ch in now_ext.items():
        _ext_seen.setdefault(key, ch["started"])
    for key, started in list(_ext_seen.items()):
        if key not in now_ext:
            with open(STATE_DIR / "lane_history.jsonl", "a") as fh:
                fh.write(json.dumps({
                    "ts": round(time.time()), "id": key, "kind": "codex-apex",
                    "vendor": "codex",
                    "runtime_s": round(time.time() - started)}) + "\n")
            _ext_seen.pop(key)


def _sweep_zombies() -> None:
    """Dead-agent detection: codex agents running >2h are hung — kill + log."""
    for key, ch in _external_codex().items():
        if ch["elapsed_s"] > 7200:
            try:
                os.killpg(os.getpgid(ch["pid"]), signal.SIGTERM)
            except OSError:
                try:
                    os.kill(ch["pid"], signal.SIGTERM)
                except OSError:
                    continue
            log(f"zombie swept: {key} after {ch['elapsed_s'] // 60}m")


def _maybe_paper() -> None:
    now = time.time()
    with _lock:
        rec = STATE.setdefault("recurring", {})
        for interval, jid, cmd in PAPER_JOBS + [APEX_BEAT]:
            if now - rec.get(jid, 0) < interval:
                continue
            if any(k == jid and _alive(c["pid"])
                   for k, c in STATE["children"].items()):
                continue
            rec[jid] = now
            save_state(STATE)
            spawn(jid, cmd, kind="paper")


def _maybe_topup() -> None:
    now = dt.datetime.now()
    last = STATE.get("last_topup")
    today_3am = now.replace(hour=TOPUP_HOUR_LOCAL, minute=0, second=0)
    due = now >= today_3am and (not last or last < today_3am.timestamp())
    if not due:
        return
    with _lock:
        if any(c["kind"] == "topup" and _alive(c["pid"])
               for c in STATE["children"].values()):
            return
        STATE["last_topup"] = time.time()
        save_state(STATE)
    chain = " && sleep 30 && ".join(TOPUP_CMDS)
    spawn(f"topup_{now:%Y%m%d}", chain, kind="topup")


# --------------------------------------------------------------------------- #
# Status assembly
# --------------------------------------------------------------------------- #
def codex_credits() -> dict:
    """Estimate codex credits spent from lane .jsonl event logs (GPT-5.5 rates)."""
    tin = tcache = tout = 0
    for p in LANE_DIR.glob("*.log"):
        try:
            with open(p, "rb") as fh:
                for raw in fh:
                    if b'"turn.completed"' not in raw:
                        continue
                    try:
                        u = json.loads(raw)["usage"]
                        tin += u.get("input_tokens", 0)
                        tcache += u.get("cached_input_tokens", 0)
                        tout += u.get("output_tokens", 0)
                    except Exception:
                        continue
        except OSError:
            continue
    credits = ((tin - tcache) * 125 + tcache * 12.5 + tout * 750) / 1e6
    return {"input_tokens": tin, "cached": tcache, "output_tokens": tout,
            "credits_est": round(credits, 1)}


def ledger_tail(n: int = 6) -> list:
    if not LEDGER.exists():
        return []
    rows = []
    for raw in LEDGER.read_text().strip().splitlines()[-n:]:
        try:
            r = json.loads(raw)
            rows.append({k: r.get(k) for k in
                         ("ts", "hyp_id", "family", "verdict", "cents_per_contract",
                          "passed") if k in r})
        except Exception:
            continue
    return rows


def _external_codex() -> dict:
    """Codex agents launched outside the controller (apex shell fleets).
    Display-only adoption so the dashboard never lies about what's running."""
    out = {}
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,etime=,command="],
                            capture_output=True, text=True, timeout=5).stdout
        for line in ps.splitlines():
            if "codex exec" not in line or "ps -axo" in line:
                continue
            pid, etime = line.split(None, 2)[:2]
            # cwd reveals which quarantine the agent works in (absolute path:
            # launchd PATH lacks /usr/sbin)
            try:
                cwd = subprocess.run(
                    ["/usr/sbin/lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                    capture_output=True, text=True, timeout=5).stdout
            except Exception:  # noqa: BLE001
                cwd = ""
            wd = next((l[1:] for l in cwd.splitlines() if l.startswith("n")), "?")
            label = Path(wd).name if wd != "?" else f"pid{pid}"
            parts = etime.replace("-", ":").split(":")
            secs = 0
            for p in parts:
                secs = secs * 60 + int(p)
            out[f"codex:{label}"] = {"pid": int(pid), "kind": "codex (apex)",
                                     "cmd": wd, "alive": True,
                                     "started": time.time() - secs,
                                     "elapsed_s": secs, "cwd": wd}
    except Exception:  # noqa: BLE001
        pass
    return out


def status() -> dict:
    with _lock:
        st = json.loads(json.dumps(STATE))  # snapshot
    st["children"] = {k: {**v, "alive": _alive(v["pid"]),
                          "elapsed_s": int(time.time() - v["started"])}
                      for k, v in st["children"].items()}
    st["children"].update(_external_codex())
    # next scheduled beats so the dashboard explains "idle" honestly
    rec = st.get("recurring", {})
    st["next_beats"] = {jid: int(rec.get(jid, 0) + iv - time.time())
                        for iv, jid, _ in PAPER_JOBS} if st["mode"] == "running" else {}
    disk = shutil.disk_usage(str(REPO))
    return {
        "mode": st["mode"],
        "since": st["since"],
        "children": st["children"],
        "next_beats": st.get("next_beats", {}),
        "queue": load_queue(),
        "caches": {"weather": len(list(WEATHER_CACHE.glob("*.pkl")))
                   if WEATHER_CACHE.exists() else 0,
                   "crypto": len(list(CRYPTO_CACHE.glob("*.pkl")))
                   if CRYPTO_CACHE.exists() else 0},
        "disk_free_gb": round(disk.free / 1e9, 1),
        "codex_budget": codex_credits(),
        "usage": usage_meter.read_cache(),
        "claude_policy": usage_profile.claude_defer_thresholds(),
        "throughput_policy": __import__("ops.governor", fromlist=["x"]).codex_policy(),
        "ledger_tail": ledger_tail(),
        "last_topup": st.get("last_topup"),
        "ts": time.time(),
    }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            html = (REPO / "ops/static/index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(200, json.dumps(status()).encode())
        elif self.path.startswith("/api/log/"):
            lane = self.path.rsplit("/", 1)[-1]
            p = LANE_DIR / f"{Path(lane).name}.log"
            body = p.read_bytes()[-20000:] if p.exists() else b"(no log)"
            self._send(200, body, "text/plain; charset=utf-8")
        else:
            self._send(404, b"{}")

    def do_POST(self):  # noqa: N802
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send(403, b'{"error":"localhost only"}')
            return
        if self.path == "/api/run":
            set_running()
            self._send(200, b'{"ok":true,"mode":"running"}')
        elif self.path == "/api/pause":
            set_paused()
            self._send(200, b'{"ok":true,"mode":"paused"}')
        elif self.path == "/api/enqueue":
            n = int(self.headers.get("Content-Length", 0))
            item = json.loads(self.rfile.read(n) or b"{}")
            if not item.get("id") or not item.get("cmd"):
                self._send(400, b'{"error":"need id and cmd"}')
                return
            with _lock:
                q = load_queue()
                q.append({"id": item["id"], "cmd": item["cmd"],
                          "cwd": item.get("cwd"), "status": "queued",
                          "enabled": True, "added": time.time()})
                save_queue(q)
            self._send(200, b'{"ok":true}')
        elif self.path.startswith("/api/lane_toggle/"):
            lane = self.path.rsplit("/", 1)[-1]
            with _lock:
                q = load_queue()
                for it in q:
                    if it["id"] == lane:
                        it["enabled"] = not it.get("enabled", True)
                save_queue(q)
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b"{}")

    def log_message(self, *a):  # quiet
        pass


def main() -> int:
    STATE_DIR.mkdir(exist_ok=True)
    LANE_DIR.mkdir(parents=True, exist_ok=True)
    # crash recovery: forget dead children, keep mode
    with _lock:
        STATE["children"] = {k: v for k, v in STATE["children"].items()
                             if _alive(v["pid"])}
        if STATE["mode"] == "running" and not _alive(STATE.get("caffeinate_pid")):
            p = subprocess.Popen(["caffeinate", "-dims"], start_new_session=True)
            STATE["caffeinate_pid"] = p.pid
        save_state(STATE)

    threading.Thread(target=scheduler, daemon=True).start()

    def shutdown(*_):
        log("controller shutting down (children left running per mode)")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    log(f"controller up on http://localhost:{PORT} mode={STATE['mode']}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
