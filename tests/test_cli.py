"""Smoke tests for the unified `kalshi-arb` CLI dispatch."""

import pytest

from kalshi_arbitrage.cli import build_parser, main


def test_parser_lists_all_subcommands():
    parser = build_parser()
    # argparse stores subcommand names in the choices of the subparsers action.
    sub = [a for a in parser._actions if a.dest == "command"][0]
    for cmd in ["doctor", "scan", "diagnose", "backtest", "analyze-paper",
                "readiness", "live"]:
        assert cmd in sub.choices


def test_no_command_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0  # required subcommand


def test_live_status_dispatches(capsys):
    rc = main(["live", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ARMED=" in out


def test_backtest_missing_labels_is_friendly(capsys, tmp_path):
    rc = main(["backtest", "--labeled", str(tmp_path / "nope.jsonl")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "No labeled pairs" in out


def test_analyze_paper_missing_file_is_friendly(capsys, tmp_path):
    rc = main(["analyze-paper", "--path", str(tmp_path / "nope.jsonl")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "No capture file" in out
