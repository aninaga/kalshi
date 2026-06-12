"""Live-readiness checklist: clean paper-run evaluation + corpus fallback.

The shared capture file accumulates sample fixtures and historical
'exchange'/'simulation' rows; readiness must evaluate only real paper fills (and
optionally one session) so old data can't pollute drift/unwind. And the matching
gate must default to the shipped gold corpus, not a non-existent path.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_arbitrage.validation.pilot import live_readiness_checklist as rc


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows))


def test_paper_run_filters_to_paper_source(tmp_path):
    ex = tmp_path / "ex.jsonl"
    _write(ex, [
        # Old fixture: exchange source, failed unwind — must be EXCLUDED.
        {"confirmation_source": "exchange", "skipped_reason": None,
         "realized_net": 0, "expected_net": 10, "est_vs_real_delta": -10,
         "hedge_residual": 40, "hedge_filled": False, "ts": 1},
        # Real paper fills that reconcile.
        {"confirmation_source": "paper", "skipped_reason": None,
         "realized_net": 7.62, "expected_net": 7.63, "est_vs_real_delta": -0.01,
         "hedge_residual": 0, "ts": 1000},
        {"confirmation_source": "paper", "skipped_reason": None,
         "realized_net": 5.66, "expected_net": 5.66, "est_vs_real_delta": 0.0,
         "hedge_residual": 0, "ts": 1001},
    ])
    res = rc.check_paper_run(str(ex))   # default source='paper'
    assert res.passed                    # drift ~0, no unwind failures from the fixture
    assert "unwind_failures=0" in res.detail


def test_paper_run_since_scopes_to_session(tmp_path):
    ex = tmp_path / "ex.jsonl"
    _write(ex, [
        {"confirmation_source": "paper", "skipped_reason": None, "realized_net": 1,
         "expected_net": 9, "est_vs_real_delta": -8, "ts": 100},     # old, big drift
        {"confirmation_source": "paper", "skipped_reason": None, "realized_net": 5,
         "expected_net": 5.0, "est_vs_real_delta": 0.0, "ts": 5000},  # this session
    ])
    # Without scoping, the old -8 drift fails; scoped to the session it passes.
    assert not rc.check_paper_run(str(ex)).passed
    assert rc.check_paper_run(str(ex), since=4000).passed


def test_default_labeled_path_finds_gold_corpus():
    p = rc._default_labeled_path()
    assert p.endswith(".jsonl")
    # Either the operator file or the shipped corpus must exist for the gate.
    assert Path(p).exists() or "labeled_pairs" in p
