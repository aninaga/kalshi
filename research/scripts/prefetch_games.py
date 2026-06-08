"""Parallel game prefetcher — build the per-game pkl cache concurrently.

The serial ``batch.sub_reactions`` loop builds one game at a time (~60-90s each
on a cold fetch), which is far too slow for a season-scale study. Every game is
independent and cached to its own pkl, so we fan the cold builds out across a
thread pool (the work is network-bound: ESPN PBP + Kalshi candles + Polymarket
history). Already-cached games are skipped instantly.

Usage::

    python3 -m research.scripts.prefetch_games --start 2025-10-21 --end 2026-01-15 \
        --workers 12

Writes nothing but the pkl cache under market_data/nba_studies/_cache/.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nba_odds_study import batch, schedule  # noqa: E402

# Default moneyline-only (win-prob signal). Pass --kinds total (or winner,total)
# to build the over/under markets. The cache key includes kinds, so each kind
# set is a distinct shard — keep downstream consumers in sync.
KINDS = {"winner"}


def _build_one(game: dict, kinds: set[str]) -> tuple[dict, bool, str]:
    try:
        d = batch.load_or_build(game, kinds=kinds)
        n_sub = len(d.subs)
        # "usable" = has the price series this kind set needs.
        if "total" in kinds and "winner" not in kinds:
            ok_cols = [c for c in d.minute.columns if "implied_total" in c or c == "proj_total"]
            usable = bool(len(d.odds_long)) if hasattr(d, "odds_long") else True
        else:
            wp_cols = [c for c in ("kalshi_home_winprob", "pm_home_winprob") if c in d.minute]
            usable = bool(wp_cols) and d.minute[wp_cols].notna().any().any()
        return game, usable, f"{n_sub} subs usable={usable}"
    except Exception as e:  # noqa: BLE001
        return game, False, f"FAIL {str(e)[:70]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--kinds", default="winner", help="comma-sep: winner,total")
    a = ap.parse_args()
    kinds = {k.strip() for k in a.kinds.split(",") if k.strip()}

    games = schedule.completed_games(a.start, a.end)
    if a.limit:
        games = games[: a.limit]
    print(f"{len(games)} completed games in {a.start}..{a.end}; kinds={sorted(kinds)} "
          f"workers={a.workers}", flush=True)

    t0 = time.time()
    ok = wp = 0
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(_build_one, g, kinds): g for g in games}
        for fut in as_completed(futs):
            game, has_wp, msg = fut.result()
            done += 1
            if "FAIL" not in msg:
                ok += 1
            if has_wp:
                wp += 1
            if done % 10 == 0 or "FAIL" in msg:
                el = time.time() - t0
                rate = done / el if el else 0
                eta = (len(games) - done) / rate if rate else 0
                print(f"  [{done}/{len(games)}] ok={ok} wp={wp} "
                      f"{game['date']} {game['away']}@{game['home']}: {msg} "
                      f"| {rate:.2f} g/s eta {eta/60:.1f}m", flush=True)

    print(f"\nDONE: {ok}/{len(games)} built, {wp} with winprob, in {(time.time()-t0)/60:.1f} min",
          flush=True)


if __name__ == "__main__":
    main()
