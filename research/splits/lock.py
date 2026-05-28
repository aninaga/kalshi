"""
research/splits/lock.py
=======================
CLI script: compute and lock the chronological train/val/test split.

Usage:
    python -m research.splits.lock [--force]

Idempotency: if market_data/splits.json already exists, refuses to overwrite
unless --force is passed.  The split is meant to be locked once per project
lifetime; re-running with --force is a serious action.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Paths (relative to project root, i.e. cwd when invoked as a module)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GAMES_PARQUET = PROJECT_ROOT / "research" / "cache" / "games_v1.parquet"
SPLITS_JSON = PROJECT_ROOT / "market_data" / "splits.json"
LAKE_TEST_DIR = PROJECT_ROOT / "market_data" / "lake_test"
EXPECTED_SHA_FILE = LAKE_TEST_DIR / ".expected_sha256"
TEST_UNLOCKS_LOG = PROJECT_ROOT / "market_data" / "test_unlocks.log"
CACHE_DIR = PROJECT_ROOT / "research" / "cache"


def git_short_sha() -> str:
    """Return git short SHA, prefixed with 'dirty:' if the working tree is dirty."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
        ).decode().strip()
        # Check for dirty tree
        status = subprocess.check_output(
            ["git", "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
        ).decode().strip()
        if status:
            return f"dirty:{sha}"
        return sha
    except Exception:
        return "unknown"


def compute_lake_test_sha(test_game_ids: list[str]) -> str:
    """
    Compute SHA256 over all research/cache/*_v1.parquet files relevant to the
    test set, sorted by (table_name, game_id).

    Initially the per-game lake files may not exist; fall back to scanning
    the whole-dataset parquets in research/cache/ sorted by filename.

    Returns the hex digest string to write to .expected_sha256.
    """
    # Gather candidate parquet files: all *_v1.parquet in research/cache/
    cache_files = sorted(CACHE_DIR.glob("*_v1.parquet"))

    if not cache_files:
        print("WARNING: no *_v1.parquet files found in research/cache/; "
              ".expected_sha256 will reflect an empty hash.", file=sys.stderr)

    # Concatenate file bytes in sorted order, then hash the concatenation
    hasher = hashlib.sha256()
    for fp in cache_files:
        hasher.update(fp.read_bytes())

    return hasher.hexdigest()


def compute_splits(df: pd.DataFrame):
    """
    Sort games chronologically (date, then game_id), then partition:
      train: first 70%
      val:   next 15%
      test:  remaining 15%

    Day-clean rule: if the 70% or 85% index cut lands mid-day (i.e. the game
    immediately before the cut and the game immediately after share the same
    date), advance the cut to include ALL games on that shared date.

    Returns (train_ids, val_ids, test_ids, boundaries).
    """
    df_s = df.sort_values(["date", "game_id"]).reset_index(drop=True)
    n = len(df_s)

    n_train_raw = int(n * 0.70)
    n_val_raw = int(n * 0.15)

    # --- Train boundary (day-clean) ---
    train_last_date = df_s.iloc[n_train_raw - 1]["date"]
    first_after_train_date = df_s.iloc[n_train_raw]["date"] if n_train_raw < n else None

    if first_after_train_date is not None and train_last_date == first_after_train_date:
        # Advance: include all games on that date in train
        n_train = int((df_s["date"] <= train_last_date).sum())
    else:
        n_train = n_train_raw

    train_end_date = df_s.iloc[n_train - 1]["date"]

    # --- Val boundary (day-clean), starting from adjusted train end ---
    n_val_end_raw = n_train + n_val_raw
    if n_val_end_raw >= n:
        n_val_end = n - 1  # edge case: clamp
    else:
        val_last_date = df_s.iloc[n_val_end_raw - 1]["date"]
        first_after_val_date = df_s.iloc[n_val_end_raw]["date"]
        if val_last_date == first_after_val_date:
            n_val_end = int((df_s["date"] <= val_last_date).sum())
        else:
            n_val_end = n_val_end_raw

    val_end_date = df_s.iloc[n_val_end - 1]["date"]
    test_end_date = df_s.iloc[-1]["date"]

    train_ids = df_s.iloc[:n_train]["game_id"].tolist()
    val_ids = df_s.iloc[n_train:n_val_end]["game_id"].tolist()
    test_ids = df_s.iloc[n_val_end:]["game_id"].tolist()

    boundaries = {
        "train_end_date": train_end_date,
        "val_end_date": val_end_date,
        "test_end_date": test_end_date,
    }

    return train_ids, val_ids, test_ids, boundaries


def main():
    parser = argparse.ArgumentParser(
        description="Lock the chronological train/val/test split."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing splits.json (DANGEROUS — split should be locked once).",
    )
    args = parser.parse_args()

    # --- Idempotency guard ---
    if SPLITS_JSON.exists() and not args.force:
        print(
            f"ERROR: {SPLITS_JSON} already exists.\n"
            "The split is meant to be locked once for the project lifetime.\n"
            "Pass --force to overwrite (this is a serious action).",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Load games ---
    if not GAMES_PARQUET.exists():
        print(f"ERROR: games parquet not found: {GAMES_PARQUET}", file=sys.stderr)
        sys.exit(2)

    print(f"Loading games from {GAMES_PARQUET} ...")
    df = pd.read_parquet(GAMES_PARQUET, columns=["game_id", "date"])
    print(f"  Loaded {len(df)} games.")

    # --- Compute split ---
    train_ids, val_ids, test_ids, boundaries = compute_splits(df)
    print(
        f"  Split: train={len(train_ids)}, val={len(val_ids)}, test={len(test_ids)} "
        f"(total={len(train_ids)+len(val_ids)+len(test_ids)})"
    )
    print(f"  Boundaries: {boundaries}")

    # --- SHA256 of test-set parquets (cache files for now) ---
    print("Computing SHA256 of test-set parquets ...")
    sha256_hex = compute_lake_test_sha(test_ids)
    print(f"  sha256={sha256_hex[:16]}...")

    # --- Git SHA ---
    locked_by = git_short_sha()
    print(f"  locked_by={locked_by}")

    # --- Write splits.json ---
    SPLITS_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "train_game_ids": train_ids,
        "val_game_ids": val_ids,
        "test_game_ids": test_ids,
        "boundaries": boundaries,
        "locked_at_ts": int(time.time()),
        "locked_by": locked_by,
        "n_train": len(train_ids),
        "n_val": len(val_ids),
        "n_test": len(test_ids),
    }
    with open(SPLITS_JSON, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"Wrote {SPLITS_JSON}")

    # --- Write .expected_sha256 ---
    LAKE_TEST_DIR.mkdir(parents=True, exist_ok=True)
    EXPECTED_SHA_FILE.write_text(sha256_hex + "\n")
    print(f"Wrote {EXPECTED_SHA_FILE}")

    # --- Touch test_unlocks.log ---
    if not TEST_UNLOCKS_LOG.exists():
        TEST_UNLOCKS_LOG.write_text(
            "# format: <iso_ts> <spec_hash> <attempt|burn> <reason>\n"
        )
        print(f"Wrote {TEST_UNLOCKS_LOG}")
    else:
        print(f"  {TEST_UNLOCKS_LOG} already exists, not overwriting.")

    print("Done. Split locked successfully.")


if __name__ == "__main__":
    main()
