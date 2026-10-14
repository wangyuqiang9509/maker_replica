#!/usr/bin/env python3
"""Pure helpers of the per-fill edge report: slugs, fees, rows, markouts, bootstrap."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fill_edge_report as fe  # noqa: E402

MARKET = {'slug': 'btc-updown-5m-600', 'start': 600, 'end': 900, 'winner': 'Up',
          'fee_rate': 0.07, 'rebate_rate': 0.2}


def fill(outcome, side, price, size, ts=700, tx='0xa'):
    return {'outcome': outcome, 'side': side, 'price': price, 'size': size,
            'timestamp': ts, 'transactionHash': tx}


class SlugTests(unittest.TestCase):
    def test_only_windows_fully_inside_range(self):
        slugs = fe.market_slugs(301, 1500, ['5m'])
        self.assertEqual([s for s, _, _ in slugs],
                         ['btc-updown-5m-600', 'btc-updown-5m-900', 'btc-updown-5m-1200'])

    def test_15m_aligned_to_900(self):
        self.assertEqual(fe.market_slugs(0, 1800, ['15m']),
                         [('btc-updown-15m-0', 0, 900), ('btc-updown-15m-900', 900, 900)])


class FeeTests(unittest.TestCase):
    def test_matches_on_chain_fee(self):
        # 20 shares @ 0.94 paid 0.07896 pUSD on-chain (OrderFilled fee field).
        self.assertAlmostEqual(fe.taker_fee(20, 0.94, 0.07), 0.07896, places=6)


class RowTests(unittest.TestCase):
    def test_maker_buy_on_winner(self):
        (r,) = fe.build_rows([fill('Up', 'BUY', 0.4, 10)], MARKET, taker_hashes=set())
        self.assertEqual(r['role'], 'maker')
        self.assertAlmostEqual(r['gross'], 6.0)
        self.assertEqual(r['fee'], 0.0)
        self.assertAlmostEqual(r['rebate'], 0.2 * 10 * 0.07 * 0.4 * 0.6)
        self.assertEqual(r['secs_to_close'], 200)

    def test_taker_buy_on_loser_pays_fee(self):
        (r,) = fe.build_rows([fill('Down', 'BUY', 0.6, 10, tx='0xt')], MARKET, taker_hashes={'0xt'})
        self.assertEqual(r['role'], 'taker')
        self.assertAlmostEqual(r['gross'], -6.0)
        self.assertAlmostEqual(r['fee'], 10 * 0.07 * 0.6 * 0.4)
        self.assertEqual(r['rebate'], 0.0)

    def test_sell_of_loser_is_a_gain(self):
        (r,) = fe.build_rows([fill('Down', 'SELL', 0.3, 10)], MARKET, taker_hashes=set())
        self.assertAlmostEqual(r['gross'], 3.0)

    def test_net_adds_taker_rebate_share_of_fee(self):
        (r,) = fe.build_rows([fill('Up', 'BUY', 0.5, 10, tx='0xt')], MARKET, taker_hashes={'0xt'})
        self.assertAlmostEqual(fe.net_value(r, 0.5), r['gross'] - 0.5 * r['fee'])


class MarkoutTests(unittest.TestCase):
    def setUp(self):
        tape = [{'timestamp': 690, 'outcome': 'Up', 'price': 0.50},
                {'timestamp': 712, 'outcome': 'Down', 'price': 0.30},
                {'timestamp': 780, 'outcome': 'Up', 'price': 0.80}]
        self.series = fe.tape_index(tape)

    def test_uses_complement_of_other_side(self):
        (r,) = fe.build_rows([fill('Up', 'BUY', 0.5, 1, ts=700)], MARKET, set())
        self.assertAlmostEqual(fe.markout(r, self.series, 10), 0.2)

    def test_sell_markout_is_sign_flipped(self):
        (r,) = fe.build_rows([fill('Up', 'SELL', 0.5, 1, ts=700)], MARKET, set())
        self.assertAlmostEqual(fe.markout(r, self.series, 60), -0.3)

    def test_none_past_end_of_tape(self):
        (r,) = fe.build_rows([fill('Up', 'BUY', 0.5, 1, ts=770)], MARKET, set())
        self.assertIsNone(fe.markout(r, self.series, 60))

    def test_none_when_tape_starts_after_fill(self):
        (r,) = fe.build_rows([fill('Up', 'BUY', 0.5, 1, ts=600)], MARKET, set())
        self.assertIsNone(fe.markout(r, self.series, 10))


class SummaryTests(unittest.TestCase):
    def rows(self):
        out = []
        for i in range(40):
            m = dict(MARKET, slug='m%d' % i, winner='Up' if i % 2 else 'Down')
            out += fe.build_rows([fill('Up', 'BUY', 0.45, 10, tx='x%d' % i)], m, set())
        return out

    def test_edge_and_ci_bracket_point_estimate(self):
        s = fe.summarize(self.rows(), 0.0)
        self.assertEqual(s['markets'], 40)
        self.assertAlmostEqual(s['gross_pct'], 100 * (0.5 - 0.45) / 0.45, places=6)
        lo, hi = s['net_ci90']
        self.assertLess(lo, s['net_pct'])
        self.assertGreater(hi, s['net_pct'])

    def test_bootstrap_is_deterministic(self):
        rows = self.rows()
        self.assertEqual(fe.bootstrap_ci(rows, lambda r: r['gross']),
                         fe.bootstrap_ci(rows, lambda r: r['gross']))

    def test_empty_rows(self):
        self.assertIsNone(fe.summarize([], 0.0))


class ParseTsTests(unittest.TestCase):
    def test_iso_without_zone_is_utc(self):
        self.assertEqual(fe.parse_ts('1970-01-01T00:01:00'), 60)
        self.assertEqual(fe.parse_ts('1970-01-01T00:01:00Z'), 60)
        self.assertEqual(fe.parse_ts('60'), 60)


if __name__ == '__main__':
    unittest.main()
