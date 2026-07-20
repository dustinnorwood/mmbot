"""Unit tests for Lighter trade -> fill mapping (pure, no network)."""

import unittest

from mmkit.lighter import trade_to_fill

ME = 4242
OTHER = 9999


def trade(**kw):
    base = {
        "trade_id": 1001,
        "market_id": 21,
        "price": "0.1372",
        "size": "110.0",
        "usd_amount": "15.09",
        "bid_account_id": ME,
        "ask_account_id": OTHER,
        "is_maker_ask": True,
        "maker_fee": 0,
        "taker_fee": 0,
        "bid_account_pnl": 0.5,
        "ask_account_pnl": -0.5,
        "timestamp": 1_752_969_600_000,
        "type": "trade",
        "tx_hash": "0xabc",
    }
    base.update(kw)
    return base


class TestTradeMapping(unittest.TestCase):
    def test_not_my_trade_returns_none(self):
        self.assertIsNone(trade_to_fill(trade(bid_account_id=OTHER, ask_account_id=OTHER), ME, "FARTCOIN"))

    def test_i_am_bid_taker(self):
        # maker was ask side; I'm bid → I took
        f = trade_to_fill(trade(bid_account_id=ME, ask_account_id=OTHER, is_maker_ask=True), ME, "FARTCOIN")
        self.assertEqual(f["side"], "B")
        self.assertTrue(f["crossed"])
        self.assertEqual(f["closedPnl"], 0.5)   # bid-side pnl

    def test_i_am_ask_maker(self):
        # maker was ask side; I'm ask → I made
        f = trade_to_fill(trade(bid_account_id=OTHER, ask_account_id=ME, is_maker_ask=True), ME, "FARTCOIN")
        self.assertEqual(f["side"], "A")
        self.assertFalse(f["crossed"])
        self.assertEqual(f["closedPnl"], -0.5)  # ask-side pnl

    def test_i_am_bid_maker(self):
        # maker was bid side; I'm bid → I made
        f = trade_to_fill(trade(bid_account_id=ME, ask_account_id=OTHER, is_maker_ask=False), ME, "FARTCOIN")
        self.assertEqual(f["side"], "B")
        self.assertFalse(f["crossed"])

    def test_i_am_ask_taker(self):
        f = trade_to_fill(trade(bid_account_id=OTHER, ask_account_id=ME, is_maker_ask=False), ME, "FARTCOIN")
        self.assertEqual(f["side"], "A")
        self.assertTrue(f["crossed"])

    def test_fee_uses_my_role(self):
        f = trade_to_fill(
            trade(bid_account_id=ME, is_maker_ask=True, maker_fee=0.001, taker_fee=0.02),
            ME, "FARTCOIN")
        self.assertEqual(f["fee"], 0.02)        # I was taker
        f = trade_to_fill(
            trade(ask_account_id=ME, bid_account_id=OTHER, is_maker_ask=True,
                  maker_fee=0.001, taker_fee=0.02),
            ME, "FARTCOIN")
        self.assertEqual(f["fee"], 0.001)       # I was maker

    def test_self_match_skipped(self):
        self.assertIsNone(trade_to_fill(trade(bid_account_id=ME, ask_account_id=ME), ME, "FARTCOIN"))

    def test_schema_fields(self):
        f = trade_to_fill(trade(), ME, "FARTCOIN")
        self.assertEqual(f["tid"], 1001)
        self.assertEqual(f["coin"], "FARTCOIN")
        self.assertAlmostEqual(f["px"], 0.1372)
        self.assertAlmostEqual(f["sz"], 110.0)
        self.assertEqual(f["time"], 1_752_969_600_000)
        self.assertEqual(f["hash"], "0xabc")


if __name__ == "__main__":
    unittest.main()
