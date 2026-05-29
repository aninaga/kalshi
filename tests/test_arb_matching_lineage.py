"""Tests for arb-bot Batch-2 matching/lineage fixes:
A8 (numeric/year tokens are distinguishing), B10 (close-time proximity),
B16 (opportunity book lineage)."""
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kalshi_arbitrage import utils
from kalshi_arbitrage.market_analyzer import MarketAnalyzer


def test_a8_extract_key_terms_keeps_numeric_tokens():
    a = MarketAnalyzer()
    terms = a._extract_key_terms("Fed funds rate above 5% in 2024")
    assert "2024" in terms                      # year retained (was dropped)
    assert any(t.startswith("5") for t in terms)  # "5%" retained


def test_a8_years_are_distinguishing_not_boilerplate():
    assert "2024" not in utils._BOILERPLATE and "2028" not in utils._BOILERPLATE
    same = utils._penalize_divergent_terms(0.95, {"2024"}, {"2024"})
    diff = utils._penalize_divergent_terms(0.95, {"2024"}, {"2028"})
    assert diff < same  # a year mismatch now reduces the match score


def test_b10_close_time_parse_and_proximity():
    a = MarketAnalyzer()
    assert a._parse_close_time("2026-01-01T00:00:00Z") is not None
    assert a._parse_close_time(None) is None
    base = {"clean_title": "lakers beat celtics tonight", "volume": 0}
    k = {**base, "close_time": "2026-01-01T00:00:00Z"}
    near = {**base, "close_time": "2026-01-01T00:30:00Z"}  # 30 min apart -> boost
    far = {**base, "close_time": "2026-02-01T00:00:00Z"}   # ~1 month apart -> penalty
    assert a._calculate_match_confidence(k, near) > a._calculate_match_confidence(k, far)


def test_b16_book_lineage_reports_source_and_age():
    a = MarketAnalyzer()
    now = time.time()
    lin = a._book_lineage(
        {"source": "ws", "timestamp": now, "is_synthetic": False},
        {"source": "rest", "timestamp": now - 5, "is_synthetic": False},
    )
    assert lin["kalshi_book"]["source"] == "ws"
    assert lin["polymarket_book"]["source"] == "rest"
    assert 4 <= lin["polymarket_book"]["age_s"] <= 7
    assert lin["kalshi_book"]["synthetic"] is False
    # missing book -> all-None meta, no crash
    assert a._book_lineage(None, None)["kalshi_book"]["source"] is None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
