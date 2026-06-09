"""research.lab.hypothesis — the dynamic hypothesis registry.

An append-only JSONL store of :class:`research.lab.types.Hypothesis` rows. This
is the runtime replacement for the hard-coded direction menus
(``agents/usage/edge_hunt_loop.py::DIRECTIONS`` /
``agents/orchestrator/run.py::DIRECTION_ROTATION``): an analyst-agent ORIGINATES
ideas at runtime by ``register``-ing them, ``claim``-s an open one to work on it,
and ``update``-s its verdict/results when done.

Design notes
------------
- **Append-only JSONL.** Every mutation (register / claim / update) appends one
  JSON line. State is reconstructed by replaying the file and applying
  last-write-wins per hypothesis id. This is concurrency-tolerant: many agents
  can append concurrently (each ``write`` is a single small append) and each
  reader simply re-reads the whole file. We do not rewrite or truncate.
- **Dedupe by idea hash.** ``register`` assigns ``id = h.hash()`` (a stable key
  over market|mechanism|direction, NOT status/results), so re-registering the
  same idea is a no-op that returns the existing row.

Public API (see ``research/lab/CONTRACT.md`` §lab/hypothesis.py)::

    DEFAULT_PATH
    register(h, path=...) -> Hypothesis
    claim(hyp_id, agent, path=...) -> Hypothesis
    update(hyp_id, *, verdict=None, results=None, status=None, path=...) -> Hypothesis
    query(status=None, market=None, path=...) -> list[Hypothesis]
    open_hypotheses(path=...) -> list[Hypothesis]
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone

from research.lab.types import Hypothesis

DEFAULT_PATH = "research/reports/alpha/hypotheses.jsonl"

# Fields persisted per JSONL row (exactly the Hypothesis dataclass fields).
_FIELDS = tuple(f.name for f in dataclasses.fields(Hypothesis))


def _now() -> str:
    """UTC ISO-8601 timestamp (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_row(h: Hypothesis) -> dict:
    return {f: getattr(h, f) for f in _FIELDS}


def _from_row(row: dict) -> Hypothesis:
    # Tolerate forward/backward-compatible rows: ignore unknown keys, default missing.
    kwargs = {f: row[f] for f in _FIELDS if f in row}
    return Hypothesis(**kwargs)


def _read_rows(path: str) -> list[dict]:
    """Read every JSONL row in file order. Skips blank/corrupt lines."""
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn line from a concurrent partial write — skip it.
                continue
    return rows


def _load_state(path: str) -> "dict[str, Hypothesis]":
    """Replay the log into the current state (last-write-wins per id)."""
    state: dict[str, Hypothesis] = {}
    for row in _read_rows(path):
        hyp_id = row.get("id")
        if not hyp_id:
            continue
        state[hyp_id] = _from_row(row)
    return state


def _append(path: str, h: Hypothesis) -> None:
    """Append one row. Creates parent dirs on first write."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(_to_row(h), ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def register(h: Hypothesis, path: str = DEFAULT_PATH) -> Hypothesis:
    """Register an idea. Assigns ``id = h.hash()`` and DEDUPES on it.

    If an idea with the same hash already exists, returns the existing row
    unchanged (no duplicate append). Otherwise appends a new ``open`` row with
    ``created``/``updated`` timestamps and returns it.
    """
    hyp_id = h.hash()
    existing = _load_state(path).get(hyp_id)
    if existing is not None:
        return existing

    now = _now()
    new = dataclasses.replace(
        h,
        id=hyp_id,
        status=h.status or "open",
        created=h.created or now,
        updated=now,
    )
    _append(path, new)
    return new


def claim(hyp_id: str, agent: str, path: str = DEFAULT_PATH) -> Hypothesis:
    """Claim an open hypothesis: set ``status="running"`` and record ``agent``.

    Raises ``KeyError`` if ``hyp_id`` is unknown.
    """
    state = _load_state(path)
    current = state.get(hyp_id)
    if current is None:
        raise KeyError(f"unknown hypothesis id: {hyp_id!r}")

    results = dict(current.results)
    results["claimed_by"] = agent
    results["claimed_at"] = _now()
    updated = dataclasses.replace(
        current, status="running", results=results, updated=_now()
    )
    _append(path, updated)
    return updated


def update(
    hyp_id: str,
    *,
    verdict: str | None = None,
    results: dict | None = None,
    status: str | None = None,
    path: str = DEFAULT_PATH,
) -> Hypothesis:
    """Update a hypothesis's ``verdict`` / ``results`` / ``status``.

    ``results`` is MERGED into the existing results dict (not replaced), so
    callers can incrementally accumulate trial output. ``None`` arguments leave
    that field unchanged. Raises ``KeyError`` if ``hyp_id`` is unknown.
    """
    state = _load_state(path)
    current = state.get(hyp_id)
    if current is None:
        raise KeyError(f"unknown hypothesis id: {hyp_id!r}")

    merged = dict(current.results)
    if results:
        merged.update(results)

    updated = dataclasses.replace(
        current,
        verdict=verdict if verdict is not None else current.verdict,
        results=merged,
        status=status if status is not None else current.status,
        updated=_now(),
    )
    _append(path, updated)
    return updated


def query(
    status: str | None = None,
    market: str | None = None,
    path: str = DEFAULT_PATH,
) -> list[Hypothesis]:
    """Return current hypotheses, optionally filtered by ``status`` / ``market``.

    Ordered by creation time (then id) for a stable, deterministic listing.
    """
    rows = list(_load_state(path).values())
    if status is not None:
        rows = [h for h in rows if h.status == status]
    if market is not None:
        rows = [h for h in rows if h.market == market]
    rows.sort(key=lambda h: (h.created, h.id))
    return rows


def open_hypotheses(path: str = DEFAULT_PATH) -> list[Hypothesis]:
    """All hypotheses with ``status == "open"`` (i.e. unclaimed)."""
    return query(status="open", path=path)


# --- NO hardcoded ideas live here, by design -------------------------------
# Hypotheses are ORIGINATED at runtime by an agent via ``research.lab.scout``
# (which reasons over ``research.lab.eda`` diagnostics) and PRIORITIZED by
# ``research.lab.director``. There is deliberately no hand-written starter list
# or ``seed_defaults`` — the only thing the codebase hardcodes about which
# strategies to pursue is that decision *machinery*, never the strategies. A
# cold (empty) registry is populated by ``scout.propose(...)``, not by canned ideas.
