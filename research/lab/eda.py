"""research.lab.eda — idea-agnostic data scanners (raw material for the scout).

This module contains NO strategy ideas. It scans a market's Panels and surfaces
*where* the book deviates from calibration/efficiency, and through WHICH lenses —
so the scout has more structure to notice. It reports, purely descriptively:

  * the anchoring-gap distribution and quote staleness;
  * a calibration table (realized vs implied by price bucket);
  * DERIVED LENSES (EDA v2): operator-composition features built from the existing
    panel fields (ratios / deltas / rolling / interactions / z-scores), ranked by
    how strongly each one *segments the realized-vs-line outcome* — i.e. where the
    feature vocabulary the scout is shown is widened so it can notice non-obvious
    structure WITHOUT any new raw data;
  * REGIMES (EDA v2): unsupervised clusters of in-game states, with per-regime
    calibration/gap/outcome stats, so conditional ("in regime A the relationship
    differs") structure becomes legible.

Everything here is description, never a recommendation. The scout originates the
hypotheses; this module only expands what is visible to it.
"""
from __future__ import annotations

import numpy as np

from research.lab import signals
from research.lab.types import SPREAD, TOTAL, WINNER

# Cap on minute-rows pulled into the matrix ops (lens/regime), for speed.
_MAX_ROWS = 60_000


def _market_gap(panel) -> np.ndarray | None:
    """Market-aware anchoring gap (projection − implied level), or None.

    ``signals.anchoring_gap`` projects from TOTAL points (its contract), so it
    is only meaningful for the totals book. For SPREAD we project the signed
    margin; WINNER has no pace-anchoring gap (its diagnostic is calibration).
    """
    if panel.market == TOTAL:
        return np.asarray(signals.anchoring_gap(panel), float)
    if panel.market == SPREAD:
        dur = getattr(panel, "duration_sec", 2880.0)
        if dur is None:  # untimed event class: no pace, no anchoring gap
            return None
        e = np.asarray(panel.elapsed_sec, float)
        mid = np.asarray(panel.mid, float)
        margin = np.asarray(panel.margin, float)
        with np.errstate(divide="ignore", invalid="ignore"):
            proj = np.where(e > 120.0, margin * float(dur) / e, np.nan)
        return proj - mid
    return None  # WINNER: no anchoring gap


def _zscore_in_game(x: np.ndarray) -> np.ndarray:
    m = np.nanmean(x) if np.isfinite(x).any() else 0.0
    s = np.nanstd(x)
    return (x - m) / s if s and np.isfinite(s) else np.zeros_like(x)


def _derived_features(panel) -> dict:
    """Operator-composition lenses over the EXISTING panel fields (no new data).

    Ratios / deltas / rolling / interactions / z-scores — interpretable names,
    each a plain numpy array of length n. This is the widened lens vocabulary.
    """
    e = np.asarray(panel.elapsed_sec, float)
    margin = np.asarray(panel.margin, float)
    total = np.asarray(panel.total, float)
    mid = np.asarray(panel.mid, float)
    mins = e / 60.0 + 1.0
    feats = panel.features or {}
    lead = np.asarray(feats.get("lead_changes", np.zeros_like(e)), float)

    def _delta(a, k=3):
        out = np.full_like(a, np.nan)
        out[k:] = a[k:] - a[:-k]
        return out

    def _roll_std(a, w=5):
        out = np.full_like(a, np.nan)
        for i in range(len(a)):
            lo = max(0, i - w + 1)
            out[i] = np.nanstd(a[lo:i + 1])
        return out

    feats_out = {
        "abs_margin": np.abs(margin),
        "margin_per_min": margin / mins,
        "total_per_min": total / mins,
        "mid_zscore": _zscore_in_game(mid),
        "margin_zscore": _zscore_in_game(margin),
        "mid_delta3": _delta(mid, 3),
        "margin_delta3": _delta(margin, 3),
        "mid_roll_std5": _roll_std(mid, 5),
        "lead_change_rate": lead / mins,
        "absmargin_x_mid": np.abs(margin) * mid,
    }
    # Clock-based lenses only exist for TIMED event classes.
    dur = getattr(panel, "duration_sec", 2880.0)
    if dur is not None:
        dur = float(dur)
        feats_out["time_remaining"] = np.maximum(dur - e, 0.0)
        feats_out["margin_x_timeleft"] = margin * np.maximum(dur - e, 0.0) / dur
    return feats_out


def _residual_label(panel) -> np.ndarray | None:
    """Per-minute calibration RESIDUAL (realized − implied), idea-agnostic.

    The residual is what the market is NOT already pricing, so lenses ranked
    against it surface predictability BEYOND the price (otherwise, e.g., margin
    would trivially "predict" the winner the book has already priced).
      WINNER:        home_won − implied_home_winprob (panel.mid)
      TOTAL/SPREAD:  1{final > implied_level} − 0.5  (the ATM line implies 0.5)
    """
    mid = np.asarray(panel.mid, float)
    if panel.market == WINNER:
        if panel.home_won is None:
            return None
        return float(panel.home_won) - mid
    final = panel.final_total if panel.market == TOTAL else panel.final_margin
    if final is None:
        return None
    with np.errstate(invalid="ignore"):
        return (final > mid).astype(float) - 0.5


def _collect_rows(panels: list, market: str):
    """Stack derived features + outcome + gap over (subsampled) minute rows."""
    feat_rows: dict[str, list] = {}
    ys: list = []
    gaps: list = []
    states: list = []
    for p in panels:
        y = _residual_label(p)        # target = calibration residual (realized - implied)
        if y is None:
            continue
        feats = _derived_features(p)
        g = _market_gap(p)
        g = np.asarray(g, float) if g is not None else np.full(p.n, np.nan)
        e = np.asarray(p.elapsed_sec, float)
        for k, v in feats.items():
            feat_rows.setdefault(k, []).append(np.asarray(v, float))
        ys.append(y)
        gaps.append(g)
        # compact state vector for regime clustering
        states.append(np.column_stack([
            e, feats["abs_margin"], feats["margin_per_min"], feats["total_per_min"],
            np.asarray(p.mid, float), feats["lead_change_rate"]]))
    if not ys:
        return None
    X = {k: np.concatenate(v) for k, v in feat_rows.items()}
    y = np.concatenate(ys)
    gap = np.concatenate(gaps)
    S = np.concatenate(states, axis=0)
    if len(y) > _MAX_ROWS:                       # subsample deterministically
        idx = np.linspace(0, len(y) - 1, _MAX_ROWS).astype(int)
        X = {k: v[idx] for k, v in X.items()}
        y, gap, S = y[idx], gap[idx], S[idx]
    return X, y, gap, S


def lens_informativeness(X: dict, resid: np.ndarray, top: int = 6) -> list:
    """Rank derived lenses by how strongly they predict the calibration RESIDUAL.

    For each feature: |corr| with the residual (predictability BEYOND the price),
    and the mean residual in its top vs bottom decile. Pure description — where
    un-priced structure lives, for the scout to interpret; never a trade.
    """
    out = []
    fin_y = np.isfinite(resid)
    for name, v in X.items():
        m = fin_y & np.isfinite(v)
        if m.sum() < 200 or np.nanstd(v[m]) == 0:
            continue
        vv, rr = v[m], resid[m]
        corr = float(np.corrcoef(vv, rr)[0, 1])
        lo_q, hi_q = np.quantile(vv, 0.1), np.quantile(vv, 0.9)
        bot = rr[vv <= lo_q].mean() if (vv <= lo_q).any() else np.nan
        topr = rr[vv >= hi_q].mean() if (vv >= hi_q).any() else np.nan
        out.append({"lens": name, "corr_with_residual": round(corr, 4),
                    "bottom_decile_residual": round(float(bot), 4),
                    "top_decile_residual": round(float(topr), 4),
                    "residual_spread": round(float(topr - bot), 4), "n": int(m.sum())})
    out.sort(key=lambda r: abs(r["corr_with_residual"]), reverse=True)
    return out[:top]


def regime_scan(S: np.ndarray, resid: np.ndarray, gap: np.ndarray, k: int = 4) -> list:
    """Unsupervised in-game regimes (kmeans), with per-regime RESIDUAL/gap stats.

    ``mean_residual`` is how mis-priced the line is in that regime (realized −
    implied), so the scout can notice conditional structure the global stats hide.
    """
    try:
        from sklearn.cluster import KMeans
        from sklearn.preprocessing import StandardScaler
    except Exception:  # noqa: BLE001 — sklearn optional
        return []
    ok = np.isfinite(S).all(axis=1) & np.isfinite(resid)
    if ok.sum() < max(200, k * 50):
        return []
    Sx = StandardScaler().fit_transform(S[ok])
    labels = KMeans(n_clusters=k, n_init=4, random_state=0).fit_predict(Sx)
    rr, gg, Sok = resid[ok], gap[ok], S[ok]
    out = []
    for c in range(k):
        sel = labels == c
        if sel.sum() < 50:
            continue
        gsel = gg[sel][np.isfinite(gg[sel])]
        out.append({
            "regime": c, "n": int(sel.sum()),
            "mean_elapsed_min": round(float(Sok[sel, 0].mean() / 60.0), 1),
            "mean_abs_margin": round(float(Sok[sel, 1].mean()), 1),
            "mean_residual": round(float(rr[sel].mean()), 4),
            "mean_gap": round(float(gsel.mean()), 3) if gsel.size else None,
        })
    return out


def scan(panels: list, *, market: str | None = None) -> dict:
    """Aggregate idea-agnostic diagnostics over a list of Panels (EDA v2).

    Returns a JSON-able report: sample sizes, anchoring-gap distribution,
    staleness, a calibration table, plus the widened-vocabulary lenses
    (``derived_lenses``) and unsupervised ``regimes``. Pure description.
    """
    panels = [p for p in panels if getattr(p, "n", 0)]
    if not panels:
        return {"market": market, "n_games": 0, "n_obs": 0, "notes": ["no panels"]}
    mkt = market or panels[0].market

    gaps, stales, mids, realized = [], [], [], []
    for p in panels:
        try:
            g = _market_gap(p)
            s = np.asarray(signals.staleness_min(p), float)
        except Exception:  # noqa: BLE001 — a malformed panel must not sink the scan
            continue
        if g is not None:
            g = np.asarray(g, float)
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

    # EDA v2: widened lenses + regimes (operator-composition + clustering).
    derived_lenses: list = []
    regimes: list = []
    collected = _collect_rows(panels, mkt)
    if collected is not None:
        Xc, yc, gapc, Sc = collected
        derived_lenses = lens_informativeness(Xc, yc)
        regimes = regime_scan(Sc, yc, gapc)

    report = {
        "market": mkt,
        "n_games": len(panels),
        "n_obs": int(sum(p.n for p in panels)),
        "anchoring_gap": {"mean": round(float(gap.mean()), 4) if gap.size else None,
                          "pctiles": _pct(gap)},
        "staleness_min": {"median": round(float(np.median(stale)), 3) if stale.size else None,
                          "pctiles": _pct(stale)},
        "calibration": _calibration_table(mids, realized) if mids else [],
        "derived_lenses": derived_lenses,
        "regimes": regimes,
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
    if derived_lenses:
        L = derived_lenses[0]
        report["notes"].append(
            f"lens '{L['lens']}' most predicts the realized-vs-line residual "
            f"(corr {L['corr_with_residual']:+.3f}, top/bottom-decile residual "
            f"{L['top_decile_residual']:+.3f}/{L['bottom_decile_residual']:+.3f})")
    if regimes:
        r = max(regimes, key=lambda z: abs(z["mean_residual"]))
        report["notes"].append(
            f"regime {r['regime']} (~{r['mean_elapsed_min']}min, |margin|~{r['mean_abs_margin']}, "
            f"n={r['n']}) is the most mis-priced (mean residual {r['mean_residual']:+.3f})")
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
