"""Unit tests for the quoter strategy engine (pure math, no network)."""

import unittest

from quoter.engine import (QuoteParams, SafetyState, VolEstimator, check_halt,
                           desired_quotes, round_px, round_sz, should_requote,
                           size_for_notional)


def params(**kw):
    defaults = dict(coin="TEST", sz_decimals=1, n_levels=2,
                    level_notional_usd=15.0, base_half_spread_bps=6.0,
                    level_step_bps=3.0, vol_k=1.5, skew_gain_bps=4.0,
                    max_position_usd=120.0, requote_bps=2.0, max_quote_age_s=45.0)
    defaults.update(kw)
    return QuoteParams(**defaults)


class TestRounding(unittest.TestCase):
    def test_five_sig_figs(self):
        self.assertEqual(round_px(1234.5678, 1), 1234.6)
        self.assertEqual(round_px(0.00123456, 0), 0.001235)  # capped at 6 decimals for szDec=0

    def test_decimal_cap_follows_sz_decimals(self):
        # szDecimals=4 → at most 2 decimal places
        self.assertEqual(round_px(1.23456, 4), 1.23)

    def test_size_floor(self):
        self.assertEqual(round_sz(1.2599, 2), 1.25)

    def test_min_notional_bump(self):
        # $5 target at px=100 with szDec=1 → 0.0 floored, must bump to >= $10.50
        sz = size_for_notional(5.0, 100.0, 1)
        self.assertGreaterEqual(sz * 100.0, 10.5)


class TestQuotes(unittest.TestCase):
    def test_flat_book_symmetric(self):
        q = desired_quotes(100.0, 0.0, params(), sigma_1m_bps=None)
        bids = [x for x in q if x.is_buy]
        asks = [x for x in q if not x.is_buy]
        self.assertEqual(len(bids), 2)
        self.assertEqual(len(asks), 2)
        # symmetric around mid when flat
        self.assertAlmostEqual((bids[0].px + asks[0].px) / 2, 100.0, places=1)
        self.assertLess(bids[0].px, 100.0)
        self.assertGreater(asks[0].px, 100.0)

    def test_long_inventory_shifts_down(self):
        flat = desired_quotes(100.0, 0.0, params(), None)
        long_ = desired_quotes(100.0, 60.0, params(), None)  # half of max long
        flat_ask = min(x.px for x in flat if not x.is_buy)
        long_ask = min(x.px for x in long_ if not x.is_buy)
        flat_bid = max(x.px for x in flat if x.is_buy)
        long_bid = max(x.px for x in long_ if x.is_buy)
        self.assertLess(long_ask, flat_ask)   # ask closer to mid → sell sooner
        self.assertLess(long_bid, flat_bid)   # bid further from mid → buy less

    def test_max_long_suppresses_bids(self):
        q = desired_quotes(100.0, 120.0, params(), None)
        self.assertFalse(any(x.is_buy for x in q))
        self.assertTrue(all(not x.is_buy for x in q))

    def test_max_short_suppresses_asks(self):
        q = desired_quotes(100.0, -120.0, params(), None)
        self.assertTrue(all(x.is_buy for x in q))

    def test_vol_widens_spread(self):
        calm = desired_quotes(100.0, 0.0, params(), sigma_1m_bps=1.0)
        wild = desired_quotes(100.0, 0.0, params(), sigma_1m_bps=20.0)
        calm_spread = (min(x.px for x in calm if not x.is_buy)
                       - max(x.px for x in calm if x.is_buy))
        wild_spread = (min(x.px for x in wild if not x.is_buy)
                       - max(x.px for x in wild if x.is_buy))
        self.assertGreater(wild_spread, calm_spread)

    def test_quotes_never_cross_mid(self):
        # extreme skew: even at full inventory, bid stays below mid, ask above
        q = desired_quotes(100.0, 119.0, params(skew_gain_bps=50.0), None)
        for x in q:
            if x.is_buy:
                self.assertLess(x.px, 100.0)
            else:
                self.assertGreater(x.px, 100.0)

    def test_levels_monotonic(self):
        q = desired_quotes(100.0, 0.0, params(n_levels=3), None)
        bids = sorted((x.px for x in q if x.is_buy), reverse=True)
        asks = sorted(x.px for x in q if not x.is_buy)
        self.assertEqual(bids, sorted(bids, reverse=True))
        self.assertEqual(len(set(bids)), 3)  # distinct levels
        self.assertEqual(len(set(asks)), 3)

    def test_min_notional_respected(self):
        q = desired_quotes(100.0, 0.0, params(level_notional_usd=8.0), None)
        for x in q:
            self.assertGreaterEqual(x.notional, 10.0)


class TestRequote(unittest.TestCase):
    def test_first_time_always(self):
        self.assertTrue(should_requote(0, None, None, 100.0, False, params()))

    def test_fill_triggers(self):
        self.assertTrue(should_requote(10, 9, 100.0, 100.0, True, params()))

    def test_small_drift_no_requote(self):
        # 1 bp move < 2 bp threshold, age < 45s
        self.assertFalse(should_requote(10, 9, 100.0, 100.01, False, params()))

    def test_drift_triggers(self):
        self.assertTrue(should_requote(10, 9, 100.0, 100.03, False, params()))

    def test_staleness_triggers(self):
        self.assertTrue(should_requote(100, 10, 100.0, 100.0, False, params()))


class TestSafety(unittest.TestCase):
    def test_kill_file_halts(self):
        s = check_halt(SafetyState(), 200.0, 20.0, kill_file_exists=True)
        self.assertTrue(s.halted)

    def test_loss_halts(self):
        s = SafetyState()
        s = check_halt(s, 200.0, 20.0, False)
        self.assertFalse(s.halted)
        s = check_halt(s, 179.0, 20.0, False)
        self.assertTrue(s.halted)

    def test_loss_within_budget_ok(self):
        s = SafetyState()
        s = check_halt(s, 200.0, 20.0, False)
        s = check_halt(s, 185.0, 20.0, False)
        self.assertFalse(s.halted)


class TestVol(unittest.TestCase):
    def test_needs_two_samples(self):
        v = VolEstimator()
        v.update(100.0, 0.0)
        self.assertIsNone(v.sigma_1m_bps())

    def test_constant_price_zero_vol(self):
        v = VolEstimator()
        for i in range(10):
            v.update(100.0, float(i))
        self.assertAlmostEqual(v.sigma_1m_bps(), 0.0)

    def test_bigger_moves_bigger_sigma(self):
        calm, wild = VolEstimator(), VolEstimator()
        px_c, px_w = 100.0, 100.0
        for i in range(1, 50):
            px_c *= 1.0001 if i % 2 else 0.9999
            px_w *= 1.001 if i % 2 else 0.999
            calm.update(px_c, float(i))
            wild.update(px_w, float(i))
        self.assertGreater(wild.sigma_1m_bps(), calm.sigma_1m_bps())


if __name__ == "__main__":
    unittest.main()
