#!/usr/bin/env python3
"""Two-sided maker quoting: pair cap, imbalance, budget, fills parsing, paper fills."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import quoting as q  # noqa: E402

P = q.Params(order_shares=5, max_market_usd=12, pair_cap=0.97, max_imbalance=15,
             min_price=0.03, max_price=0.97, start_delay_secs=3, stop_secs=5, improve=True)
MID = dict(secs_left=150, secs_in=150)


def quotes(tops, inv=None, params=P, tick=0.01, committed=None, **kw):
    t = dict(MID, **kw)
    return q.compute_quotes(tops, tick, inv or q.Inventory(), params, t['secs_left'], t['secs_in'], committed)


class BookTests(unittest.TestCase):
    def test_best_levels_regardless_of_order(self):
        lv = lambda p, s='10': NS(price=p, size=s)  # noqa: E731
        book = NS(bids=[lv('0.01'), lv('0.40'), lv('0.39')], asks=[lv('0.99'), lv('0.43'), lv('0.45', '0')])
        self.assertEqual(q.best_bid_ask(book), (0.40, 0.43))

    def test_floor_tick(self):
        self.assertEqual(q.floor_tick(0.4567, 0.01), 0.45)
        self.assertEqual(q.floor_tick(0.4567, 0.001), 0.456)


class QuoteTests(unittest.TestCase):
    def test_flat_book_shades_both_sides_under_pair_cap(self):
        # 0.48/0.50 both sides: improving gives 0.49 + 0.49 = 0.98 > 0.97, so each is capped at 0.97 - 0.48.
        out = quotes({'Up': (0.48, 0.50), 'Down': (0.48, 0.50)})
        self.assertEqual(out['Up'], (0.49, 'ok'))
        self.assertEqual(out['Down'], (0.49, 'ok'))
        out = quotes({'Up': (0.49, 0.51), 'Down': (0.49, 0.51)})
        self.assertEqual(out['Up'], (0.48, 'ok'))

    def test_one_tick_spread_joins_best_bid(self):
        out = quotes({'Up': (0.70, 0.71), 'Down': (0.20, 0.30)}, params=q.Params(pair_cap=1.0))
        self.assertEqual(out['Up'][0], 0.70)
        self.assertEqual(out['Down'][0], 0.21)

    def test_lagging_side_prices_against_own_average(self):
        inv = q.Inventory()
        inv.add('Up', 'BUY', 10, 0.60)  # Up leads; Down must keep avg pair <= 0.97
        out = quotes({'Up': (0.30, 0.32), 'Down': (0.66, 0.70)}, inv)
        self.assertEqual(out['Down'], (0.37, 'ok'))

    def test_leading_side_stops_at_imbalance(self):
        inv = q.Inventory()
        inv.add('Up', 'BUY', 15, 0.40)
        out = quotes({'Up': (0.40, 0.42), 'Down': (0.55, 0.57)}, inv)
        self.assertEqual(out['Up'], (None, 'imbalance'))
        self.assertEqual(out['Down'][1], 'ok')

    def test_budget_counts_other_sides_resting_bid(self):
        inv = q.Inventory()
        inv.add('Up', 'BUY', 10, 0.45)
        inv.add('Down', 'BUY', 10, 0.45)  # spent 9
        out = quotes({'Up': (0.40, 0.45), 'Down': (0.50, 0.55)}, inv, committed={'Up': 2.0})
        # Down: 9 spent + 2 resting on Up + 0.51 * 5 > 12. Up ignores its own resting bid: 9 + 0.41 * 5 <= 12.
        self.assertEqual(out['Down'], (None, 'budget'))
        self.assertEqual(out['Up'], (0.41, 'ok'))

    def test_price_band(self):
        out = quotes({'Up': (0.01, 0.02), 'Down': (0.97, 0.99)}, params=q.Params(pair_cap=1.0))
        self.assertEqual(out['Up'], (0.01, 'ok'))          # the cheap leg of a 0.97 + 0.01 pair is allowed
        self.assertEqual(out['Down'], (None, 'price_band'))
        out = quotes({'Up': (0.01, 0.02), 'Down': (0.97, 0.99)}, params=q.Params(pair_cap=1.0, min_price=0.03))
        self.assertEqual(out['Up'], (None, 'price_band'))

    def test_window_edges(self):
        tops = {'Up': (0.48, 0.50), 'Down': (0.48, 0.50)}
        self.assertEqual(quotes(tops, secs_left=5)['Up'], (None, 'closing'))
        self.assertEqual(quotes(tops, secs_in=1)['Up'], (None, 'opening'))

    def test_missing_book(self):
        out = quotes({'Up': (None, 0.5), 'Down': (0.4, 0.5)})
        self.assertEqual(out['Up'], (None, 'no_book'))
        self.assertEqual(out['Down'], (None, 'no_book'))

    def test_crossed_book_is_not_quoted(self):
        out = quotes({'Up': (0.47, 0.45), 'Down': (0.50, 0.52)})
        self.assertEqual(out['Up'], (None, 'crossed'))

    def test_never_crosses_ask(self):
        out = quotes({'Up': (0.10, 0.11), 'Down': (0.10, 0.12)}, params=q.Params(pair_cap=1.0))
        self.assertLess(out['Up'][0], 0.11)
        self.assertLess(out['Down'][0], 0.12)


class LocalBookTests(unittest.TestCase):
    def book(self):
        lv = lambda p, s: NS(price=p, size=s)  # noqa: E731
        b = q.Book()
        b.snapshot([lv('0.40', '100'), lv('0.39', '50')], [lv('0.45', '30'), lv('0.46', '10')])
        return b

    def test_level_updates(self):
        b = self.book()
        b.level('BUY', '0.41', '20')
        b.level('SELL', '0.45', '0')
        self.assertEqual(b.top(), (0.41, 0.46))

    def test_own_bid_is_excluded_so_we_do_not_chase_ourselves(self):
        b = self.book()
        b.level('BUY', '0.41', '5')          # our improved bid, now the visible best bid
        self.assertEqual(b.top({0.41: 5.0}), (0.40, 0.45))
        b.level('BUY', '0.41', '25')         # someone joined us: 0.41 is the market again
        self.assertEqual(b.top({0.41: 5.0}), (0.41, 0.45))

    def test_trim_drops_levels_past_the_server_top(self):
        b = self.book()
        b.level('BUY', '0.47', '40')         # crossing bid arrives before the asks it took are removed
        self.assertEqual(b.top(), (0.47, 0.45))
        b.trim('0.47', '0.48')
        self.assertEqual(b.top(), (0.47, None))
        self.assertEqual(b.asks, {})


class FillParseTests(unittest.TestCase):
    W = '0xabc'

    def test_maker_entries_only_for_our_wallet(self):
        t = {'id': 't1', 'status': 'CONFIRMED', 'trader_side': 'MAKER', 'outcome': 'Down', 'side': 'SELL',
             'size': '50', 'price': '0.6',
             'maker_orders': [
                 {'order_id': 'o1', 'maker_address': '0xABC', 'outcome': 'Up', 'side': 'BUY',
                  'matched_amount': '5', 'price': '0.4'},
                 {'order_id': 'o2', 'maker_address': '0xdef', 'outcome': 'Up', 'side': 'BUY',
                  'matched_amount': '45', 'price': '0.4'}]}
        self.assertEqual(q.fills_from_trades([t], self.W)[0], {('t1', 'o1'): ('Up', 'BUY', 5.0, 0.4, 'maker')})

    def test_maker_entry_matched_by_api_key_when_address_missing(self):
        t = {'id': 't1', 'status': 'MATCHED', 'trader_side': None, 'maker_orders': [
            {'order_id': 'o1', 'owner': 'key-1', 'maker_address': None, 'outcome': 'Down', 'side': 'BUY',
             'matched_amount': '5', 'price': '0.3'}]}
        self.assertEqual(q.fills_from_trades([t], self.W, 'key-1')[0],
                         {('t1', 'o1'): ('Down', 'BUY', 5.0, 0.3, 'maker')})
        self.assertEqual(q.fills_from_trades([t], self.W, 'other')[0], {})

    def test_taker_row_and_failed_skipped(self):
        rows = [NS(id='t2', status='MATCHED', trader_side='TAKER', outcome='Up', side='BUY', size='5',
                   price='0.5', maker_orders=[]),
                NS(id='t3', status='FAILED', trader_side='TAKER', outcome='Up', side='BUY', size='5',
                   price='0.5', maker_orders=[])]
        fills, failed = q.fills_from_trades(rows, self.W)
        self.assertEqual(fills, {('t2', 'taker'): ('Up', 'BUY', 5.0, 0.5, 'taker')})
        self.assertEqual(failed, {'t3'})


class InventoryTests(unittest.TestCase):
    def test_avg_and_spent(self):
        inv = q.Inventory()
        inv.add('Up', 'BUY', 5, 0.4)
        inv.add('Up', 'BUY', 5, 0.6)
        self.assertAlmostEqual(inv.avg('Up'), 0.5)
        self.assertIsNone(inv.avg('Down'))
        self.assertAlmostEqual(inv.spent, 5.0)


class PaperFillTests(unittest.TestCase):
    def test_needs_trade_through(self):
        self.assertFalse(q.paper_fill(0.40, 0.40))
        self.assertTrue(q.paper_fill(0.40, 0.39))
        self.assertFalse(q.paper_fill(None, 0.30))
        self.assertFalse(q.paper_fill(0.40, None))


if __name__ == '__main__':
    unittest.main()
