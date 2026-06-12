"""research.nba_odds_study.cache_hash — content-hash freeze for the study cache.

WHY (data-repro freeze, 2026-06-12): re-fetching the historical odds caches
shifted lab verdicts by ~1.7c on *identical code* — the input data drifted
under us (a vendor re-stated a candle, a fetch landed a different last-known
quote), so a backtest was no longer reproducible from its pkl. Pickle mtimes do
not catch this; the bytes can change while the filename is stable.

This module gives each built :class:`~nba_odds_study.dataset.Dataset` a stable
CONTENT hash over the two arrays a lab Panel is built from — ``odds_long`` (the
per-minute strike-ladder quotes) and ``minute`` (the per-minute score/elapsed
frame) — and writes it to a sidecar ``<pkl>.sha256`` on build. A later checker
recomputes the hash from an existing pkl and WARNS if it drifts from the
recorded sidecar.

It is deliberately **opt-in and non-breaking**: ``write_sidecar`` and
``check_sidecar`` never raise on a content mismatch and never block a build —
they record and expose. Wiring lives in ``batch.load_or_build`` (writes a
sidecar on a fresh build; checks-and-warns on a cache hit). Nothing here touches
``market_data/lake_test`` or ``splits.json``.

Public API::

    content_hash(dataset) -> str          # 64-hex sha256 of odds_long + minute
    sidecar_path(pkl_path) -> str
    write_sidecar(pkl_path, dataset) -> str
    read_sidecar(pkl_path) -> str | None
    check_sidecar(pkl_path, dataset, *, warn=...) -> (ok, recorded, current)
"""
from __future__ import annotations

import hashlib
import os
import warnings
from typing import Optional, Tuple

import pandas as pd

SIDECAR_SUFFIX = ".sha256"
# The exact arrays a lab Panel is built from. Hashing only these makes the hash
# insensitive to incidental Dataset bookkeeping (subs/runs ordering) while
# binding the two frames that actually move a verdict.
_HASHED_ATTRS = ("odds_long", "minute")


def _frame_bytes(obj) -> bytes:
    """Deterministic bytes for one hashed Dataset attribute.

    DataFrames are canonicalised (sorted columns, then sorted rows) so an
    incidental row/column REORDER is not flagged as content drift, while any
    change to a cell value, an added/removed row, or a changed index IS. A
    missing/empty attribute hashes to a stable sentinel.
    """
    if obj is None:
        return b"<none>"
    if isinstance(obj, pd.DataFrame):
        df = obj
        if df.empty:
            # Still bind the column identity of an empty frame.
            return ("<empty-df>:" + ",".join(map(str, sorted(map(str,
                    df.columns))))).encode()
        # A NAMED index carries data (e.g. ``minute`` is keyed on the minute
        # timestamp) — fold it in as a column so a re-stated key IS detected.
        # An unnamed default RangeIndex is just positional bookkeeping; dropping
        # it makes the hash invariant to a pure ROW reorder.
        if df.index.name is not None or not isinstance(df.index, pd.RangeIndex):
            df = df.reset_index()
        df = df.reindex(sorted(df.columns, key=str), axis=1)
        try:
            df = df.sort_values(list(df.columns), kind="mergesort")
        except TypeError:
            df = df.sort_index(kind="mergesort")
        df = df.reset_index(drop=True)
        payload = pd.util.hash_pandas_object(df, index=False).values.tobytes()
        cols = ",".join(map(str, df.columns)).encode()
        return cols + b"|" + payload
    if isinstance(obj, pd.Series):
        return pd.util.hash_pandas_object(obj, index=True).values.tobytes()
    # Fallback for any unexpected type: its repr (stable for primitives).
    return repr(obj).encode()


def content_hash(dataset) -> str:
    """Stable 64-hex sha256 over the Dataset's ``odds_long`` + ``minute``.

    Two Datasets with byte-identical odds-quote and minute content produce the
    same hash regardless of row/column order; any cell, row-set, or index change
    flips it. Does not depend on code version — purely a function of the data.
    """
    h = hashlib.sha256()
    for attr in _HASHED_ATTRS:
        h.update(attr.encode())
        h.update(b"=")
        h.update(_frame_bytes(getattr(dataset, attr, None)))
        h.update(b";")
    return h.hexdigest()


def sidecar_path(pkl_path: str) -> str:
    """Path of the sidecar hash file for a cache pkl (``<pkl>.sha256``)."""
    return str(pkl_path) + SIDECAR_SUFFIX


def write_sidecar(pkl_path: str, dataset) -> str:
    """Compute and persist the content hash next to ``pkl_path``; return it.

    Best-effort: a write failure (read-only dir, race) is swallowed — the freeze
    is an evidence aid, never a build dependency.
    """
    digest = content_hash(dataset)
    try:
        with open(sidecar_path(pkl_path), "w", encoding="utf-8") as fh:
            fh.write(digest + "\n")
    except OSError:
        pass
    return digest


def read_sidecar(pkl_path: str) -> Optional[str]:
    """Return the recorded content hash for ``pkl_path``, or ``None`` if absent."""
    sp = sidecar_path(pkl_path)
    if not os.path.exists(sp):
        return None
    try:
        with open(sp, "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def check_sidecar(pkl_path: str, dataset, *, warn: bool = True
                  ) -> Tuple[bool, Optional[str], str]:
    """Compare ``dataset``'s content hash to the recorded sidecar.

    Returns ``(ok, recorded, current)``. ``ok`` is True when there is no
    recorded hash yet (nothing to drift from) OR the recorded hash matches the
    current content; it is False ONLY on a real drift. NEVER raises and never
    blocks a build — on drift it emits a ``UserWarning`` (when ``warn``) so the
    operator sees that a cached pkl's odds/minute content changed under
    identical code, then returns False for callers that want to act on it.
    """
    current = content_hash(dataset)
    recorded = read_sidecar(pkl_path)
    if recorded is None:
        return True, None, current
    if recorded == current:
        return True, recorded, current
    if warn:
        warnings.warn(
            f"nba_odds_study cache content drift for {os.path.basename(pkl_path)}: "
            f"recorded sha256 {recorded[:12]}… != current {current[:12]}… — "
            f"the cached odds_long/minute changed under identical code; a "
            f"verdict built from this pkl is no longer reproducible. "
            f"Re-freeze with cache_hash.write_sidecar if the re-fetch is "
            f"intentional.",
            UserWarning,
            stacklevel=2,
        )
    return False, recorded, current


__all__ = [
    "SIDECAR_SUFFIX",
    "content_hash",
    "sidecar_path",
    "write_sidecar",
    "read_sidecar",
    "check_sidecar",
]
