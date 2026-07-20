"""Unit tests for PnL attribution and markout math (synthetic fills, no network)."""

import unittest

from mmkit.analytics import attribute_pnl, compute_markouts


def fill(coin="ETH", side="B", px=100.0, sz=1.0, fee=0.01, crossed=False, closed_pnl=0.0, time_ms=0):
    return {
        "coin": coin, "side": side, "px": px, "sz": sz,
        "fee": fee, "crossed": crossed, "closed_pnl": closed_pnl, "time_ms": time_ms,
    }


class TestPnL(unittest.TestCase):
    def test_round_trip_spread_capture(self):
        # buy 1 @ 99.95, sell 1 @ 100.05 → realized +0.10
        fills = [fill(side="B", px=99.95), fill(side="A", px=100.05)]
        c = attribute_pnl(fills)["ETH"]
        self.assertAlmostEqual(c.realized, 0.10)
        self.assertEqual(c.position, 0.0)
        self.assertAlmostEqual(c.fees_paid, 0.02)
        self.assertAlmostEqual(c.net, 0.10 - 0.02)

    def test_avg_cost_extension(self):
        # buy 1 @ 100, buy 1 @ 102 → avg 101; sell 2 @ 103 → realized 2*(103-101)=4
        fills = [fill(px=100.0), fill(px=102.0), fill(side="A", px=103.0, sz=2.0)]
        c = attribute_pnl(fills)["ETH"]
        self.assertAlmostEqual(c.realized, 4.0)
        self.assertEqual(c.position, 0.0)

    def test_flip_through_zero(self):
        # long 1 @ 100, sell 3 @ 104 → realize +4 on 1, now short 2 @ avg 104
        fills = [fill(px=100.0), fill(side="A", px=104.0, sz=3.0)]
        c = attribute_pnl(fills)["ETH"]
        self.assertAlmostEqual(c.realized, 4.0)
        self.assertAlmostEqual(c.position, -2.0)
        self.assertAlmostEqual(c.avg_cost, 104.0)

    def test_short_side_realization(self):
        # short 2 @ 100, cover 2 @ 97 → realized +6
        fills = [fill(side="A", px=100.0, sz=2.0), fill(side="B", px=97.0, sz=2.0)]
        c = attribute_pnl(fills)["ETH"]
        self.assertAlmostEqual(c.realized, 6.0)
        self.assertEqual(c.position, 0.0)

    def test_unrealized_with_marks(self):
        # long 1 @ 100, mark 105 → unrealized +5
        c = attribute_pnl([fill(px=100.0)], marks={"ETH": 105.0})["ETH"]
        self.assertAlmostEqual(c.unrealized, 5.0)

    def test_rebates_tracked_separately(self):
        # maker rebate arrives as negative fee
        c = attribute_pnl([fill(fee=-0.005)])["ETH"]
        self.assertAlmostEqual(c.rebates, 0.005)
        self.assertAlmostEqual(c.fees_paid, 0.0)

    def test_maker_taker_counts(self):
        pnl = attribute_pnl([fill(crossed=False), fill(side="A", crossed=True)])
        self.assertEqual(pnl["ETH"].n_maker, 1)
        self.assertEqual(pnl["ETH"].n_fills, 2)

    def test_multi_coin_isolation(self):
        fills = [fill(coin="ETH", px=100.0), fill(coin="SOL", px=20.0)]
        pnl = attribute_pnl(fills)
        self.assertEqual(set(pnl), {"ETH", "SOL"})


class TestMarkout(unittest.TestCase):
    def test_buy_positive_markout_when_price_rises(self):
        # bought at 100, mid at t+60s is 100.5 → +50bps * $0.5
        fills = [fill(px=100.0, sz=1.0, time_ms=0, crossed=False)]
        mids = {("ETH", 60_000): 100.5, ("ETH", 300_000): 101.0}
        rows = compute_markouts(fills, lambda c, t: mids.get((c, t)), [60, 300])
        r60 = next(r for r in rows if r.horizon_s == 60)
        self.assertAlmostEqual(r60.avg_bps, 50.0)
        self.assertAlmostEqual(r60.sum_usd, 0.5)

    def test_sell_negative_markout_when_price_rises(self):
        # sold at 100 and price went up → adverse selection, negative markout
        fills = [fill(side="A", px=100.0, sz=2.0, time_ms=0)]
        rows = compute_markouts(fills, lambda c, t: 100.5, [60])
        self.assertAlmostEqual(rows[0].avg_bps, -50.0)
        self.assertAlmostEqual(rows[0].sum_usd, -1.0)

    def test_missing_mid_counted(self):
        fills = [fill(time_ms=0)]
        rows = compute_markouts(fills, lambda c, t: None, [60])
        self.assertEqual(rows[0].n, 0)
        self.assertEqual(rows[0].n_missing, 1)

    def test_maker_taker_split(self):
        fills = [fill(time_ms=0, crossed=False), fill(time_ms=0, crossed=True)]
        rows = compute_markouts(fills, lambda c, t: 100.1, [60])
        self.assertEqual(len(rows), 2)  # one maker row, one taker row


if __name__ == "__main__":
    unittest.main()
