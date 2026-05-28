"""Subprocess management for the Codex worker pool.

Each "worker" is one ``codex exec`` subprocess running in an isolated scratch
directory. The orchestrator spawns N of them in parallel; each one reads its
own ``direction.md`` and is expected to produce ``result.md`` before exit.

Codex CLI v0.128.x reference (verified at build time, 2026-05-28):

    codex exec [OPTIONS] [PROMPT]
        -s, --sandbox <SANDBOX_MODE>       read-only|workspace-write|danger-full-access
        -C, --cd <DIR>                     working root
            --add-dir <DIR>                additional writable dirs
            --skip-git-repo-check
            --json                          emit events as JSONL
        -m, --model <MODEL>                inherit from config.toml profile by default
        -c, --config <key=value>           toml-encoded overrides

Note: there is NO ``--prompt-file`` flag in this version. We pass the prompt
as the positional PROMPT argument; if it is large, we pipe via stdin instead.
We pipe via stdin so the prompt does not show up in process listings or
argv-based audit tools.

The ``workspace-write`` sandbox lets Codex write within ``--cd <worker_dir>``
and read project files; combined with the additional safety boundary in
``research/agents/tools/run_backtest.py`` (which is what every worker is
expected to invoke), this is enough for Phase 1. Phase 2 could tighten via
``--add-dir`` restrictions if needed.

Codex command construction details
----------------------------------
- We prepend ``--skip-git-repo-check`` because the per-worker scratch
  directory under ``research/experiments/codex_<id>/`` is inside the parent
  repo but Codex's git-repo detection is sometimes flaky for nested paths.
  Failing fast on git checks would mask real failures in the worker.
- We pass an empty PROMPT positional and feed the actual prompt via stdin
  so it works for arbitrarily large prompt files.
- We tee stderr to a per-worker file so timeout-killed workers leave a
  forensic trail.

Failure modes the dispatcher handles
------------------------------------
- Codex CLI binary missing → :class:`FileNotFoundError` surfaced cleanly.
- Worker timeout → SIGKILL the process group; result dict records
  ``exit_code = -9`` and ``timed_out = True``.
- Worker crashes without writing ``result.md`` → ``result_md_text = None``;
  caller treats as gate_passed=False.
- Codex returns a rate-limit signature in stderr → ``rate_limited = True``;
  the orchestrator's main loop pauses and reduces concurrency.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


DEFAULT_EXPERIMENTS_BASE = Path("research/experiments")
DEFAULT_PROMPT_PATH = Path("research/agents/workers/codex_worker_prompt.md")
DEFAULT_TIMEOUT_SEC = 30 * 60  # 30 minutes per plan v2 §codex_dispatcher

# Where the dispatcher looks for the Codex CLI binary if ``codex`` is not on
# PATH. Set in priority order; first hit wins.
_CODEX_FALLBACK_PATHS = (
    Path.home() / ".nvm/versions/node/v20.19.2/bin/codex",
    Path("/usr/local/bin/codex"),
    Path("/opt/homebrew/bin/codex"),
)

# Regex matches common rate-limit / quota-exhaustion signatures we have seen
# from OpenAI-style CLIs. Tested via the unit suite; extend as needed when
# real-world stderr surfaces new wording.
_RATE_LIMIT_RX = re.compile(
    r"(rate.?limit|429|quota|too[\s_-]?many[\s_-]?requests)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass
class CodexWorker:
    """In-flight handle to a spawned Codex worker.

    The dataclass is NOT frozen because :func:`wait_for_workers` mutates
    ``proc`` and the dispatcher caches stderr-tail data on it. The fields
    documented as "set at spawn" are written exactly once.
    """

    worker_id: str
    """8-char hex token uniquely identifying this worker run."""

    worker_dir: Path
    """Scratch directory under ``research/experiments/codex_<id>/``."""

    direction_md: str
    """Research-direction prompt content that was written into ``direction.md``."""

    start_ts: float
    """``time.time()`` at spawn — used by the timeout enforcer."""

    proc: subprocess.Popen | None = None
    """The running subprocess. None for dry-run / stubbed workers."""

    stderr_path: Path | None = None
    """Where the worker's stderr is being tee'd. None if not started."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve_codex_binary() -> str:
    """Find the ``codex`` executable.

    Looks at the ``CODEX_BIN`` env override first (used by tests to stub
    ``codex`` with ``/bin/echo`` or a fake script), then PATH, then the
    fallback paths above. Returns the full path as a string suitable for
    ``subprocess.Popen``.

    Raises ``FileNotFoundError`` if no binary can be located.
    """
    override = os.environ.get("CODEX_BIN")
    if override:
        return override
    on_path = shutil.which("codex")
    if on_path is not None:
        return on_path
    for cand in _CODEX_FALLBACK_PATHS:
        if cand.exists():
            return str(cand)
    raise FileNotFoundError(
        "codex CLI not found on PATH and none of the fallback paths exist; "
        "set CODEX_BIN env var or install codex. Looked at: "
        + ", ".join(str(p) for p in _CODEX_FALLBACK_PATHS)
    )


def _make_worker_id() -> str:
    return uuid.uuid4().hex[:8]


def detect_rate_limit(stderr: str) -> bool:
    """True iff ``stderr`` contains a rate-limit / 429 signature.

    Conservative: a false positive causes the dispatcher to sleep 10 minutes
    unnecessarily; a false negative causes us to hammer Codex and risk a
    longer cool-down. Better to over-detect.
    """
    if not stderr:
        return False
    return bool(_RATE_LIMIT_RX.search(stderr))


# --------------------------------------------------------------------------- #
# spawn_workers
# --------------------------------------------------------------------------- #


def _write_direction_file(worker_dir: Path, direction_md: str) -> None:
    """Write ``direction.md`` into the worker's scratch dir."""
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "direction.md").write_text(direction_md, encoding="utf-8")


def _build_codex_argv(
    codex_bin: str,
    worker_dir: Path,
    model_flag: str | None,
) -> list[str]:
    """Compose the codex exec argv.

    The prompt is fed via stdin in :func:`spawn_workers`, so we do not pass
    a positional PROMPT here.
    """
    argv = [
        codex_bin,
        "exec",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(worker_dir),
        "--skip-git-repo-check",
    ]
    if model_flag:
        # ``-c model=<name>`` per ``codex exec --help``. We accept a free-form
        # ``model_flag`` string so the caller can also pass ``-c
        # model_provider=...`` if the active config profile is wrong; we just
        # split on whitespace.
        argv.extend(model_flag.split())
    return argv


def spawn_workers(
    direction: str,
    worker_count: int,
    base_dir: Path | str = DEFAULT_EXPERIMENTS_BASE,
    prompt_path: Path | str = DEFAULT_PROMPT_PATH,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    model_flag: str | None = None,
) -> list[CodexWorker]:
    """Spawn ``worker_count`` codex subprocesses, return immediately.

    Each worker gets its own scratch directory under
    ``<base_dir>/codex_<worker_id>/`` with ``direction.md`` already written.
    The prompt at ``prompt_path`` is piped to each worker's stdin.

    Parameters
    ----------
    direction:
        Research-direction markdown content. The same content is written to
        every worker's ``direction.md`` — the workers diverge through Codex's
        own sampling and the per-worker scratch directory.
    worker_count:
        Number of parallel workers to spawn. Must be >= 1.
    base_dir:
        Parent directory for per-worker scratch dirs.
    prompt_path:
        Path to the codex worker prompt markdown. Required to exist; we read
        it once and feed the same bytes to each worker's stdin.
    timeout_sec:
        Per-worker timeout used by :func:`wait_for_workers`. Stored on the
        CodexWorker for the waiter to consult.
    model_flag:
        Optional extra args (``-c model=<name>`` or similar) prepended to
        the codex argv after the sandbox flags. ``None`` means inherit the
        user's ``~/.codex/config.toml`` default profile.

    Raises
    ------
    ValueError
        If ``worker_count < 1`` or ``prompt_path`` does not exist.
    FileNotFoundError
        If the codex binary cannot be located.
    """
    if worker_count < 1:
        raise ValueError(f"worker_count must be >= 1, got {worker_count!r}")
    prompt_p = Path(prompt_path)
    if not prompt_p.exists():
        raise ValueError(f"codex worker prompt not found at {prompt_p}")
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)

    codex_bin = _resolve_codex_binary()
    prompt_bytes = prompt_p.read_bytes()

    workers: list[CodexWorker] = []
    for _ in range(worker_count):
        worker_id = _make_worker_id()
        worker_dir = base / f"codex_{worker_id}"
        _write_direction_file(worker_dir, direction)

        stderr_path = worker_dir / "stderr.log"
        stdout_path = worker_dir / "stdout.log"
        # Open stdout/stderr files; subprocess.Popen will keep them open until
        # the child finishes.
        stderr_fh = stderr_path.open("wb")
        stdout_fh = stdout_path.open("wb")

        argv = _build_codex_argv(codex_bin, worker_dir, model_flag)

        # ``start_new_session=True`` (POSIX) puts the child in its own process
        # group so :func:`wait_for_workers` can SIGKILL the entire tree on
        # timeout. Codex itself spawns helper processes; without this we'd
        # leave orphans.
        proc = subprocess.Popen(  # noqa: S603 — argv list is trusted
            argv,
            stdin=subprocess.PIPE,
            stdout=stdout_fh,
            stderr=stderr_fh,
            cwd=str(worker_dir),
            start_new_session=(os.name == "posix"),
        )
        # Feed the prompt to stdin in a non-blocking way: write everything,
        # close stdin so codex sees EOF. Codex's exec mode reads the prompt
        # from stdin only when no positional PROMPT was supplied.
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt_bytes)
                proc.stdin.close()
        except BrokenPipeError:
            # The child has already died — wait_for_workers will report the
            # exit code. Don't lose the worker handle here.
            pass

        workers.append(
            CodexWorker(
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
# wait_for_workers
# --------------------------------------------------------------------------- #


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _kill_worker(worker: CodexWorker) -> None:
    """Best-effort termination of a worker's process group."""
    if worker.proc is None:
        return
    pid = worker.proc.pid
    try:
        if os.name == "posix":
            # We started the child in its own session, so killpg with -pid is safe.
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        else:
            worker.proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone — fine.
        pass


def wait_for_workers(
    workers: list[CodexWorker],
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    poll_interval_sec: float = 1.0,
) -> list[dict]:
    """Block until all workers exit or hit ``timeout_sec`` since their spawn.

    Returns one result dict per worker (same order as ``workers``):

        {
            "worker_id": str,
            "worker_dir": str,        # absolute path string
            "exit_code": int,         # -9 on timeout kill, -1 on never-started
            "stdout": str,            # tail of stdout (last ~64KB)
            "stderr": str,            # tail of stderr (last ~64KB)
            "duration_sec": float,
            "result_md_text": str | None,   # contents of <worker_dir>/result.md, if any
            "rate_limited": bool,     # detect_rate_limit(stderr)
            "timed_out": bool,
        }
    """
    remaining = list(workers)
    finished: dict[str, dict] = {}

    # ``per-worker`` timeout is wall-clock from spawn; we honour that, not the
    # call-time of wait_for_workers, so a slow handoff between spawn and wait
    # doesn't shorten the budget.
    while remaining:
        next_remaining: list[CodexWorker] = []
        now = time.time()
        for w in remaining:
            if w.proc is None:
                # Not started — record and move on.
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
            # Still running — check timeout.
            if (now - w.start_ts) > timeout_sec:
                _kill_worker(w)
                # Give the kernel a moment, then call wait() with a short
                # timeout so the proc object cleans up.
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

    # Re-emit in input order so the caller's per-index assumptions hold.
    return [finished[w.worker_id] for w in workers]


def _result_dict_for(
    worker: CodexWorker,
    exit_code: int,
    timed_out: bool,
) -> dict:
    """Compose the result dict for one worker."""
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
    }


def _tail_text(path: Path, max_bytes: int = 64 * 1024) -> str:
    """Read the last ``max_bytes`` from ``path`` as utf-8 (errors=replace).

    The orchestrator only needs the tail for rate-limit detection and audit;
    long full-file reads would balloon memory if a worker writes verbose logs.
    """
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


__all__ = [
    "CodexWorker",
    "DEFAULT_EXPERIMENTS_BASE",
    "DEFAULT_PROMPT_PATH",
    "DEFAULT_TIMEOUT_SEC",
    "detect_rate_limit",
    "spawn_workers",
    "wait_for_workers",
]
