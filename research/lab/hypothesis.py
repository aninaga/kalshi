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
    seed_defaults(path=...) -> None
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone

from research.lab.types import SPREAD, TOTAL, WINNER, Hypothesis

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


# --- starter ideas (drawn from research/ALPHA_FINDINGS.md) ------------------

def _default_hypotheses() -> list[Hypothesis]:
    """Starter NBA ideas seeded only into an empty store.

    Pre-registered directions are 1-bit and mechanism-driven; verdicts here are
    intentionally NOT pre-filled even where ALPHA_FINDINGS has a prior read, so a
    fresh agent re-derives the call under realistic execution + the gate.
    """
    return [
        # --- anchoring family ---
        Hypothesis(
            market=TOTAL,
            mechanism=(
                "Live totals lines anchor to a naive pace projection (current "
                "total scaled to a full game) and are slow to mean-revert toward "
                "the season-typical pace, so early-game pace shocks over/under-price "
                "the final total."
            ),
            signal_desc="pace_projection(panel) - implied total level (anchoring_gap)",
            direction="bet toward the season-mean pace when the projection diverges",
        ),
        Hypothesis(
            market=SPREAD,
            mechanism=(
                "Live spread lines anchor to the current margin and underweight "
                "expected reversion of an early lead, leaving a residual mispricing "
                "on the trailing side that survives realistic fills (real fill mid "
                "~0.5187, not 0.50)."
            ),
            signal_desc="current margin vs. reversion-adjusted expected final margin",
            direction="bet the trailing side when the implied margin over-extrapolates the lead",
        ),
        # --- calibration family ---
        Hypothesis(
            market=WINNER,
            mechanism=(
                "In-game win-probability quotes are mis-calibrated at the extremes "
                "(favorite-longshot bias): heavy favorites are systematically "
                "over-priced and longshots under-priced relative to realized "
                "outcomes."
            ),
            signal_desc="calibration_gap(panel, realized) at extreme implied win-prob",
            direction="fade extreme favorites / back extreme longshots",
        ),
        Hypothesis(
            market=TOTAL,
            mechanism=(
                "At extreme totals strikes (deep over/under tails) the listed "
                "ladder is mis-calibrated because tail liquidity is thin and slow "
                "to update on realized pace."
            ),
            signal_desc="calibration_gap on deep-tail strikes of the totals ladder",
            direction="bet toward realized pace on mis-calibrated tail strikes",
        ),
        # --- reactions family ---
        Hypothesis(
            market=WINNER,
            mechanism=(
                "After a discrete game event (star substitution / timeout), the "
                "market re-rates slowly beyond the raw scoreboard move, so the "
                "win-prob lags fair value for a few minutes (sub-latency)."
            ),
            signal_desc="staleness_min(panel) elevated immediately after an event",
            direction="trade toward the post-event fair value while the quote is stale",
        ),
        Hypothesis(
            market=SPREAD,
            mechanism=(
                "A fair-value gap opens when the quoted level drifts away from a "
                "freshly-updated model fair value (e.g. after a scoring run) and "
                "the gap closes over the next minutes (mean-reversion of the gap)."
            ),
            signal_desc="zscore of (implied level - rolling fair value)",
            direction="fade the gap: bet toward fair value when the z-score is extreme",
        ),
    ]


def seed_defaults(path: str = DEFAULT_PATH) -> None:
    """Seed a few starter NBA ideas — ONLY if the store is currently empty.

    Idempotent: a second call is a no-op once anything has been registered.
    """
    if _load_state(path):
        return
    for h in _default_hypotheses():
        register(h, path=path)
