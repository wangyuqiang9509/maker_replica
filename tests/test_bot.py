#!/usr/bin/env python3
"""Engine paths that only matter live: reconcile order, self-exclusion, settlement, user stream, REST sync."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot as b  # noqa: E402
import quoting as q  # noqa: E402


def market(end):
    return {"slug": "btc-updown-5m-%d" % (end - 300), "condition_id": "0xc", "start": end - 300, "end": end,
            "tokens": {"Up": "1", "Down": "2"}}


class FakeLive:
    """Live-shaped exchange that records calls in order."""
    live = True
    has_relayer = True

    def __init__(self):
        self.orders, self.fills = {}, defaultdict(dict)
        self.calls, self.redeemed = [], []
        self.seq = 0
        self.wallet = "0xme"

    def market_orders(self, m):
        return {oid: o for oid, o in self.orders.items() if o["market"] == m["condition_id"]}

    async def cancel(self, ids):
        self.calls.append(("cancel", sorted(ids)))
        for oid in ids:
            self.orders.pop(oid, None)

    async def place(self, m, outcome, price, size):
        self.seq += 1
        oid = "o%d" % self.seq
        self.calls.append(("place", outcome, price))
        self.orders[oid] = {"market": m["condition_id"], "outcome": outcome, "price": price, "size": size, "ts": 0}
        return oid, None

    async def final_fills(self, m):
        return dict(self.fills[m["condition_id"]])

    async def redeem(self, m):
        self.redeemed.append(m["slug"])
        return "0xtx"


def run(coro):
    return asyncio.run(coro)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "j.jsonl"
        self.ex = FakeLive()
        self.bot = b.Bot(self.ex, q.Params(), ["5m"], b.Journal(self.path), 15, None)
        self.bot.spot.push(80000.0, time.time())
        for p in (mock.patch.object(b, "REDEEM_DELAY_SECS", 0), mock.patch("builtins.print")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def events(self, name):
        with open(self.path) as f:
            return [r for r in map(json.loads, f) if r["event"] == name]

    def window(self, up=(0.40, 0.45), down=(0.50, 0.55)):
        w = b.Window(market(int(time.time()) + 150))
        lv = lambda p: NS(price=p, size=100)  # noqa: E731
        w.books["Up"].snapshot([lv(up[0])], [lv(up[1])])
        w.books["Down"].snapshot([lv(down[0])], [lv(down[1])])
        return w

    def test_quotes_both_sides_and_does_not_chase_its_own_bid(self):
        w = self.window()
        run(self.bot.reconcile(w, time.time()))
        self.assertEqual(sorted(self.ex.calls), [("place", "Down", 0.51), ("place", "Up", 0.41)])
        # Our bids now show up in the streamed book; nothing should change.
        w.books["Up"].level("BUY", 0.41, 5)
        w.books["Down"].level("BUY", 0.51, 5)
        self.ex.calls.clear()
        run(self.bot.reconcile(w, time.time()))
        self.assertEqual(self.ex.calls, [])

    def test_stale_bids_are_cancelled_before_replacements(self):
        w = self.window()
        run(self.bot.reconcile(w, time.time()))
        self.ex.calls.clear()
        w.books["Up"].level("BUY", 0.40, 0)        # Up bid drops to 0.30
        w.books["Up"].level("BUY", 0.30, 100)
        run(self.bot.reconcile(w, time.time()))
        self.assertEqual(self.ex.calls, [("cancel", ["o1"]), ("place", "Up", 0.31)])

    def test_close_cancels_settles_from_final_fills_and_redeems(self):
        async def go():
            w = self.window()
            w.m["end"] = time.time() - 1
            w.sub = mock.AsyncMock()
            self.ex.orders["o9"] = {"market": "0xc", "outcome": "Up", "price": 0.4, "size": 5, "ts": 0}
            w.last_targets = {}
            self.ex.fills["0xc"] = {("t", "o9"): ("Up", "BUY", 5.0, 0.4, "maker")}
            with mock.patch.object(b, "resolved_winner", mock.AsyncMock(return_value="Up")):
                await self.bot.close_window(w, "window_end")
                await asyncio.gather(*list(self.bot.settling))
        run(go())
        self.assertIn(("cancel", ["o9"]), self.ex.calls)
        s = self.events("settle")[0]
        self.assertAlmostEqual(s["pnl"], 3.0)
        self.assertEqual(len(self.ex.redeemed), 1)

    def test_no_redeem_without_relayer(self):
        self.ex.has_relayer = False

        async def go():
            m = market(int(time.time()) - 1)
            self.ex.fills["0xc"] = {("t", "o"): ("Up", "BUY", 5.0, 0.4, "maker")}
            with mock.patch.object(b, "resolved_winner", mock.AsyncMock(return_value="Up")):
                await self.bot.settle(m)
        run(go())
        self.assertEqual(self.ex.redeemed, [])
        self.assertEqual(len(self.events("settle")), 1)


class SpotPullTests(EngineTests):
    async def placed_window(self):
        w = self.window(up=(0.49, 0.51), down=(0.49, 0.51))
        self.bot.windows[w.m["slug"]] = w
        await self.bot.reconcile(w, time.time())
        self.assertEqual(len(self.ex.orders), 2)
        self.ex.calls.clear()
        return w

    def test_btc_jump_pulls_both_bids_and_pauses_quoting(self):
        async def go():
            w = await self.placed_window()
            self.bot.spot.push(80000.5, time.time())
            self.bot.on_spot(80000.5)               # tiny move: keep resting
            await asyncio.sleep(0)
            self.assertEqual(self.ex.calls, [])
            self.bot.spot.push(80030.0, time.time())
            self.bot.on_spot(80030.0)               # ~3.7bp within the window: several ticks
            await asyncio.sleep(0.01)
            self.assertEqual(self.ex.calls, [("cancel", ["o1", "o2"])])
            await self.bot.reconcile(w, time.time())  # cooling down: nothing re-posted
            self.assertEqual(self.ex.orders, {})
            w.pulled_until = 0
            await self.bot.reconcile(w, time.time())  # cooldown over but BTC still moving: still out
            self.assertEqual(self.ex.orders, {})
            self.assertEqual(self.events("pull")[0]["orders"], ["o1", "o2"])
            w.pulled_until = 0
            self.bot.spot.recv = time.time()
            self.bot.spot.recent.clear()
            await self.bot.reconcile(w, time.time())
            self.assertEqual(len(self.ex.orders), 2)
        run(go())

    def test_stale_spot_cancels_and_blocks_quoting(self):
        async def go():
            w = await self.placed_window()
            self.bot.spot.recv = time.time() - 10
            await self.bot.reconcile(w, time.time())
        run(go())
        self.assertEqual(self.ex.calls, [("cancel", ["o1", "o2"])])
        self.assertEqual(self.ex.orders, {})


class SpotWindowTests(unittest.TestCase):
    def test_slow_drift_does_not_count_only_recent_moves(self):
        s = b.Spot("x", None, None, window_secs=2.0)
        s.push(80000, 100.0)
        s.push(80020, 103.0)                         # 3s later: the old price fell out of the window
        self.assertEqual(s.range_ref(), 80020)
        s.push(80005, 104.0)
        s.push(80030, 104.5)
        self.assertEqual(s.range_ref(), 80005)      # farthest recent price from the current one


class FairMoveTests(unittest.TestCase):
    def test_sensitivity_scales_with_moneyness_and_time(self):
        mid = q.fair_move(80000, 80008, 0.5, 150, 0.5)     # 1bp at the money, 150s left: ~3.7c
        self.assertAlmostEqual(mid, 0.0366, places=3)
        self.assertLess(q.fair_move(80000, 80008, 0.95, 150, 0.5), mid / 3)
        self.assertGreater(q.fair_move(80000, 80008, 0.5, 30, 0.5), mid * 2)
        self.assertEqual(q.fair_move(None, 80008, 0.5, 150, 0.5), 0.0)


class LiveBackendTests(unittest.TestCase):
    def live(self):
        lv = b.Live.__new__(b.Live)
        lv.orders, lv.fills = {}, defaultdict(dict)
        lv.wallet, lv.api_key = "0xme", "key"
        return lv

    def test_sdk_exception_is_a_reject(self):
        lv = self.live()
        lv.c = NS(place_limit_order=mock.AsyncMock(side_effect=RuntimeError("post-only would cross")))
        oid, err = run(lv.place(market(0), "Up", 0.5, 5))
        self.assertIsNone(oid)
        self.assertIn("post-only", err)
        self.assertEqual(lv.orders, {})

    def test_user_stream_fill_then_failed_trade_is_dropped(self):
        lv = self.live()
        mo = NS(order_id="o1", owner="key", maker_address="0xME", outcome="Up", side="BUY",
                matched_amount="5", price="0.4")
        trade = NS(id="t1", status="MATCHED", trader_side="MAKER", maker_orders=[mo], condition_id="0xc")
        cid, new = lv.on_user_event(NS(type="trade", payload=trade))
        self.assertEqual((cid, list(new.values())), ("0xc", [("Up", "BUY", 5.0, 0.4, "maker")]))
        trade.status = "FAILED"
        lv.on_user_event(NS(type="trade", payload=trade))
        self.assertEqual(lv.fills["0xc"], {})

    def test_order_events_update_and_remove_tracked_orders(self):
        lv = self.live()
        lv.orders["o1"] = {"market": "0xc", "outcome": "Up", "price": 0.4, "size": 5, "ts": 0}
        ev = lambda kind, matched: NS(type="order", payload=NS(  # noqa: E731
            id="o1", original_size="5", size_matched=matched, order_event_type=kind, condition_id="0xc"))
        lv.on_user_event(ev("UPDATE", "2"))
        self.assertEqual(lv.orders["o1"]["size"], 3.0)
        lv.on_user_event(ev("CANCELLATION", "2"))
        self.assertNotIn("o1", lv.orders)

    def test_sync_adopts_untracked_and_keeps_just_placed(self):
        lv = self.live()
        now = time.time()
        lv.orders["new"] = {"market": "0xc", "outcome": "Up", "price": 0.4, "size": 5, "ts": now + 5}
        lv.orders["gone"] = {"market": "0xc", "outcome": "Up", "price": 0.4, "size": 5, "ts": 0}

        def pager(items):
            async def it():
                for x in items:
                    yield x
            return NS(iter_items=it)
        stray = NS(id="stray", original_size="5", size_matched="0", side="BUY", asset_id="2", price="0.3")
        lv.c = NS(list_account_trades=lambda market: pager([]), list_open_orders=lambda market: pager([stray]))
        run(lv.sync(market(0)))
        self.assertEqual(set(lv.orders), {"new", "stray"})
        self.assertEqual(lv.orders["stray"]["outcome"], "Down")


if __name__ == "__main__":
    unittest.main()
