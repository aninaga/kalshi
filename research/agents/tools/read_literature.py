"""read_literature — keyword search over the research/papers/ directory.

CLI::

    python -m research.agents.tools.read_literature \\
        --query "halftime bounce nba" \\
        [--max-results N]            # default 5
        [--papers-dir <path>]        # default research/papers/

Outputs a JSON array of ``{path, score, snippet}`` objects, ordered by score
descending.  Returns ``[]`` immediately when the papers directory contains no
eligible files (only the ``.gitkeep`` placeholder, or is empty).

File formats handled
--------------------
- ``*.md``, ``*.txt`` — read directly as UTF-8.
- ``*.pdf.txt`` — pre-extracted sidecar files; read as UTF-8.

PDF parsing is intentionally NOT done in-process; callers are expected to have
extracted PDFs offline into ``<name>.pdf.txt`` sidecars.

Scoring
-------
For each file, score = sum over query tokens of
``log(1 + count_in_doc) / log(1 + doc_length_in_tokens)`` where
``count_in_doc`` is the number of exact-substring (case-insensitive) matches of
the token and ``doc_length_in_tokens`` is the whitespace-split token count.

This is intentionally a simple weighted TF (not BM25 or vector search); the
plan explicitly defers full RAG to Phase 2.

Snippet
-------
A 200-character window around the first occurrence of the most-frequent query
token in the document text.  Newlines within the window are replaced with a
space for readability.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------

class _ScoredDoc(NamedTuple):
    path: str
    score: float
    snippet: str


# ---------------------------------------------------------------------------
# Core search
# ---------------------------------------------------------------------------

def _read_file(path: Path) -> str | None:
    """Read a text file, returning None on read errors."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, IOError):
        return None


def _eligible_files(papers_dir: Path) -> list[Path]:
    """Glob eligible files under papers_dir."""
    patterns = ["*.md", "*.txt", "*.pdf.txt"]
    found: list[Path] = []
    for pattern in patterns:
        for p in sorted(papers_dir.glob(pattern)):
            # Skip .gitkeep (zero-byte placeholder)
            if p.name == ".gitkeep":
                continue
            # pdf.txt files are already matched by *.txt as well; deduplicate
            # by using a set keyed on resolved path.
            found.append(p)
    # Deduplicate preserving order (*.pdf.txt already covered by *.txt).
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in found:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            deduped.append(p)
    return deduped


def _score_doc(text: str, tokens: list[str]) -> tuple[float, str]:
    """Return (score, snippet) for a document text and query tokens."""
    text_lower = text.lower()
    doc_words = text.split()
    doc_length = max(len(doc_words), 1)  # avoid division by zero

    # Per-token exact-substring counts (case-insensitive).
    token_counts: dict[str, int] = {}
    for token in tokens:
        token_lower = token.lower()
        count = text_lower.count(token_lower)
        token_counts[token] = count

    # Score: sum of log-weighted TF / log(1 + doc_length)
    score = sum(
        math.log(1 + cnt) / math.log(1 + doc_length)
        for cnt in token_counts.values()
    )

    # Snippet: 200-char window around first match of the most-frequent token.
    snippet = ""
    if score > 0:
        # Find the token with the highest count (tiebreak: token order).
        best_token = max(tokens, key=lambda t: (token_counts[t], -tokens.index(t)))
        best_lower = best_token.lower()
        idx = text_lower.find(best_lower)
        if idx >= 0:
            start = max(0, idx - 80)
            end = min(len(text), idx + 120)
            window = text[start:end].replace("\n", " ").replace("\r", " ")
            snippet = window.strip()

    return score, snippet


def search(
    query: str,
    papers_dir: Path,
    max_results: int = 5,
) -> list[dict]:
    """Search papers_dir for query tokens.  Returns list of result dicts."""
    if not papers_dir.is_dir():
        return []

    files = _eligible_files(papers_dir)
    if not files:
        return []

    tokens = query.split()
    if not tokens:
        return []

    scored: list[_ScoredDoc] = []
    for path in files:
        text = _read_file(path)
        if text is None:
            continue
        score, snippet = _score_doc(text, tokens)
        scored.append(_ScoredDoc(path=str(path), score=score, snippet=snippet))

    # Sort by score descending, then by path for determinism on ties.
    scored.sort(key=lambda d: (-d.score, d.path))

    return [
        {"path": d.path, "score": round(d.score, 6), "snippet": d.snippet}
        for d in scored[:max_results]
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_papers_dir() -> Path:
    """Returns research/papers/ relative to the project root (cwd)."""
    return Path("research/papers")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keyword search over the research/papers/ directory.",
        prog="python -m research.agents.tools.read_literature",
    )
    parser.add_argument(
        "--query",
        required=True,
        metavar="QUERY",
        help='Search query, e.g. "halftime bounce nba".',
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        metavar="N",
        help="Maximum number of results to return (default: 5).",
    )
    parser.add_argument(
        "--papers-dir",
        default=None,
        metavar="PATH",
        help="Override the papers directory (default: research/papers/).",
    )
    args = parser.parse_args(argv)

    papers_dir = (
        Path(args.papers_dir) if args.papers_dir else _default_papers_dir()
    )

    results = search(args.query, papers_dir, args.max_results)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
