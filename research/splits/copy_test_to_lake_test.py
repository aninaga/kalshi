"""
research/splits/copy_test_to_lake_test.py
==========================================
Helper: copy per-game parquet files for test-set games from
market_data/lake/v1/{pbp,kalshi_ticks,polymarket_ticks,games,subs}/<game_id>.parquet
into market_data/lake_test/{table}/<game_id>.parquet, then recompute
.expected_sha256.

Usage:
    python -m research.splits.copy_test_to_lake_test [--dry-run]

Safe to run when the lake is not yet populated — prints a message and exits 0.
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SPLITS_JSON = PROJECT_ROOT / "market_data" / "splits.json"
LAKE_V1_DIR = PROJECT_ROOT / "market_data" / "lake" / "v1"
LAKE_TEST_DIR = PROJECT_ROOT / "market_data" / "lake_test"
EXPECTED_SHA_FILE = LAKE_TEST_DIR / ".expected_sha256"

LAKE_TABLES = ["pbp", "kalshi_ticks", "polymarket_ticks", "games", "subs"]


def compute_sha256_of_lake_test() -> str:
    """
    Compute SHA256 over all parquet files in lake_test, sorted by
    (table_name, game_id), then return the hex digest.
    """
    candidates = []
    for fp in sorted(LAKE_TEST_DIR.rglob("*.parquet")):
        # derive (table_name, game_id) from path: lake_test/<table>/<game_id>.parquet
        parts = fp.relative_to(LAKE_TEST_DIR).parts
        table_name = parts[0] if len(parts) >= 2 else ""
        game_id = fp.stem
        candidates.append((table_name, game_id, fp))

    candidates.sort(key=lambda x: (x[0], x[1]))

    hasher = hashlib.sha256()
    for _, _, fp in candidates:
        hasher.update(fp.read_bytes())

    return hasher.hexdigest()


def main():
    parser = argparse.ArgumentParser(
        description="Copy test-set game parquets from lake to lake_test."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be copied without actually copying.",
    )
    args = parser.parse_args()

    # --- Load splits ---
    if not SPLITS_JSON.exists():
        print(
            f"ERROR: {SPLITS_JSON} not found. Run `python -m research.splits.lock` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(SPLITS_JSON) as fh:
        splits = json.load(fh)

    test_game_ids: list[str] = splits["test_game_ids"]
    print(f"Test set: {len(test_game_ids)} games.")

    # --- Check if lake exists ---
    if not LAKE_V1_DIR.exists():
        print(
            f"lake not yet populated; rerun after import_cache\n"
            f"  (expected lake directory: {LAKE_V1_DIR})"
        )
        sys.exit(0)

    # --- Copy files ---
    copied = 0
    missing = 0

    for table in LAKE_TABLES:
        src_table_dir = LAKE_V1_DIR / table
        dst_table_dir = LAKE_TEST_DIR / table

        if not src_table_dir.exists():
            print(f"  [skip] lake table not found: {src_table_dir}")
            continue

        if not args.dry_run:
            dst_table_dir.mkdir(parents=True, exist_ok=True)

        for game_id in sorted(test_game_ids):
            src = src_table_dir / f"{game_id}.parquet"
            dst = dst_table_dir / f"{game_id}.parquet"

            if not src.exists():
                missing += 1
                continue

            if args.dry_run:
                print(f"  [dry-run] would copy: {src} -> {dst}")
            else:
                shutil.copy2(str(src), str(dst))
                copied += 1

    if args.dry_run:
        print(f"Dry run complete. Would copy (approx): see above. Missing: {missing}.")
        return

    print(f"Copied {copied} parquet files. {missing} game/table combos not yet in lake.")

    # --- Recompute .expected_sha256 ---
    LAKE_TEST_DIR.mkdir(parents=True, exist_ok=True)
    sha256_hex = compute_sha256_of_lake_test()
    EXPECTED_SHA_FILE.write_text(sha256_hex + "\n")
    print(f"Rewrote {EXPECTED_SHA_FILE}: {sha256_hex[:16]}...")
    print("Done.")


if __name__ == "__main__":
    main()
