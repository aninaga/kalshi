"""research.lab.data_agent — the on-demand DATA-REQUEST seam (no hardcoded fields).

In a real fund, an analyst who needs a field that does not exist yet files a
data request and a data engineer fulfills it. This module is the same seam on the
substrate: an analyst/scout agent, mid-research, ``request``-s a derived data
field (or a genuinely-new external feed), and a ``provider`` is summoned to
fulfill it into the Panel — WITHOUT hardcoding which fields get built.

The agentic seam (mirrors ``scout.propose``'s ``proposer`` /
``director.select``'s ``ranker``)
--------------------------------------------------------------------------------
``fulfill(request, panel, *, provider=default_provider)`` takes an injectable
``provider`` callable with a sensible default. The substrate is plain Python and
fully model-agnostic: nothing here knows or cares whether the provider is the
deterministic default, a smarter offline fulfiller, or an LLM-backed data agent.
An agent plugs in ONLY at that ``provider`` seam — exactly like the scout plugs
its model in at ``proposer``.

The no-fabrication rule
-----------------------
``default_provider`` is deterministic and OFFLINE (no network). It satisfies a
request ONLY when the spec is a composition of EXISTING panel fields via the
known operator vocabulary (ratios / deltas / rolling / zscore / interactions over
``mid, total, margin, elapsed_sec``), reusing ``research.lab.signals`` primitives
rather than reimplementing them. If a spec needs genuinely-new external data the
default provider cannot synthesize, the request is marked ``NEEDS_DATA`` and the
panel is returned UNCHANGED — we NEVER fabricate data. This honest "can't fulfill"
path is the point: a wrong number is worse than a missing one.

Append-only JSONL request log (mirrors ``research.lab.hypothesis``)
-------------------------------------------------------------------
Every mutation appends one JSON line; state is replayed last-write-wins per id.
``request`` dedupes on a stable hash over ``market|field_name|spec``.

Public API::

    DEFAULT_PATH
    DataRequest
    request(req, path=...) -> DataRequest
    query_requests(status=None, market=None, path=...) -> list[DataRequest]
    update_request(req_id, *, status=None, result=None, path=...) -> DataRequest
    default_provider(spec, panel) -> np.ndarray | None
    fulfill(req, panel, *, provider=default_provider) -> tuple[Panel | None, DataRequest]
    summon_for_hypothesis(h, *, spec=None, path=...) -> DataRequest
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from research.lab import signals
from research.lab.types import Panel

DEFAULT_PATH = "research/reports/alpha/data_requests.jsonl"

# Statuses a request can carry. ``NEEDS_DATA`` is the honest "can't synthesize
# offline — a human/agent must source it" terminal state.
OPEN = "open"
FULFILLED = "fulfilled"
NEEDS_DATA = "NEEDS_DATA"

# The base panel series the deterministic operator vocabulary composes over.
_BASE_KEYS = ("mid", "total", "margin", "elapsed_sec")


@dataclass
class DataRequest:
    """An analyst's request for a derived field / data improvement.

    ``spec`` describes the requested derivation, e.g. ``"zscore(mid,5)"``,
    ``"rolling(margin,3) - margin"``, or a free-text external-data ask such as
    ``"injury report feed"``. ``status`` is one of ``open`` / ``fulfilled`` /
    ``NEEDS_DATA``. ``result`` holds provider metadata once fulfilled (never the
    array itself — arrays attach to the panel, not the log).
    """
    market: str
    field_name: str                  # the name the field will attach under
    spec: str                        # requested derivation (op-expr or free text)
    rationale: str = ""              # WHY the analyst needs it
    id: str = ""
    status: str = OPEN               # open | fulfilled | NEEDS_DATA
    result: dict = field(default_factory=dict)
    created: str = ""
    updated: str = ""

    def hash(self) -> str:
        """Stable dedupe key from the request identity (not its status/result)."""
        key = f"{self.market}|{self.field_name.strip().lower()}|{self.spec.strip().lower()}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]


# Fields persisted per JSONL row (exactly the DataRequest dataclass fields).
_FIELDS = tuple(f.name for f in dataclasses.fields(DataRequest))


def _now() -> str:
    """UTC ISO-8601 timestamp (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_row(r: DataRequest) -> dict:
    return {f: getattr(r, f) for f in _FIELDS}


def _from_row(row: dict) -> DataRequest:
    # Tolerate forward/backward-compatible rows: ignore unknown, default missing.
    kwargs = {f: row[f] for f in _FIELDS if f in row}
    return DataRequest(**kwargs)


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


def _load_state(path: str) -> "dict[str, DataRequest]":
    """Replay the log into the current state (last-write-wins per id)."""
    state: dict[str, DataRequest] = {}
    for row in _read_rows(path):
        req_id = row.get("id")
        if not req_id:
            continue
        state[req_id] = _from_row(row)
    return state


def _append(path: str, r: DataRequest) -> None:
    """Append one row. Creates parent dirs on first write."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(_to_row(r), ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def request(req: DataRequest, path: str = DEFAULT_PATH) -> DataRequest:
    """File a data request. Assigns ``id = req.hash()`` and DEDUPES on it.

    If a request with the same hash already exists, returns the existing row
    unchanged (no duplicate append). Otherwise appends a new ``open`` row with
    ``created``/``updated`` timestamps and returns it.
    """
    req_id = req.hash()
    existing = _load_state(path).get(req_id)
    if existing is not None:
        return existing

    now = _now()
    new = dataclasses.replace(
        req,
        id=req_id,
        status=req.status or OPEN,
        created=req.created or now,
        updated=now,
    )
    _append(path, new)
    return new


def update_request(
    req_id: str,
    *,
    status: str | None = None,
    result: dict | None = None,
    path: str = DEFAULT_PATH,
) -> DataRequest:
    """Update a request's ``status`` / ``result``.

    ``result`` is MERGED into the existing result dict (not replaced). ``None``
    arguments leave that field unchanged. Raises ``KeyError`` if ``req_id`` is
    unknown.
    """
    state = _load_state(path)
    current = state.get(req_id)
    if current is None:
        raise KeyError(f"unknown data request id: {req_id!r}")

    merged = dict(current.result)
    if result:
        merged.update(result)

    updated = dataclasses.replace(
        current,
        status=status if status is not None else current.status,
        result=merged,
        updated=_now(),
    )
    _append(path, updated)
    return updated


def query_requests(
    status: str | None = None,
    market: str | None = None,
    path: str = DEFAULT_PATH,
) -> list[DataRequest]:
    """Return current requests, optionally filtered by ``status`` / ``market``.

    Ordered by creation time (then id) for a stable, deterministic listing.
    """
    rows = list(_load_state(path).values())
    if status is not None:
        rows = [r for r in rows if r.status == status]
    if market is not None:
        rows = [r for r in rows if r.market == market]
    rows.sort(key=lambda r: (r.created, r.id))
    return rows


# --- the deterministic, offline default provider --------------------------
# A tiny grammar over the EXISTING panel fields. Anything outside it is, by
# definition, data we do not have — and we refuse to fabricate it.

_NUM = r"-?\d+(?:\.\d+)?"
# rolling(key, w) / zscore(key, w) over a base key.
_ROLLING_RE = re.compile(rf"^rolling\(\s*(\w+)\s*,\s*(\d+)\s*\)$")
_ZSCORE_RE = re.compile(rf"^zscore\(\s*(\w+)\s*,\s*(\d+)\s*\)$")
# A binary op between two terms; each term is a base key, a rolling/zscore call,
# or a numeric literal. Operators: + - * /.
_BINOP_RE = re.compile(r"^(.+?)\s*([+\-*/])\s*(.+)$")


def _term(token: str, panel: Panel) -> np.ndarray | None:
    """Resolve one term to a length-n array, or None if not derivable offline."""
    token = token.strip()
    if not token:
        return None

    m = _ROLLING_RE.match(token)
    if m:
        key, w = m.group(1), int(m.group(2))
        if key not in _BASE_KEYS and key not in panel.features:
            return None
        return signals.rolling(panel, key, w)

    m = _ZSCORE_RE.match(token)
    if m:
        key, w = m.group(1), int(m.group(2))
        if key not in _BASE_KEYS and key not in panel.features:
            return None
        return signals.zscore(signals._series(panel, key), w)

    # numeric literal -> broadcast scalar
    if re.fullmatch(_NUM, token):
        return np.full(panel.n, float(token), dtype=float)

    # bare base key (or existing feature)
    if token in _BASE_KEYS or token in panel.features:
        return signals._series(panel, token)

    return None


def _apply(op: str, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    # op == "/"
    with np.errstate(divide="ignore", invalid="ignore"):
        return a / b


def default_provider(spec: str, panel: Panel) -> np.ndarray | None:
    """Deterministic, OFFLINE fulfiller over EXISTING panel fields.

    Returns a length-``panel.n`` array if ``spec`` is a composition of existing
    fields via the known operator vocabulary (ratios / deltas / rolling / zscore
    / interactions over ``mid, total, margin, elapsed_sec``), reusing
    ``research.lab.signals`` primitives. Returns ``None`` if the spec needs
    external/new data it cannot synthesize — it NEVER fabricates. No network.
    """
    spec = (spec or "").strip()
    if not spec:
        return None

    # First try the whole spec as a single term (e.g. "zscore(mid,5)", "mid").
    whole = _term(spec, panel)
    if whole is not None:
        return whole

    # Otherwise try a single binary op between two terms (left-associative split
    # on the LAST top-level operator). We only support flat A op B expressions;
    # richer nesting is exactly the kind of thing a richer injected provider can
    # handle.
    m = _BINOP_RE.match(spec)
    if m:
        left = _term(m.group(1), panel)
        right = _term(m.group(3), panel)
        if left is not None and right is not None:
            return _apply(m.group(2), left, right)

    return None


def fulfill(
    req: DataRequest,
    panel: Panel,
    *,
    provider=default_provider,
    path: str = DEFAULT_PATH,
) -> "tuple[Panel | None, DataRequest]":
    """Summon ``provider`` to satisfy ``req`` into the Panel — the agentic seam.

    ``provider(spec, panel)`` returns a length-``panel.n`` array if it can derive
    the field, else ``None``. On success the array is attached to a COPY of the
    panel under ``panel.features[field_name]`` (we do not mutate the caller's
    panel) and the request is marked ``fulfilled``; the copy is returned. On
    failure the request is marked ``NEEDS_DATA`` and the ORIGINAL panel is
    returned unchanged — never fabricated.

    The ``provider`` arg is the model-agnostic seam: inject a smarter offline
    fulfiller or an LLM-backed data agent exactly like ``scout``'s ``proposer``.
    If ``req`` has been filed (``req.id`` set and present in the log), its status
    is persisted; otherwise only the in-memory request object is updated.
    """
    arr = provider(req.spec, panel)

    if arr is None:
        updated = _mark(req, NEEDS_DATA,
                        {"reason": "not derivable from existing fields offline",
                         "provider": getattr(provider, "__name__", repr(provider))},
                        path)
        return panel, updated

    arr = np.asarray(arr, dtype=float)
    if arr.shape[0] != panel.n:
        # A provider that returns a wrong-length array is a contract violation,
        # not data — treat as unfulfillable rather than attaching garbage.
        updated = _mark(req, NEEDS_DATA,
                        {"reason": f"provider returned length {arr.shape[0]} != panel.n {panel.n}",
                         "provider": getattr(provider, "__name__", repr(provider))},
                        path)
        return panel, updated

    new_features = dict(panel.features)
    new_features[req.field_name] = arr
    new_panel = dataclasses.replace(panel, features=new_features)
    updated = _mark(req, FULFILLED,
                    {"field_name": req.field_name, "length": int(arr.shape[0]),
                     "provider": getattr(provider, "__name__", repr(provider))},
                    path)
    return new_panel, updated


def _mark(req: DataRequest, status: str, result: dict, path: str) -> DataRequest:
    """Persist a status/result transition if the request is logged; always
    return an updated in-memory request object."""
    if req.id:
        try:
            return update_request(req.id, status=status, result=result, path=path)
        except KeyError:
            pass  # not yet filed to this log — update in-memory only
    merged = dict(req.result)
    merged.update(result)
    return dataclasses.replace(req, status=status, result=merged, updated=_now())


def summon_for_hypothesis(
    h,
    *,
    field_name: str | None = None,
    spec: str | None = None,
    path: str = DEFAULT_PATH,
) -> DataRequest:
    """File the data request a ``NEEDS_DATA`` hypothesis implies — thin glue.

    Wires the "analyst submits a data-improvement request that summons a data
    agent" flow: given a hypothesis whose verdict is ``NEEDS_DATA``, derive a
    request whose ``spec`` is the hypothesis's ``signal_desc`` (or an explicit
    override) and file it via :func:`request`. The rationale carries the
    hypothesis's mechanism + id for traceability.
    """
    fname = field_name or _slug(getattr(h, "signal_desc", "") or getattr(h, "mechanism", "field"))
    req = DataRequest(
        market=getattr(h, "market", ""),
        field_name=fname,
        spec=spec or getattr(h, "signal_desc", "") or getattr(h, "mechanism", ""),
        rationale=(f"hypothesis {getattr(h, 'id', '') or h.hash()}: "
                   f"{getattr(h, 'mechanism', '')}"),
    )
    return request(req, path=path)


def _slug(text: str) -> str:
    """A compact, file-safe field name from free text."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", (text or "").strip().lower()).strip("_")
    return (s or "field")[:48]
