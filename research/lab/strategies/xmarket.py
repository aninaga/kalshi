"""research.lab.strategies.xmarket — cross-market internal-consistency strategy.

Mechanism (cross-market, distinct from the spreads-anchoring family)
--------------------------------------------------------------------
For the SAME game and the SAME elapsed minute the three NBA books imply
overlapping quantities. In particular the **spread** ladder directly prices
``P(home_margin > strike)``; evaluated at ``strike = 0`` that IS
``P(home_margin > 0) = P(home win)`` (ties are impossible in the NBA). So the
spread book carries its OWN implied home-win probability, with **no fitted
margin->winprob calibration required** — the mapping is parameter-free and
point-in-time, which is precisely what dodges the look-ahead trap that kills
naive cross-market edges (fitting a margin->winprob curve on whole-game data).

That spread-implied ``P(win)`` should equal the **winner** market's mid (a
direct ``P(home win)``). When the two diverge, EDA shows the winner book is the
better-calibrated / leading book (lower Brier vs the realized outcome), so the
spread-implied estimate is the laggier, noisier one.

Pre-registered direction: **fade the spread book toward the winner book.** When
``spread_pwin`` sits BELOW the winner mid by a threshold, the spread book is
under-pricing home's win chance -> take the spread contract that pays if home
wins (``cover_home`` at the strike nearest 0, i.e. ``P(margin>0)``). When
``spread_pwin`` sits ABOVE the winner mid, take the complement (``cover_away``).
Held to settlement, the winner mid is the superior outcome estimate, so the
convergence trade should carry a small positive edge IF the divergence is real
and not inside the bid/ask.

Execution honesty (the two failure modes this lane must avoid)
--------------------------------------------------------------
1. **No fitted calibration** => no calibration look-ahead. The spread->winprob
   map is the identity read of the ladder at strike 0.
2. **Single-leg, realistic fills.** We trade the SPREAD leg ONLY, filled by the
   substrate's REALISTIC ``FillModel`` (snap to the listed strike nearest 0, its
   real quoted prob, half-spread, PM taker fee). We NEVER assume we also trade
   the winner leg at mid — the winner mid is used only as the *reference* the
   spread is faded toward, never as a fill. This makes the two-leg-fill trap
   structurally impossible: there is one real fill.

The winner mid is joined onto each spread Panel as a feature
(``winner_pwin``) aligned by ``elapsed_sec``; the divergence
``spread_pwin - winner_pwin`` is the signal. Both are point-in-time (each minute
reads only that minute's quotes), so there is no look-ahead.
"""
from __future__ import annotations

import numpy as np

from research.lab.strategy import Strategy
from research.lab.types import Panel

_ELAPSED_TOL_SEC = 90.0   # max |elapsed| gap when aligning a winner bar to a spread bar


def spread_implied_pwin(panel: Panel) -> np.ndarray:
    """Per-minute ``P(home_margin > 0)`` read off the spread ladder at strike 0.

    The ladder maps ``strike -> P(margin > strike)`` (decreasing in strike). We
    interpolate that curve to ``strike = 0`` each minute, using only the finite,
    bracketing quotes available at that minute. Parameter-free: no fitted
    margin->winprob model, so no look-ahead. Returns NaN where fewer than two
    finite quotes exist that minute.
    """
    strikes = np.array(sorted(panel.ladder.keys()), dtype=float)
    n = panel.n
    out = np.full(n, np.nan, dtype=float)
    if strikes.size < 2:
        return out
    grid = np.vstack([np.asarray(panel.ladder[s], dtype=float) for s in strikes])  # (k, n)
    for i in range(n):
        ys = grid[:, i]
        ok = np.isfinite(ys)
        if ok.sum() < 2:
            continue
        xs, yy = strikes[ok], ys[ok]
        order = np.argsort(xs)
        out[i] = float(np.interp(0.0, xs[order], yy[order]))
    return np.clip(out, 0.0, 1.0)


def attach_winner_mid(spread_panels: list[Panel], winner_panels: list[Panel]) -> list[Panel]:
    """Return spread panels with two extra features attached, joined by game_id.

    Adds, point-in-time and aligned by ``elapsed_sec`` (nearest winner bar within
    ``_ELAPSED_TOL_SEC``):

    * ``winner_pwin`` — the winner market mid (direct P(home win)) at that minute;
    * ``spread_pwin`` — the spread-implied P(home win) from :func:`spread_implied_pwin`;
    * ``xmkt_div``    — ``spread_pwin - winner_pwin`` (the divergence signal).

    Spread panels with no matching winner game are dropped (we cannot form the
    cross-market signal for them). No outcome fields are read, so no leakage.
    """
    win_by_gid = {p.game_id: p for p in winner_panels}
    out: list[Panel] = []
    for sp in spread_panels:
        wp = win_by_gid.get(sp.game_id)
        if wp is None:
            continue
        sps = spread_implied_pwin(sp)
        w_elapsed = np.asarray(wp.elapsed_sec, dtype=float)
        w_mid = np.asarray(wp.mid, dtype=float)
        winner_pwin = np.full(sp.n, np.nan, dtype=float)
        for i in range(sp.n):
            e = sp.elapsed_sec[i]
            if not np.isfinite(e):
                continue
            j = int(np.argmin(np.abs(w_elapsed - e)))
            if abs(w_elapsed[j] - e) > _ELAPSED_TOL_SEC:
                continue
            if np.isfinite(w_mid[j]):
                winner_pwin[i] = w_mid[j]
        feats = dict(sp.features)
        feats["spread_pwin"] = sps
        feats["winner_pwin"] = winner_pwin
        feats["xmkt_div"] = sps - winner_pwin
        out.append(
            Panel(
                game_id=sp.game_id, date=sp.date, market=sp.market,
                home_team=sp.home_team, away_team=sp.away_team,
                minute_ts=sp.minute_ts, elapsed_sec=sp.elapsed_sec,
                margin=sp.margin, total=sp.total, mid=sp.mid, ladder=sp.ladder,
                features=feats, home_won=sp.home_won,
                final_total=sp.final_total, final_margin=sp.final_margin,
                split=sp.split,
            )
        )
    return out


def xmarket_consistency(threshold: float = 0.08) -> Strategy:
    """Fade the spread book toward the winner book on a cross-market divergence.

    Entry: a fresh spread minute where ``|spread_pwin - winner_pwin| >= threshold``.
    Side: if the spread under-prices home (``div < 0``), take ``cover_home``
    (the spread contract paying if ``margin > strike_near_0``); if it over-prices
    home (``div > 0``), take ``cover_away``. Held to settlement; single realistic
    fill on the spread leg only.

    ``threshold`` is the pre-registered minimum divergence (prob units). The
    direction is fully pre-registered (fade toward the winner book) — sign is
    fixed by ``div``, not fit.
    """
    def entry(p: Panel) -> np.ndarray:
        div = np.asarray(p.features.get("xmkt_div"), dtype=float)
        return np.isfinite(div) & (np.abs(div) >= threshold)

    def side(p: Panel, i: int) -> str:
        div = float(p.features["xmkt_div"][i])
        # div < 0: spread under-prices home win -> winner book says home more likely
        #          -> bet home covers (margin > 0) => cover_home.
        # div > 0: spread over-prices home win -> bet away => cover_away.
        return "cover_home" if div < 0 else "cover_away"

    return Strategy(
        name=f"xmarket_consistency(thr={threshold})",
        entry=entry,
        side=side,
        exit="settlement",
        min_elapsed=600.0,
        max_elapsed=2520.0,
        max_stale_min=2.0,
    )
