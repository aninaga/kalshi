"""Golden tests for venue_fees — pin the OFFICIAL published fee schedules.

These tests are the repo's contract with the venues' published fee schedules.
If one fails after a venue changes its schedule, update venue_fees AND these
pins together (with the new fetch date), never just the code.

Sources:
- Polymarket: docs.polymarket.com/trading/fees +
  help.polymarket.com/en/articles/13364478-trading-fees (verified 2026-06-09
  and 2026-06-12). Taker = shares x (bps/10000) x p x (1-p); category bps:
  geopolitics 0, sports 300, politics/finance/tech/mentions 400,
  economics/culture/weather/other 500, crypto 700. Makers $0 (+20-25% rebate,
  not modeled). HALF_UP to 5 decimals, 0.00001 USDC minimum when nonzero.
- Kalshi: kalshi.com/fee-schedule + help.kalshi.com/en/articles/13823805-fees
  (verified 2026-06-12). Taker = ceil_cents(0.07 x C x P x (1-P)); maker =
  ceil_cents(0.0175 x C x P x (1-P)) on "quadratic_with_maker_fees" series
  only. Per-series fee_type/fee_multiplier from /trade-api/v2/series/<ticker>
  (fetched 2026-06-12): KXNBA with-maker x1.0; KXBTCD/KXETHD/KXHIGHNY plain
  quadratic x1.0; KXINX quadratic x0.5.
"""
import pytest

import venue_fees as vf


# ---------------------------------------------------------------------------
# Kalshi taker — ceil_cents(0.07 * C * P * (1-P))
# ---------------------------------------------------------------------------
class TestKalshiTaker:
    def test_atm_batch_of_100_is_175_cents(self):
        # 0.07 * 100 * 0.25 = $1.75 exactly (no rounding needed)
        assert vf.kalshi_taker_fee(0.50, 100) == pytest.approx(1.75)

    def test_atm_single_contract_ceils_to_2_cents(self):
        # 0.07 * 1 * 0.25 = $0.0175 -> ceil to next cent = $0.02
        assert vf.kalshi_taker_fee(0.50, 1) == pytest.approx(0.02)

    def test_tail_batch_of_100(self):
        # 0.07 * 100 * 0.95 * 0.05 = $0.3325 -> ceil cents = $0.34
        assert vf.kalshi_taker_fee(0.95, 100) == pytest.approx(0.34)

    def test_fee_vanishes_at_extremes(self):
        assert vf.kalshi_taker_fee(0.99, 100) == pytest.approx(0.07)
        assert vf.kalshi_taker_fee(0.50, 100) > vf.kalshi_taker_fee(0.90, 100)

    def test_financials_half_multiplier(self):
        # KXINX-style series: fee_multiplier 0.5 halves the factor pre-ceil.
        assert vf.kalshi_taker_fee(0.50, 100, fee_multiplier=0.5) == pytest.approx(0.88)


# ---------------------------------------------------------------------------
# Kalshi maker — quarter-of-taker, ONLY on with-maker-fees series
# ---------------------------------------------------------------------------
class TestKalshiMaker:
    def test_nba_maker_atm_quarter_of_taker(self):
        # 0.0175 * 100 * 0.25 = $0.4375 -> ceil cents = $0.44
        fee = vf.kalshi_maker_fee(0.50, 100, fee_type=vf.KALSHI_FEE_TYPE_WITH_MAKER)
        assert fee == pytest.approx(0.44)

    def test_plain_quadratic_series_pay_zero(self):
        # KXBTCD / KXETHD / KXHIGHNY are taker-only series.
        assert vf.kalshi_maker_fee(0.50, 100,
                                   fee_type=vf.KALSHI_FEE_TYPE_QUADRATIC) == 0.0

    def test_maker_is_not_full_taker(self):
        # The pre-2026-06-12 maker_fill_study charged makers the FULL taker fee.
        maker = vf.kalshi_maker_fee(0.50, 100)
        taker = vf.kalshi_taker_fee(0.50, 100)
        assert maker == pytest.approx(taker / 4.0, abs=0.01)

    def test_series_snapshot(self):
        assert vf.kalshi_series_fee_params("KXNBA") == (vf.KALSHI_FEE_TYPE_WITH_MAKER, 1.0)
        assert vf.kalshi_series_fee_params("KXBTCD") == (vf.KALSHI_FEE_TYPE_QUADRATIC, 1.0)
        assert vf.kalshi_series_fee_params("KXHIGHNY") == (vf.KALSHI_FEE_TYPE_QUADRATIC, 1.0)
        assert vf.kalshi_series_fee_params("KXINX") == (vf.KALSHI_FEE_TYPE_QUADRATIC, 0.5)

    def test_unknown_series_defaults_conservative(self):
        # Unknown series must default to the OVER-charging side.
        assert vf.kalshi_series_fee_params("KXNEWTHING") == (
            vf.KALSHI_FEE_TYPE_WITH_MAKER, 1.0)


# ---------------------------------------------------------------------------
# Polymarket taker — official parabolic, per-category
# ---------------------------------------------------------------------------
class TestPolymarketTaker:
    def test_sports_atm(self):
        # 0.03 * 0.5 * 0.5 = $0.0075 per share
        assert vf.pm_taker_fee(0.50, 1, category="sports") == pytest.approx(0.0075)

    def test_sports_tail_is_14x_cheaper_than_legacy(self):
        # 0.03 * 0.95 * 0.05 = 0.0014250 -> HALF_UP 5dp = 0.00143
        official = vf.pm_taker_fee(0.95, 1, category="sports")
        assert official == pytest.approx(0.00143, abs=1e-6)
        legacy = vf.legacy_pm_taker_fee(0.95, 1)
        assert legacy / official > 10  # the tail over-charge that killed tail families

    def test_crypto_atm(self):
        assert vf.pm_taker_fee(0.50, 1, category="crypto") == pytest.approx(0.0175)

    def test_geopolitics_is_genuinely_free(self):
        assert vf.pm_taker_fee(0.50, 1, category="geopolitics") == 0.0

    def test_explicit_bps_beats_category(self):
        assert vf.pm_taker_fee(0.50, 1, fee_rate_bps=0, category="crypto") == 0.0
        assert vf.pm_taker_fee(0.50, 1, fee_rate_bps=300,
                               category="crypto") == pytest.approx(0.0075)

    def test_default_fallback_is_conservative_500bps(self):
        assert vf.pm_taker_fee(0.50, 1) == pytest.approx(0.0125)

    def test_minimum_charge(self):
        fee = vf.pm_taker_fee(0.0001, 0.01, fee_rate_bps=300)
        assert fee == pytest.approx(0.00001)

    def test_unknown_category_raises(self):
        with pytest.raises(ValueError):
            vf.pm_taker_fee(0.50, 1, category="esports")

    def test_makers_pay_zero(self):
        assert vf.pm_maker_fee(0.50, 100) == 0.0


# ---------------------------------------------------------------------------
# Legacy research fee — pinned for memo reproduction, never for new verdicts
# ---------------------------------------------------------------------------
class TestLegacyFee:
    def test_atm_pin(self):
        # flat 2% of 0.50 = 0.0100; curve = 0.5*0.25*(0.25)^2 = 0.0078125
        # -> 0.0178125 -> HALF_UP 4dp = 0.0178
        assert vf.legacy_pm_taker_fee(0.50, 1) == pytest.approx(0.0178, abs=1e-6)

    def test_tail_pin(self):
        # flat 2% of 0.95 = 0.0190; curve ~ 0.0005359 -> 0.0195
        assert vf.legacy_pm_taker_fee(0.95, 1) == pytest.approx(0.0195, abs=1e-6)

    def test_atm_overcharge_vs_official_sports_is_about_2_4x(self):
        ratio = vf.legacy_pm_taker_fee(0.50, 1) / vf.pm_taker_fee(0.50, 1, category="sports")
        assert 2.2 < ratio < 2.6


# ---------------------------------------------------------------------------
# Dispatchers
# ---------------------------------------------------------------------------
class TestDispatchers:
    def test_taker_dispatch(self):
        assert vf.taker_fee("kalshi", 0.5, 100) == vf.kalshi_taker_fee(0.5, 100)
        assert vf.taker_fee("polymarket", 0.5, 1,
                            category="sports") == vf.pm_taker_fee(0.5, 1, category="sports")
        assert vf.taker_fee("polymarket", 0.5, 1,
                            legacy=True) == vf.legacy_pm_taker_fee(0.5, 1)

    def test_maker_dispatch(self):
        assert vf.maker_fee("polymarket", 0.5, 100) == 0.0
        # Kalshi default = conservative with-maker schedule
        assert vf.maker_fee("kalshi", 0.5, 100) == pytest.approx(0.44)
        assert vf.maker_fee("kalshi", 0.5, 100,
                            fee_type=vf.KALSHI_FEE_TYPE_QUADRATIC) == 0.0

    def test_unknown_venue_raises(self):
        with pytest.raises(ValueError):
            vf.taker_fee("ftx", 0.5)
        with pytest.raises(ValueError):
            vf.maker_fee("ftx", 0.5)


# ---------------------------------------------------------------------------
# Cross-implementation parity — every consumer must agree with venue_fees
# ---------------------------------------------------------------------------
PRICE_GRID = [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]
SIZE_GRID = [1, 7, 100]


class TestConsumerParity:
    def test_arb_feemodel_kalshi(self):
        from kalshi_arbitrage.mock_execution import FeeModel
        for p in PRICE_GRID:
            for s in SIZE_GRID:
                assert FeeModel.kalshi_taker_fee(p, s) == vf.kalshi_taker_fee(p, s)

    def test_arb_feemodel_polymarket(self):
        from kalshi_arbitrage.mock_execution import FeeModel
        for p in PRICE_GRID:
            for bps in (0, 300, 500, 700):
                expect = vf.pm_taker_fee(p, 1, fee_rate_bps=bps) if bps else 0.0
                assert FeeModel.polymarket_taker_fee(p, 1, bps) == expect

    def test_lab_fillmodel_kalshi(self):
        from research.lab.execution import FillModel
        fm = FillModel(venue="kalshi")
        for p in PRICE_GRID:
            assert fm.fee(p) == vf.kalshi_taker_fee(p, 1.0)

    def test_lab_fillmodel_polymarket_is_official_sports(self):
        from research.lab.execution import FillModel
        fm = FillModel(venue="polymarket")
        for p in PRICE_GRID:
            assert fm.fee(p) == vf.pm_taker_fee(p, 1.0, category="sports")

    def test_lab_fillmodel_legacy_flag_reproduces_old_memos(self):
        from research.lab.execution import FillModel
        fm = FillModel(venue="polymarket", legacy_fees=True)
        for p in PRICE_GRID:
            assert fm.fee(p) == vf.legacy_pm_taker_fee(p, 1.0)

    def test_harness_realistic_fills(self):
        from research.harness import realistic_fills as rf
        from research.harness.cost_profile import get_active_profile
        prof = get_active_profile()
        for p in PRICE_GRID:
            assert rf._kalshi_taker_fee(p, 1.0) == pytest.approx(
                vf.kalshi_taker_fee(p, 1.0) * prof.kalshi_taker_multiplier)
            assert rf._polymarket_curve_fee(p, 1.0, 300) == vf.pm_taker_fee(
                p, 1.0, fee_rate_bps=300)

    def test_maker_fill_study(self):
        from research.scripts import maker_fill_study as mfs
        for p in PRICE_GRID:
            assert mfs._pm_maker_fee(p) == 0.0
            assert mfs._kalshi_maker_fee(p, 100) == vf.kalshi_maker_fee(
                p, 100, fee_type=vf.KALSHI_FEE_TYPE_WITH_MAKER)

    def test_evaluator_scripts(self):
        # Subprocess: the evaluator scripts import the root nba_odds_study
        # namespace package, which collides with research/nba_odds_study when
        # other suite modules import the latter first (sys.modules is
        # first-import-wins). A fresh interpreter is order-independent.
        import json
        import subprocess
        import sys
        from pathlib import Path

        code = (
            "import json, venue_fees as vf;"
            "from research.scripts import totals_realistic as tr;"
            "from research.scripts import spread_realistic as sr;"
            "grid = [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99];"
            "print(json.dumps(all("
            "  tr._pm_taker_fee(p) == vf.pm_taker_fee(p, 1.0, category='sports')"
            "  and sr._pm_taker_fee(p) == vf.pm_taker_fee(p, 1.0, category='sports')"
            "  for p in grid)))"
        )
        repo_root = Path(__file__).resolve().parents[1]
        out = subprocess.run([sys.executable, "-c", code], cwd=repo_root,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout.strip()) is True
