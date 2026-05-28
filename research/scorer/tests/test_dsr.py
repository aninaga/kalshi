"""Tests for research.scorer.dsr."""
from __future__ import annotations

import unittest

from research.scorer.dsr import deflated_sharpe, expected_max_sharpe


class TestDeflatedSharpe(unittest.TestCase):
    def test_dsr_high_sharpe_high_pvalue_at_many_trials(self) -> None:
        """sharpe=1.5, n_trials=2000, n_observations=500 — DSR should be
        modest (~0.5) and p > 0.05; the multiple-testing correction bites.
        """
        # With sharpe_variance=1.0, expected_max_sharpe at n=2000 is large
        # (rough order ~3 from inverse-CDF lookups), so a sharpe of 1.5
        # is BELOW the best-of-N benchmark — DSR should drop well below 0.5.
        dsr, p = deflated_sharpe(
            sharpe=1.5,
            n_trials=2000,
            n_observations=500,
            sharpe_variance=1.0,
        )
        # We expect DSR very small (<0.05) because 1.5 << expected_max ~3.
        self.assertLess(dsr, 0.5, f"dsr {dsr} not below 0.5 — correction not biting")
        self.assertGreater(p, 0.05)

    def test_dsr_low_sharpe_fails(self) -> None:
        """sharpe=0.1 — DSR very low."""
        dsr, p = deflated_sharpe(
            sharpe=0.1,
            n_trials=100,
            n_observations=500,
            sharpe_variance=1.0,
        )
        self.assertLess(dsr, 0.1)
        self.assertGreater(p, 0.9)

    def test_dsr_zero_trials_handled(self) -> None:
        """n_trials=1 should produce a sensible number, not divide by zero.
        With n_trials=1 the expected_max_sharpe collapses to 0 (no
        multiple-testing), so DSR == PSR(sharpe, 0).
        """
        dsr, p = deflated_sharpe(
            sharpe=0.5,
            n_trials=1,
            n_observations=200,
            sharpe_variance=1.0,
        )
        self.assertTrue(0.0 <= dsr <= 1.0)
        self.assertTrue(0.0 <= p <= 1.0)
        # PSR with benchmark 0 and a positive sharpe should be > 0.5.
        self.assertGreater(dsr, 0.5)

    def test_expected_max_sharpe_grows_with_n_trials(self) -> None:
        """Sanity: more trials → higher expected-max — basic monotonicity."""
        e10 = expected_max_sharpe(10, 1.0)
        e100 = expected_max_sharpe(100, 1.0)
        e1000 = expected_max_sharpe(1000, 1.0)
        self.assertLess(e10, e100)
        self.assertLess(e100, e1000)


if __name__ == "__main__":
    unittest.main()
