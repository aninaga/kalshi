"""Claude Code worker pool — symmetric with :mod:`codex_dispatcher`.

Each worker is one ``claude -p --dangerously-skip-permissions`` headless
subprocess running in its own scratch directory. The API surface
(:func:`spawn_workers`, :func:`wait_for_workers`) intentionally mirrors
``codex_dispatcher`` so the orchestrator can treat the two pools uniformly.

Why a separate dispatcher
-------------------------
The ``claude`` CLI differs from ``codex`` in three important ways:

1. ``claude -p`` takes the prompt as a positional argv string (no `--prompt-file`
   and stdin is for tool I/O once the agent is alive). Large prompts go on argv;
   macOS allows ~256KB which is plenty for the worker prompt + direction.
2. ``claude --dangerously-skip-permissions`` bypasses BOTH the permissions
   allowlist AND the project hooks. This is required because the worker calls
   ``-m research.agents.tools.run_backtest`` (module form), which is a write
   path against ``market_data/trials.db`` — the hook would otherwise deny.
3. Auth uses the user's Max-20x subscription via OAuth/keychain (NOT
   ``--bare``, which would break OAuth and force API-key billing).

Per plan §safety, ``run_backtest.py`` is the canonical security boundary;
this dispatcher trusts that layer plus the explicit worker prompt to keep
the worker honest, exactly like the codex pool does.

The result-dict shape returned by :func:`wait_for_workers` is identical to
``codex_dispatcher``'s, so the orchestrator's per-result code path treats
both pools uniformly.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


DEFAULT_EXPERIMENTS_BASE = Path("research/experiments")
DEFAULT_PROMPT_PATH = Path("research/agents/workers/codex_worker_prompt.md")
DEFAULT_TIMEOUT_SEC = 30 * 60

# Fallback locations to look for the ``claude`` CLI if ``shutil.which`` misses
# (e.g. when the orchestrator runs from a venv whose PATH doesn't include the
# user's ``~/.local/bin``).
_CLAUDE_FALLBACK_PATHS = (
    Path.home() / ".local/bin/claude",
    Path("/usr/local/bin/claude"),
    Path("/opt/homebrew/bin/claude"),
)

# Same rate-limit signature regex as codex_dispatcher: matches the common
# stderr patterns from rate-limited LLM CLIs. False positives → 10-min
# back-off (cheap); false negatives → continue hammering, which is worse.
_RATE_LIMIT_RX = re.compile(
    r"(rate.?limit|429|quota|too[\s_-]?many[\s_-]?requests|usage[\s_-]?limit)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass
class ClaudeWorker:
    """In-flight handle to a spawned Claude worker. Mirrors CodexWorker."""

    worker_id: str
    worker_dir: Path
    direction_md: str
    start_ts: float
    proc: subprocess.Popen | None = None
    stderr_path: Path | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve_claude_binary() -> str:
    """Find the ``claude`` executable.

    Honours ``CLAUDE_BIN`` env override (used by tests), then PATH, then the
    fallback paths.
    """
    override = os.environ.get("CLAUDE_BIN")
    if override:
        return override
    on_path = shutil.which("claude")
    if on_path is not None:
        return on_path
    for cand in _CLAUDE_FALLBACK_PATHS:
        if cand.exists():
            return str(cand)
    raise FileNotFoundError(
        "claude CLI not found on PATH and none of the fallback paths exist; "
        "set CLAUDE_BIN env var or install claude. Looked at: "
        + ", ".join(str(p) for p in _CLAUDE_FALLBACK_PATHS)
    )


def _make_worker_id() -> str:
    return uuid.uuid4().hex[:8]


def detect_rate_limit(stderr: str) -> bool:
    if not stderr:
        return False
    return bool(_RATE_LIMIT_RX.search(stderr))


def _write_direction_file(worker_dir: Path, direction_md: str) -> None:
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "direction.md").write_text(direction_md, encoding="utf-8")


def _compose_prompt(prompt_md: str, direction_md: str, worker_id: str) -> str:
    """Combine the worker prompt + direction + worker_id into one argv string.

    Workers expect ``<YOUR_WORKER_ID>`` to come from ``direction.md`` per the
    prompt, but ``direction.md`` is built by the orchestrator and doesn't
    actually include a worker_id (the orchestrator never knew which worker
    was which when composing). We patch it in here as a clearly-labelled
    block at the top.
    """
    header = (
        f"# CLAUDE WORKER BOOTSTRAP\n\n"
        f"You are Claude worker `{worker_id}` (an 8-hex-char id). "
        f"When the worker prompt below says to use "
        f"`--agent-id \"codex:<YOUR_WORKER_ID>\"`, you instead use "
        f"`--agent-id \"claude:{worker_id}\"`. The rest of the worker "
        f"prompt applies verbatim. Ignore any auto-loaded CLAUDE.md; your "
        f"sole context is this prompt + direction.md.\n\n"
        f"---\n\n"
    )
    return header + prompt_md + "\n\n---\n\n# direction.md\n\n" + direction_md


# --------------------------------------------------------------------------- #
# spawn_workers
# --------------------------------------------------------------------------- #


def spawn_workers(
    direction: str,
    worker_count: int,
    base_dir: Path | str = DEFAULT_EXPERIMENTS_BASE,
    prompt_path: Path | str = DEFAULT_PROMPT_PATH,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    model: str | None = None,
    cost_profile: str | None = None,
) -> list[ClaudeWorker]:
    """Spawn ``worker_count`` claude-headless subprocesses, return immediately.

    Mirrors :func:`codex_dispatcher.spawn_workers`. Each worker gets its own
    scratch directory under ``<base_dir>/claude_<worker_id>/``.

    ``model`` is the optional model override (e.g. ``"claude-opus-4-7"``).
    If ``None``, the user's default Claude model is used.

    Raises
    ------
    ValueError
        If ``worker_count < 1`` or the prompt file is missing.
    FileNotFoundError
        If the claude binary cannot be located.
    """
    if worker_count < 1:
        raise ValueError(f"worker_count must be >= 1, got {worker_count!r}")
    prompt_p = Path(prompt_path)
    if not prompt_p.exists():
        raise ValueError(f"worker prompt not found at {prompt_p}")
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)

    claude_bin = _resolve_claude_binary()
    prompt_md = prompt_p.read_text(encoding="utf-8")

    workers: list[ClaudeWorker] = []
    for _ in range(worker_count):
        worker_id = _make_worker_id()
        worker_dir = base / f"claude_{worker_id}"
        _write_direction_file(worker_dir, direction)

        composed = _compose_prompt(prompt_md, direction, worker_id)

        stderr_path = worker_dir / "stderr.log"
        stdout_path = worker_dir / "stdout.log"
        stderr_fh = stderr_path.open("wb")
        stdout_fh = stdout_path.open("wb")

        argv: list[str] = [
            claude_bin,
            "-p",
            "--dangerously-skip-permissions",
            "--effort", "xhigh",  # max reasoning effort for research workers
        ]
        if model:
            argv.extend(["--model", model])
        argv.append(composed)

        env = os.environ.copy()
        if cost_profile:
            env["RESEARCH_COST_PROFILE"] = cost_profile
        proc = subprocess.Popen(  # noqa: S603 — argv list is trusted
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout_fh,
            stderr=stderr_fh,
            cwd=str(worker_dir.resolve()),
            env=env,
            start_new_session=(os.name == "posix"),
        )

        workers.append(
            ClaudeWorker(
                worker_id=worker_id,
                worker_dir=worker_dir,
                direction_md=direction,
                start_ts=time.time(),
                proc=proc,
                stderr_path=stderr_path,
            )
        )

    return workers


# --------------------------------------------------------------------------- #
# wait_for_workers — copied shape from codex_dispatcher
# --------------------------------------------------------------------------- #


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail_text(path: Path, max_bytes: int = 64 * 1024) -> str:
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _kill_worker(worker: ClaudeWorker) -> None:
    if worker.proc is None:
        return
    pid = worker.proc.pid
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        else:
            worker.proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _result_dict_for(
    worker: ClaudeWorker,
    exit_code: int,
    timed_out: bool,
) -> dict:
    stderr_text = _tail_text(worker.stderr_path) if worker.stderr_path else ""
    stdout_path = worker.worker_dir / "stdout.log"
    stdout_text = _tail_text(stdout_path) if stdout_path.exists() else ""
    result_md = worker.worker_dir / "result.md"
    result_text: str | None = None
    if result_md.exists():
        result_text = _read_text_if_exists(result_md)
    duration = time.time() - worker.start_ts
    return {
        "worker_id": worker.worker_id,
        "worker_dir": str(worker.worker_dir.resolve()),
        "exit_code": int(exit_code),
        "stdout": stdout_text,
        "stderr": stderr_text,
        "duration_sec": duration,
        "result_md_text": result_text,
        "rate_limited": detect_rate_limit(stderr_text),
        "timed_out": bool(timed_out),
        "engine": "claude",
    }


def wait_for_workers(
    workers: list[ClaudeWorker],
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    poll_interval_sec: float = 1.0,
) -> list[dict]:
    """Block until every worker exits or hits ``timeout_sec`` since spawn."""
    remaining = list(workers)
    finished: dict[str, dict] = {}

    while remaining:
        next_remaining: list[ClaudeWorker] = []
        now = time.time()
        for w in remaining:
            if w.proc is None:
                finished[w.worker_id] = _result_dict_for(
                    w, exit_code=-1, timed_out=False
                )
                continue
            rc = w.proc.poll()
            if rc is not None:
                finished[w.worker_id] = _result_dict_for(
                    w, exit_code=rc, timed_out=False
                )
                continue
            if (now - w.start_ts) > timeout_sec:
                _kill_worker(w)
                try:
                    w.proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
                finished[w.worker_id] = _result_dict_for(
                    w, exit_code=-9, timed_out=True
                )
                continue
            next_remaining.append(w)
        remaining = next_remaining
        if remaining:
            time.sleep(poll_interval_sec)

    return [finished[w.worker_id] for w in workers]


__all__ = [
    "ClaudeWorker",
    "DEFAULT_EXPERIMENTS_BASE",
    "DEFAULT_PROMPT_PATH",
    "DEFAULT_TIMEOUT_SEC",
    "detect_rate_limit",
    "spawn_workers",
    "wait_for_workers",
]
