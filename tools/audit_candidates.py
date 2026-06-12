"""Audit captured match candidates for recall (false negatives) + precision.

Reads the JSONL produced by a scan with ARB_CAPTURE_CANDIDATES set (every
above-threshold pair + the verifier's verdict). Surfaces:
  - rejection breakdown by veto reason (where recall is lost),
  - the highest-similarity REJECTED pairs (most likely true matches wrongly
    killed), grouped by reason, for human review,
  - the ACCEPTED pairs (precision check).

    python -m tools.audit_candidates /tmp/candidates.jsonl [--rejected-per-reason N]
"""

import argparse
import json
import sys
from collections import Counter, defaultdict


def load(path):
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _reason_kind(reasons):
    """Collapse a verdict's reason list to the deciding veto kind."""
    for r in reasons:
        for kind in ("scope_mismatch", "office_mismatch", "categorical",
                     "entity_overlap", "predicate_qualifier", "numeric_mismatch",
                     "close_time_skew", "threshold_mismatch", "rules_divergent",
                     "rules_scope_mismatch", "require_allowlist",
                     "unknown_polarity"):
            if kind in r:
                return kind
    # passed pairs or no explicit veto
    return "(passed/other)"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--rejected-per-reason", type=int, default=8)
    args = ap.parse_args(argv)

    rows = load(args.path)
    accepted = [r for r in rows if r.get("passed")]
    rejected = [r for r in rows if r.get("passed") is False]
    print(f"candidates captured: {len(rows)}  accepted: {len(accepted)}  rejected: {len(rejected)}")

    # Rejection breakdown by deciding veto.
    by_reason = Counter(_reason_kind(r.get("reasons", [])) for r in rejected)
    print("\n=== rejections by veto kind ===")
    for kind, n in by_reason.most_common():
        print(f"  {n:4}  {kind}")

    # Highest-similarity rejected pairs per reason — these are the recall risk:
    # a high lexical similarity + a veto is exactly where a TRUE match might be
    # wrongly killed. Review these.
    grouped = defaultdict(list)
    for r in rejected:
        grouped[_reason_kind(r.get("reasons", []))].append(r)
    print(f"\n=== top {args.rejected_per_reason} highest-similarity REJECTED pairs per reason (recall review) ===")
    for kind, items in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        items.sort(key=lambda r: r.get("similarity", 0), reverse=True)
        print(f"\n--- {kind} ({len(items)}) ---")
        for r in items[:args.rejected_per_reason]:
            print(f"  sim={r.get('similarity'):.2f}  K:{r['kalshi_title'][:48]!r}")
            print(f"            P:{r['polymarket_title'][:48]!r}")

    # Accepted pairs (precision check).
    print(f"\n=== ACCEPTED pairs ({len(accepted)}) (precision review) ===")
    accepted.sort(key=lambda r: r.get("similarity", 0), reverse=True)
    for r in accepted:
        print(f"  sim={r.get('similarity'):.2f}  K:{r['kalshi_title'][:48]!r}  P:{r['polymarket_title'][:48]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
