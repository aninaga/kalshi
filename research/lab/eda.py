"""research.lab.eda — idea-agnostic data scanners (raw material for the scout).

This module contains NO strategy ideas. It scans a market's Panels and surfaces
*where* the book deviates from calibration/efficiency — calibration error by
price bucket, the anchoring-gap distribution, quote staleness, ladder shape,
sample sizes. The scout hands these findings to an agent, which ORIGINATES
hypotheses from them. Machinery, not content.
"""
from __future__ import annotations

import numpy as np

from research.lab import signals
from research.lab.types import WINNER


def scan(panels: list, *, market: str | None = None) -> dict:
    """Aggregate idea-agnostic diagnostics over a list of Panels.

    Returns a JSON-able report: sample sizes, the anchoring-gap distribution,
    staleness, and (where the market admits a 0/1 outcome) a calibration table
    of realized vs implied by price bucket. Pure description — no recommendation.
    """
    panels = [p for p in panels if getattr(p, "n", 0)]
    if not panels:
        return {"market": market, "n_games": 0, "n_obs": 0, "notes": ["no panels"]}
    mkt = market or panels[0].market

    gaps, stales, mids, realized = [], [], [], []
    for p in panels:
        try:
            g = np.asarray(signals.anchoring_gap(p), float)
            s = np.asarray(signals.staleness_min(p), float)
        except Exception:  # noqa: BLE001 — a malformed panel must not sink the scan
            continue
        gaps.append(g[np.isfinite(g)])
        stales.append(s[np.isfinite(s)])
        if mkt == WINNER and p.home_won is not None:
            m = np.asarray(p.mid, float)
            ok = np.isfinite(m)
            mids.append(m[ok])
            realized.append(np.full(int(ok.sum()), float(p.home_won)))

    gap = np.concatenate(gaps) if gaps else np.array([])
    stale = np.concatenate(stales) if stales else np.array([])

    def _pct(a):
        if a.size == 0:
            return {}
        return {q: round(float(np.percentile(a, q)), 4) for q in (5, 25, 50, 75, 95)}

    report = {
        "market": mkt,
        "n_games": len(panels),
        "n_obs": int(sum(p.n for p in panels)),
        "anchoring_gap": {"mean": round(float(gap.mean()), 4) if gap.size else None,
                          "pctiles": _pct(gap)},
        "staleness_min": {"median": round(float(np.median(stale)), 3) if stale.size else None,
                          "pctiles": _pct(stale)},
        "calibration": _calibration_table(mids, realized) if mids else [],
        "notes": [],
    }
    # idea-agnostic flags the agent can reason over (NOT strategies):
    if report["calibration"]:
        worst = max(report["calibration"], key=lambda r: abs(r["bias"]))
        report["notes"].append(
            f"largest calibration bias {worst['bias']:+.3f} in price bucket "
            f"[{worst['lo']:.2f},{worst['hi']:.2f}) (n={worst['n']})")
    if gap.size and abs(report["anchoring_gap"]["mean"]) > 1e-6:
        report["notes"].append(
            f"anchoring gap mean {report['anchoring_gap']['mean']:+.3f} "
            f"(IQR {report['anchoring_gap']['pctiles'].get(25)}..{report['anchoring_gap']['pctiles'].get(75)})")
    return report


def _calibration_table(mids: list, realized: list, n_bins: int = 8) -> list:
    m = np.concatenate(mids)
    y = np.concatenate(realized)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    out = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        sel = (m >= lo) & (m < hi) if i < n_bins - 1 else (m >= lo) & (m <= hi)
        if sel.sum() < 30:
            continue
        out.append({"lo": round(float(lo), 3), "hi": round(float(hi), 3),
                    "n": int(sel.sum()), "implied": round(float(m[sel].mean()), 4),
                    "realized": round(float(y[sel].mean()), 4),
                    "bias": round(float(y[sel].mean() - m[sel].mean()), 4)})
    return out


def scan_market(market: str, *, split: str = "train", max_games: int | None = 400) -> dict:
    """Load cached Panels for ``market``/``split`` and scan them. Cache-only.

    Never reads the test split unless ``split == "test"`` is passed explicitly
    (delegated to ``lab.data.load_panels``).
    """
    from research.lab import data  # lazy: keep eda importable without the cache
    panels = data.load_panels(market, split=split, limit=max_games)
    return scan(panels, market=market)
