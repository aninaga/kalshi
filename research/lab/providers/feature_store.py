"""research.lab.providers.feature_store — durable, leakage-safe custom datasets.

The missing half of the data-request loop: ``lab.data_agent`` lets an analyst
FILE a request and a provider seam fulfill it in-memory, but a real external
feed (a forecast history, an injury wire, a macro print) must outlive one
process and reach every panel of a family. This module is that store.

Layout
------
One parquet per field per family::

    market_data/<family>/features/<field_name>.parquet

Schema (long, append-friendly)::

    event_id      str     the family's event key (Panel.game_id)
    known_from_ts int64   epoch seconds when this value became KNOWABLE
    value         float64

Point-in-time correctness is structural, not reviewed-in: ``join_panel``
attaches each field as an AS-OF series — at panel minute ``t`` the attached
value is the latest row with ``known_from_ts <= t`` (NaN before the first).
A data agent physically cannot leak a future value into an earlier bar by
writing rows; it can only lie about ``known_from_ts`` itself, which is what
the provenance note + ``lab.audit`` review are for. Rows valid from the start
of trading use the event's first quote minute (or 0).

Writers are data agents (via :func:`write_field`); readers are family
providers (via :func:`join_panel` in their ``load_panel``). Every write also
records provenance to ``<field_name>.provenance.json`` next to the parquet:
source, fetch time, the point-in-time rule used, and the writer's name.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]

_COLS = ("event_id", "known_from_ts", "value")


def features_dir(family: str) -> Path:
    return _ROOT / "market_data" / family / "features"


def list_fields(family: str) -> list[str]:
    d = features_dir(family)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.parquet"))


def write_field(family: str, field_name: str, df: pd.DataFrame, *,
                provenance: dict, mode: str = "replace") -> Path:
    """Persist one field's rows for a family (data-agent write path).

    ``df`` must carry exactly the columns ``event_id, known_from_ts, value``.
    ``provenance`` is REQUIRED and must say where the data came from and what
    rule sets ``known_from_ts`` (the point-in-time contract a reviewer audits).
    ``mode``: "replace" (default) or "append" (concat + dedupe on
    (event_id, known_from_ts), keeping the newest write).
    """
    missing = [c for c in _COLS if c not in df.columns]
    if missing:
        raise ValueError(f"feature df missing columns {missing}; need {_COLS}")
    if not provenance or not provenance.get("source"):
        raise ValueError("provenance with a non-empty 'source' is required — "
                         "an unprovenanced feed is unauditable")

    out = df.loc[:, list(_COLS)].copy()
    out["event_id"] = out["event_id"].astype(str)
    out["known_from_ts"] = out["known_from_ts"].astype("int64")
    out["value"] = out["value"].astype(float)

    d = features_dir(family)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{field_name}.parquet"
    if mode == "append" and path.exists():
        prev = pd.read_parquet(path)
        out = (pd.concat([prev, out], ignore_index=True)
                 .drop_duplicates(subset=["event_id", "known_from_ts"], keep="last"))
    elif mode not in ("replace", "append"):
        raise ValueError(f"mode must be 'replace' or 'append', got {mode!r}")
    out = out.sort_values(["event_id", "known_from_ts"]).reset_index(drop=True)
    out.to_parquet(path, index=False)

    prov = dict(provenance)
    prov.setdefault("written_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    prov.setdefault("rows", int(len(out)))
    prov.setdefault("point_in_time_rule", "known_from_ts = when the value became knowable")
    (d / f"{field_name}.provenance.json").write_text(json.dumps(prov, indent=2))
    return path


def _asof_series(rows: pd.DataFrame, minute_ts: np.ndarray) -> np.ndarray:
    """As-of join one event's (known_from_ts, value) rows onto panel minutes.

    out[i] = value of the latest row with known_from_ts <= minute_ts[i];
    NaN before the first row becomes knowable. Future rows are unreachable by
    construction — this is the leakage guarantee.
    """
    ts = rows["known_from_ts"].to_numpy(dtype="int64")
    vals = rows["value"].to_numpy(dtype=float)
    order = np.argsort(ts, kind="stable")
    ts, vals = ts[order], vals[order]
    idx = np.searchsorted(ts, np.asarray(minute_ts, dtype="int64"), side="right") - 1
    out = np.full(len(minute_ts), np.nan)
    ok = idx >= 0
    out[ok] = vals[idx[ok]]
    return out


# Per-process cache: one parquet read per (family, field) however many panels
# join it. Invalidated on file mtime change so a fresh data-agent write is
# picked up without a process restart.
_CACHE: dict = {}


def _load_field(family: str, field_name: str) -> pd.DataFrame | None:
    path = features_dir(family) / f"{field_name}.parquet"
    if not path.exists():
        return None
    key = (family, field_name)
    mtime = path.stat().st_mtime_ns
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    df = pd.read_parquet(path)
    _CACHE[key] = (mtime, df)
    return df


def join_panel(panel, family: str, fields: list[str] | None = None):
    """Attach every stored field (or ``fields``) to ``panel.features`` as-of.

    Mutates and returns ``panel`` (providers call this at the end of their
    ``load_panel``). Fields with no rows for this event attach as all-NaN only
    if some other event has rows (so feature names are stable across a family);
    a wholly-absent field is skipped. Never raises on a bad store — a broken
    feature file must not take down data loading.
    """
    names = fields if fields is not None else list_fields(family)
    for name in names:
        try:
            df = _load_field(family, name)
            if df is None or df.empty:
                continue
            rows = df[df["event_id"] == str(panel.game_id)]
            if rows.empty:
                panel.features[name] = np.full(panel.n, np.nan)
                continue
            panel.features[name] = _asof_series(rows, panel.minute_ts)
        except Exception:  # noqa: BLE001 — a broken field never breaks loading
            continue
    return panel
