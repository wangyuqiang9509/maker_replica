#!/usr/bin/env python3
"""Two-sided maker for BTC 5m/15m Up/Down markets (replication test of @crouchpredictions).

Event driven: order books come from the CLOB market WebSocket and are kept locally,
our fills and order updates from the authenticated user WebSocket. Every book
update re-prices both bids; stale bids are cancelled first (one round trip),
then replacements are posted concurrently. REST is only used to reconcile fills
and open orders every few seconds and at settlement.

BTC trades stream from Binance. When BTC's move over the last MR_PULL_WINDOW_SECS
implies a fair-value move of MR_PULL_TICKS or more, every bid in that window is
cancelled at once, before the Polymarket book reacts (it lags BTC by ~1.5s), and
quoting pauses for MR_PULL_COOLDOWN_SECS while the book catches up. Without a
fresh BTC price no bids are posted.

Dry-run by default: the same engine runs on the live public books, but resting
bids are simulated and fill only when the ask trades through them. With
--execute it posts post-only GTC bids through the official async SDK, redeems
resolved windows through the relayer, and stops on a loss limit.
Quoting rules live in quoting.py.

Every action is appended to a JSONL journal; `bot.py summary` aggregates it.
"""
import argparse
import asyncio
import json
import os
import signal
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import httpx

import quoting as q

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GEOBLOCK = "https://polymarket.com/api/geoblock"
UA = {"User-Agent": "Mozilla/5.0 (maker-replica)"}
DURATIONS = {"5m": 300, "15m": 900}
REDEEM_DELAY_SECS = 60
SYNC_SECS = 5          # REST reconcile of fills and open orders per window
IDLE_WAKE_SECS = 0.25  # re-check timers (closing) even when the book is quiet
PREFETCH_SECS = 20     # subscribe to the next window this long before it opens
RISK_SECS = 60
SPOT_URL = "wss://data-stream.binance.vision/ws/btcusdt@trade"
ROOT = Path(__file__).resolve().parent


def env_float(name, default):
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def load_params():
    d = q.Params()
    return q.Params(
        order_shares=env_float("MR_ORDER_SHARES", d.order_shares),
        max_market_usd=env_float("MR_MAX_MARKET_USD", d.max_market_usd),
        pair_cap=env_float("MR_PAIR_CAP", d.pair_cap),
        max_imbalance=env_float("MR_MAX_IMBALANCE", d.max_imbalance),
        min_price=env_float("MR_MIN_PRICE", d.min_price),
        max_price=env_float("MR_MAX_PRICE", d.max_price),
        start_delay_secs=int(env_float("MR_START_DELAY_SECS", d.start_delay_secs)),
        stop_secs=int(env_float("MR_STOP_SECS", d.stop_secs)),
        improve=os.getenv("MR_IMPROVE", "1") != "0",
    )


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event, **kw):
        row = {"ts": round(time.time(), 3), "event": event, **kw}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        if event not in ("quote",):
            print(datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3], event,
                  json.dumps(kw, default=str)[:240], flush=True)


class SpotSettings:
    def __init__(self):
        self.url = os.getenv("MR_SPOT_URL") or SPOT_URL
        # Calibrated on 14 min of Binance vs Polymarket books: pulls ahead of ~half of all 2-tick
        # book moves, 11% false pulls, bids off the market ~22% of the time.
        self.vol = env_float("MR_BTC_VOL", 0.35)                # annualised, for the sensitivity estimate
        self.pull_ticks = env_float("MR_PULL_TICKS", 1.5)       # pull when fair value moved this many ticks
        self.window = env_float("MR_PULL_WINDOW_SECS", 2.0)     # ... within this many seconds of BTC trades
        self.cooldown = env_float("MR_PULL_COOLDOWN_SECS", 2.0)
        self.stale_secs = env_float("MR_SPOT_STALE_SECS", 3.0)


class Spot:
    """Last BTC/USDT trade from Binance's public stream; calls on_tick for every trade."""

    def __init__(self, url, journal, on_tick, window_secs=2.0):
        self.url, self.j, self.on_tick = url, journal, on_tick
        self.price, self.recv, self.event_ms = None, 0.0, 0
        self.window_secs = window_secs
        self.recent = deque()              # (local time, price) over the last window_secs

    def push(self, price, now):
        self.price, self.recv = price, now
        self.recent.append((now, price))
        while self.recent and now - self.recent[0][0] > self.window_secs:
            self.recent.popleft()

    def range_ref(self):
        """The recent price farthest from the current one (the start of the move)."""
        return max((p for _, p in self.recent), key=lambda p: abs(p - self.price), default=self.price)

    def fresh(self, stale_secs):
        return self.price is not None and time.time() - self.recv <= stale_secs

    async def run(self):
        import websockets
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20) as ws:
                    self.j.log("spot_connected", url=self.url)
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "p" not in msg:
                            continue
                        self.event_ms = msg.get("E", 0)
                        self.push(float(msg["p"]), time.time())
                        self.on_tick(self.price)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.j.log("error", where="spot_stream", error=repr(e)[:300])
            await asyncio.sleep(1)


# ---------- markets ----------

async def load_market(http, kind, start):
    slug = "btc-updown-%s-%d" % (kind, start)
    rows = (await http.get("%s/markets" % GAMMA, params={"slug": slug})).json()
    if not rows:
        return None
    m = rows[0]
    return {
        "slug": slug, "kind": kind, "condition_id": m["conditionId"],
        "start": start, "end": start + DURATIONS[kind],
        "tokens": dict(zip(json.loads(m["outcomes"]), json.loads(m["clobTokenIds"]))),
    }


async def resolved_winner(http, slug):
    rows = (await http.get("%s/markets" % GAMMA, params={"slug": slug, "closed": "true"})).json()
    if not rows:
        return None
    m = rows[0]
    prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
    if len(prices) != 2 or sorted(prices) != [0.0, 1.0]:
        return None
    return json.loads(m["outcomes"])[prices.index(1.0)]


# ---------- execution backends ----------

class Paper:
    """No keys, no orders. Resting bids fill when the streamed ask trades through them."""
    live = False
    has_relayer = False

    def __init__(self):
        self.orders = {}                   # order_id -> dict(market, outcome, price, size, ts)
        self.fills = defaultdict(dict)     # condition_id -> {fill_key: fill}
        self.seq = 0
        self.wallet = "paper"

    async def start(self):
        from polymarket import AsyncPublicClient
        self.c = AsyncPublicClient()

    async def close(self):
        await self.c.close()

    async def subscribe_market(self, tokens):
        from polymarket.streams import MarketSpec
        return await self.c.subscribe(MarketSpec(token_ids=list(tokens)))

    def market_orders(self, m):
        return {oid: o for oid, o in self.orders.items() if o["market"] == m["condition_id"]}

    async def place(self, m, outcome, price, size):
        self.seq += 1
        oid = "paper-%d" % self.seq
        self.orders[oid] = {"market": m["condition_id"], "outcome": outcome, "price": price, "size": size,
                            "ts": time.time()}
        return oid, None

    async def cancel(self, order_ids):
        for oid in order_ids:
            self.orders.pop(oid, None)

    async def cancel_all(self):
        self.orders.clear()

    def paper_fills(self, m, asks):
        """Fill resting bids the ask traded through; returns the new fills."""
        new = {}
        for oid, o in list(self.market_orders(m).items()):
            if q.paper_fill(o["price"], asks.get(o["outcome"])):
                new[(oid, "maker")] = (o["outcome"], "BUY", o["size"], o["price"], "maker")
                del self.orders[oid]
        self.fills[m["condition_id"]].update(new)
        return new

    async def sync(self, m):
        return {}

    async def final_fills(self, m):
        return dict(self.fills[m["condition_id"]])

    async def redeem(self, m):
        return None

    async def equity(self, http):
        return None


class Live:
    live = True

    def __init__(self):
        self.orders = {}
        self.fills = defaultdict(dict)
        self.has_relayer = bool(os.getenv("PM_RELAYER_API_KEY") and os.getenv("PM_RELAYER_API_KEY_ADDRESS"))
        self.wallet = None

    async def start(self):
        from polymarket import AsyncSecureClient, RelayerApiKey
        relayer = None
        if self.has_relayer:
            relayer = RelayerApiKey(key=os.environ["PM_RELAYER_API_KEY"],
                                    address=os.environ["PM_RELAYER_API_KEY_ADDRESS"])
        self.c = await AsyncSecureClient.create(private_key=os.environ["PM_PRIVATE_KEY"],
                                                wallet=os.getenv("PM_FUNDER") or None, api_key=relayer)
        self.wallet = str(self.c.wallet).lower()
        self.api_key = self.c.credentials.key

    async def close(self):
        await self.c.close()

    async def subscribe_market(self, tokens):
        from polymarket.streams import MarketSpec
        return await self.c.subscribe(MarketSpec(token_ids=list(tokens)))

    async def subscribe_user(self):
        from polymarket.streams import UserSpec
        return await self.c.subscribe(UserSpec())

    def market_orders(self, m):
        return {oid: o for oid, o in self.orders.items() if o["market"] == m["condition_id"]}

    async def place(self, m, outcome, price, size):
        """Returns (order_id | None, error | None)."""
        try:
            r = await self.c.place_limit_order(token_id=m["tokens"][outcome], price=str(price), size=str(size),
                                               side="BUY", post_only=True)
        except Exception as e:  # noqa: BLE001 - the SDK raises on HTTP rejections (e.g. post-only cross)
            return None, repr(e)[:300]
        if not getattr(r, "ok", False):
            return None, "%s %s" % (getattr(r, "code", None), getattr(r, "message", None))
        self.orders[r.order_id] = {"market": m["condition_id"], "outcome": outcome, "price": price,
                                   "size": size, "ts": time.time()}
        return r.order_id, None

    async def cancel(self, order_ids):
        # Forget them either way; the next REST sync re-adopts any order that is still open.
        for oid in order_ids:
            self.orders.pop(oid, None)
        await self.c.cancel_orders(order_ids=list(order_ids))

    async def cancel_all(self):
        self.orders.clear()
        await self.c.cancel_all()

    def merge_fills(self, cid, trades):
        """Merge trades into our fills for one market; returns the new fills."""
        fills, failed = q.fills_from_trades(trades, self.wallet, self.api_key)
        book = self.fills[cid]
        for k in [k for k in book if k[0] in failed]:
            del book[k]
        new = {k: v for k, v in fills.items() if k not in book}
        book.update(fills)
        return new

    def on_user_event(self, ev):
        """Apply one user-stream event; returns (condition_id, new_fills) or None."""
        p = ev.payload
        if ev.type == "trade":
            return p.condition_id, self.merge_fills(p.condition_id, [p])
        if ev.type == "order":
            o = self.orders.get(p.id)
            if o is not None:
                remaining = float(p.original_size) - float(p.size_matched)
                if p.order_event_type == "CANCELLATION" or remaining <= q.EPS:
                    del self.orders[p.id]
                else:
                    o["size"] = remaining
            return p.condition_id, {}
        return None

    async def sync(self, m):
        """REST reconcile of one window's fills and open orders; returns new fills."""
        cid = m["condition_id"]
        t0 = time.time()
        trades = [t async for t in self.c.list_account_trades(market=cid).iter_items()]
        new = self.merge_fills(cid, trades)
        outcome_of = {tok: o for o, tok in m["tokens"].items()}
        open_ids = set()
        async for o in self.c.list_open_orders(market=cid).iter_items():
            open_ids.add(o.id)
            remaining = float(o.original_size) - float(o.size_matched)
            if o.id in self.orders:
                self.orders[o.id]["size"] = remaining
            elif str(o.side).upper() == "BUY" and o.asset_id in outcome_of:
                # Open but untracked (e.g. a cancel that did not go through): manage it again.
                self.orders[o.id] = {"market": cid, "outcome": outcome_of[o.asset_id], "price": float(o.price),
                                     "size": remaining, "ts": 0.0}
        for oid, o in list(self.market_orders(m).items()):
            # Orders placed after the listing started may not be in it yet.
            if oid not in open_ids and o["ts"] < t0 - 1:
                del self.orders[oid]
        return new

    async def final_fills(self, m):
        await self.sync(m)
        return dict(self.fills[m["condition_id"]])

    async def redeem(self, m):
        handle = await self.c.redeem_positions(condition_id=m["condition_id"])
        return getattr(await handle.wait(), "transaction_hash", None)

    async def equity(self, http):
        cash = (await self.c.get_balance_allowance(asset_type="COLLATERAL")).balance / 1e6
        rows = (await http.get("%s/value" % DATA_API, params={"user": self.wallet})).json()
        return cash + (float(rows[0]["value"]) if rows else 0.0)


# ---------- engine ----------

class Window:
    def __init__(self, m):
        self.m = m
        self.outcome_of = {tok: o for o, tok in m["tokens"].items()}
        self.books = {o: q.Book() for o in m["tokens"]}
        self.tick = 0.01
        self.wake = asyncio.Event()
        self.closed = False
        self.last_sync = 0.0
        self.last_targets = None
        self.sub = None
        self.tasks = []
        self.pulled_until = 0.0
        self.pulling = False

    def mid_up(self):
        bb, ba = self.books["Up"].top() if "Up" in self.books else (None, None)
        return (bb + ba) / 2 if bb is not None and ba is not None else None


class Bot:
    def __init__(self, ex, params, kinds, journal, max_loss, http, start_info=None, spot=None):
        self.ex, self.p, self.kinds, self.j = ex, params, kinds, journal
        self.max_loss, self.http = max_loss, http
        self.start_info = start_info or {}
        self.ss = spot or SpotSettings()
        self.spot = Spot(self.ss.url, journal, self.on_spot, self.ss.window)
        self.windows = {}        # slug -> Window (open)
        self.done = set()        # slugs whose quoting finished
        self.settling = set()
        self.stop = asyncio.Event()
        self.start_equity = None

    # --- lifecycle ---

    async def run(self, minutes):
        await self.ex.start()
        self.j.log("start", mode="live" if self.ex.live else "paper", wallet=self.ex.wallet,
                   params=self.p.__dict__, kinds=self.kinds, **self.start_info)
        if self.ex.live:
            # Orders left behind by a crashed previous run are not tracked; clear them first.
            await self.ex.cancel_all()
            self.j.log("cancel_all", reason="startup")
        loops = [asyncio.create_task(self.discover_loop()), asyncio.create_task(self.risk_loop()),
                 asyncio.create_task(self.spot.run())]
        if self.ex.live:
            loops.append(asyncio.create_task(self.user_loop()))
        try:
            await asyncio.wait_for(self.stop.wait(), minutes * 60 if minutes > 0 else None)
        except asyncio.TimeoutError:
            pass
        for t in loops:
            t.cancel()
        await self.shutdown()

    async def shutdown(self):
        try:
            await self.ex.cancel_all()
            if self.ex.live:
                self.j.log("cancel_all", reason="shutdown")
        except Exception as e:  # noqa: BLE001
            self.j.log("cancel_all_error", error=repr(e)[:300])
        for w in list(self.windows.values()):
            await self.close_window(w, "shutdown")
        self.j.log("stopping", pending_settlements=len(self.settling))
        if self.settling:
            await asyncio.gather(*list(self.settling), return_exceptions=True)
        await self.ex.close()
        self.j.log("stop")

    # --- discovery ---

    async def discover_loop(self):
        while True:
            now = time.time()
            for kind in self.kinds:
                dur = DURATIONS[kind]
                cur = int(now) // dur * dur
                for start in (cur, cur + dur):
                    slug = "btc-updown-%s-%d" % (kind, start)
                    if slug in self.windows or slug in self.done or start - now > PREFETCH_SECS:
                        continue
                    if start + dur - now <= self.p.stop_secs:
                        continue
                    try:
                        m = await load_market(self.http, kind, start)
                        if m:
                            await self.open_window(m)
                    except Exception as e:  # noqa: BLE001
                        self.j.log("error", where="discover", market=slug, error=repr(e)[:300])
            await asyncio.sleep(1)

    async def open_window(self, m):
        w = Window(m)
        w.sub = await self.ex.subscribe_market(m["tokens"].values())
        self.windows[m["slug"]] = w
        self.j.log("market", slug=m["slug"], condition_id=m["condition_id"], end=m["end"])
        w.tasks = [asyncio.create_task(self.stream_loop(w)), asyncio.create_task(self.quote_loop(w))]

    # --- streams ---

    @staticmethod
    def apply_market_event(w, ev):
        p = ev.payload
        if ev.type == "book":
            o = w.outcome_of.get(p.asset_id)
            if o:
                w.books[o].snapshot(p.bids, p.asks)
                if p.tick_size:
                    w.tick = float(p.tick_size)
        elif ev.type == "price_change":
            for pc in p.price_changes:
                o = w.outcome_of.get(pc.asset_id)
                if o:
                    w.books[o].level(pc.side, pc.price, pc.size)
                    w.books[o].trim(pc.best_bid, pc.best_ask)
        elif ev.type == "tick_size_change":
            w.tick = float(p.new_tick_size)

    async def stream_loop(self, w):
        while not w.closed:
            try:
                async for ev in w.sub:
                    self.apply_market_event(w, ev)
                    if not self.ex.live:
                        asks = {o: b.top()[1] for o, b in w.books.items()}
                        self.record_fills(w.m, self.ex.paper_fills(w.m, asks))
                    w.wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.j.log("error", where="market_stream", market=w.m["slug"], error=repr(e)[:300])
            if w.closed:
                return
            # Stream ended: resubscribe and start from a fresh snapshot.
            await asyncio.sleep(1)
            try:
                await w.sub.close()
                w.books = {o: q.Book() for o in w.m["tokens"]}
                w.sub = await self.ex.subscribe_market(w.m["tokens"].values())
                self.j.log("resubscribe", market=w.m["slug"])
            except Exception as e:  # noqa: BLE001
                self.j.log("error", where="market_resubscribe", market=w.m["slug"], error=repr(e)[:300])

    async def user_loop(self):
        while True:
            sub = None
            try:
                sub = await self.ex.subscribe_user()
                async for ev in sub:
                    r = self.ex.on_user_event(ev)
                    if not r:
                        continue
                    cid, new = r
                    for w in list(self.windows.values()):
                        if w.m["condition_id"] == cid:
                            self.record_fills(w.m, new)
                            w.wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.j.log("error", where="user_stream", error=repr(e)[:300])
            finally:
                if sub is not None:
                    try:
                        await sub.close()
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.sleep(1)

    def record_fills(self, m, new):
        for (outcome, side, size, price, role) in new.values():
            self.j.log("fill", market=m["slug"], outcome=outcome, side=side, price=price, size=size,
                       role=role, secs_left=round(m["end"] - time.time(), 1))

    # --- BTC-driven pulls ---

    def btc_move(self, w, now):
        """Fair-value move of this window implied by BTC's move over the pull window."""
        return q.fair_move(self.spot.range_ref(), self.spot.price, w.mid_up(), w.m["end"] - now, self.ss.vol)

    def on_spot(self, price):
        now = time.time()
        for w in list(self.windows.values()):
            if w.closed or w.pulling:
                continue
            if not self.ex.market_orders(w.m):
                continue
            move = self.btc_move(w, now)
            if move >= self.ss.pull_ticks * w.tick - q.EPS:
                w.pulling = True
                asyncio.get_running_loop().create_task(self.pull(w, move, price))

    async def pull(self, w, move, price):
        """Cancel every bid in the window now; the book has not caught up with BTC yet."""
        try:
            w.pulled_until = time.time() + self.ss.cooldown
            ids = list(self.ex.market_orders(w.m))
            if ids:
                t0 = time.perf_counter()
                spot_age_ms = round(time.time() * 1000 - self.spot.event_ms) if self.spot.event_ms else None
                await self.ex.cancel(ids)
                self.j.log("pull", market=w.m["slug"], orders=ids, fair_move=round(move, 4), spot=price,
                           spot_age_ms=spot_age_ms, ms=round((time.perf_counter() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            self.j.log("error", where="pull", market=w.m["slug"], error=repr(e)[:300])
        finally:
            w.pulling = False
            w.wake.set()

    # --- quoting ---

    async def quote_loop(self, w):
        m = w.m
        while not w.closed:
            try:
                await asyncio.wait_for(w.wake.wait(), IDLE_WAKE_SECS)
            except asyncio.TimeoutError:
                pass
            w.wake.clear()
            now = time.time()
            if m["end"] - now <= self.p.stop_secs:
                await self.close_window(w, "window_end")
                return
            try:
                if self.ex.live and now - w.last_sync >= SYNC_SECS:
                    w.last_sync = now
                    self.record_fills(m, await self.ex.sync(m))
                await self.reconcile(w, time.time())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep quoting through transient API errors
                self.j.log("error", where="quote", market=m["slug"], error=repr(e)[:300])
                await asyncio.sleep(0.5)

    async def reconcile(self, w, now):
        m = w.m
        inv = q.inventory_from(self.ex.fills[m["condition_id"]])
        resting = self.ex.market_orders(m)
        own = {o: defaultdict(float) for o in m["tokens"]}
        committed = defaultdict(float)
        for o in resting.values():
            own[o["outcome"]][round(o["price"], 6)] += o["size"]
            committed[o["outcome"]] += o["price"] * o["size"]
        tops = {}
        for o, b in w.books.items():
            # Live bids are in the streamed book; remove them so we never quote against ourselves.
            tops[o] = b.top(own[o] if self.ex.live else None) if b.ready else (None, None)
        targets = q.compute_quotes(tops, w.tick, inv, self.p, m["end"] - now, now - m["start"], committed)
        if now < w.pulled_until or w.pulling:
            targets = {o: (None, "pulled") for o in targets}
        elif not self.spot.fresh(self.ss.stale_secs):
            targets = {o: (None, "no_spot") for o in targets}
        elif self.btc_move(w, now) >= self.ss.pull_ticks * w.tick - q.EPS:
            targets = {o: (None, "btc_moving") for o in targets}
        if targets != w.last_targets:
            w.last_targets = targets
            self.j.log("quote", market=m["slug"], tops=tops, targets=targets, shares=inv.shares, cost=inv.cost)

        stale, places = [], []
        for outcome, (px, reason) in targets.items():
            mine = [oid for oid, o in resting.items() if o["outcome"] == outcome]
            keep = [oid for oid in mine if px is not None and abs(resting[oid]["price"] - px) < w.tick / 2]
            stale += [(oid, outcome, reason) for oid in mine if oid not in keep]
            if px is not None and not keep:
                places.append((outcome, px))
        if stale:
            # Protective step first: pull stale bids in one request before posting new ones.
            t0 = time.perf_counter()
            await self.ex.cancel([oid for oid, _, _ in stale])
            self.j.log("cancel", market=m["slug"], orders=stale, ms=round((time.perf_counter() - t0) * 1000))
        if places and (time.time() < w.pulled_until or w.pulling):
            places = []  # a pull started while we were cancelling
        if places:
            t0 = time.perf_counter()
            results = await asyncio.gather(*(self.ex.place(m, o, px, self.p.order_shares) for o, px in places))
            ms = round((time.perf_counter() - t0) * 1000)
            for (outcome, px), (oid, err) in zip(places, results):
                if oid:
                    self.j.log("place", market=m["slug"], outcome=outcome, price=px, size=self.p.order_shares,
                               order_id=oid, ms=ms)
                else:
                    self.j.log("reject", market=m["slug"], outcome=outcome, price=px, error=err, ms=ms)

    # --- closing and settlement ---

    async def close_window(self, w, reason):
        if w.closed:
            return
        w.closed = True
        m = w.m
        self.windows.pop(m["slug"], None)
        self.done.add(m["slug"])
        resting = list(self.ex.market_orders(m))
        if resting:
            try:
                await self.ex.cancel(resting)
            except Exception as e:  # noqa: BLE001
                self.j.log("error", where="close_cancel", market=m["slug"], error=repr(e)[:300])
        for t in w.tasks:
            if t is not asyncio.current_task():
                t.cancel()
        try:
            await w.sub.close()
        except Exception:  # noqa: BLE001
            pass
        inv = q.inventory_from(self.ex.fills[m["condition_id"]])
        self.j.log("close", market=m["slug"], reason=reason, shares=inv.shares, cost=inv.cost)
        # Live: settle every window we quoted, since fills can land after the last stream update.
        if inv.spent > 0 or (self.ex.live and w.last_targets is not None):
            t = asyncio.create_task(self.settle(m))
            self.settling.add(t)
            t.add_done_callback(self.settling.discard)

    async def settle(self, m):
        """Wait for resolution, record settlement from the final fills, then redeem (live only)."""
        await asyncio.sleep(max(0.0, m["end"] + REDEEM_DELAY_SECS - time.time()))
        winner = None
        for _ in range(30):
            try:
                winner = await resolved_winner(self.http, m["slug"])
            except Exception as e:  # noqa: BLE001
                self.j.log("error", where="resolve", market=m["slug"], error=repr(e)[:300])
            if winner:
                break
            await asyncio.sleep(20)
        if not winner:
            self.j.log("settle_timeout", market=m["slug"])
            return
        fills = None
        for _ in range(3):
            try:
                fills = await self.ex.final_fills(m)
                break
            except Exception as e:  # noqa: BLE001
                self.j.log("settle_fills_error", market=m["slug"], error=repr(e)[:300])
                await asyncio.sleep(10)
        inv = q.inventory_from(fills if fills is not None else self.ex.fills[m["condition_id"]])
        if inv.spent <= 0:
            return
        payout = inv.shares[winner]
        self.j.log("settle", market=m["slug"], winner=winner, spent=round(inv.spent, 4),
                   payout=round(payout, 4), pnl=round(payout - inv.spent, 4), shares=inv.shares, cost=inv.cost)
        if self.ex.live and payout > 0 and self.ex.has_relayer:
            for attempt in range(5):
                try:
                    tx = await self.ex.redeem(m)
                    self.j.log("redeem", market=m["slug"], tx=tx)
                    return
                except Exception as e:  # noqa: BLE001 - relayer hiccups; retry then report
                    self.j.log("redeem_error", market=m["slug"], attempt=attempt, error=repr(e)[:300])
                    await asyncio.sleep(30)

    # --- risk ---

    async def risk_loop(self):
        if not self.ex.live:
            return
        while True:
            try:
                eq = await self.ex.equity(self.http)
                if self.start_equity is None:
                    self.start_equity = eq
                self.j.log("equity", equity=round(eq, 4), start=round(self.start_equity, 4))
                if eq < self.start_equity - self.max_loss:
                    self.j.log("loss_limit", equity=eq, start=self.start_equity, max_loss=self.max_loss)
                    self.stop.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.j.log("risk_error", error=repr(e)[:300])
            await asyncio.sleep(RISK_SECS)


def geoblock():
    try:
        return httpx.get(GEOBLOCK, headers=UA, timeout=15).json()
    except Exception as e:  # noqa: BLE001
        return {"blocked": None, "error": repr(e)}


# ---------- summary ----------

def summarize(journal_path):
    settles, fills, start, wallet, mode = [], [], None, None, None
    latency = {"cancel": [], "place": []}
    for line in open(journal_path, encoding="utf-8"):
        r = json.loads(line)
        if r["event"] == "start":
            start = start or r["ts"]
            wallet, mode = r.get("wallet"), r.get("mode")
        elif r["event"] == "settle":
            settles.append(r)
        elif r["event"] == "fill":
            fills.append(r)
        elif r["event"] in latency and "ms" in r:
            latency[r["event"]].append(r["ms"])
    traded = [s for s in settles if s["spent"] > 0]
    spent = sum(s["spent"] for s in traded)
    pnl = sum(s["pnl"] for s in traded)
    pairs = []
    for s in traded:
        sh, co = s["shares"], s["cost"]
        if sh["Up"] > 0 and sh["Down"] > 0:
            pairs.append(co["Up"] / sh["Up"] + co["Down"] / sh["Down"])
    pairs.sort()
    print("mode %s  wallet %s  since %s UTC" % (mode, wallet, datetime.fromtimestamp(
        start or 0, timezone.utc).strftime("%Y-%m-%d %H:%M")))
    print("windows settled %d, traded %d, fills %d" % (len(settles), len(traded), len(fills)))
    if spent:
        print("spent $%.2f  pnl $%+.2f  edge %+.2f%% (settlement, before fees/rebates)"
              % (spent, pnl, 100 * pnl / spent))
    if pairs:
        print("pair cost median %.3f (reference wallet ~0.963), both-sided windows %d/%d"
              % (pairs[len(pairs) // 2], len(pairs), len(traded)))
    if mode == "live":
        for name, xs in latency.items():
            if xs:
                xs = sorted(xs)
                print("%s round trip p50 %dms p90 %dms (n=%d)"
                      % (name, xs[len(xs) // 2], xs[int(len(xs) * 0.9)], len(xs)))
    print("start_ts %d" % int(start or 0))


async def amain(args, j, geo):
    ex = Live() if args.execute else Paper()
    kinds = [k.strip() for k in os.getenv("MR_KINDS", "5m").split(",") if k.strip()]
    async with httpx.AsyncClient(headers=UA, timeout=15) as http:
        bot = Bot(ex, load_params(), kinds, j, env_float("MR_MAX_LOSS_USD", 15.0), http, {"geo": geo})
        loop = asyncio.get_running_loop()

        def on_signal(sig):
            j.log("signal", sig=int(sig))
            bot.stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, on_signal, sig)
        await bot.run(args.minutes)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run")
    run.add_argument("--execute", action="store_true", help="post real orders (default: paper)")
    run.add_argument("--minutes", type=float, default=0, help="stop after N minutes (0 = until stopped)")
    run.add_argument("--journal", default=str(ROOT / "runtime" / "journal.jsonl"))
    sm = sub.add_parser("summary")
    sm.add_argument("--journal", default=str(ROOT / "runtime" / "journal.jsonl"))
    args = ap.parse_args()

    if args.cmd == "summary":
        summarize(args.journal)
        return
    if args.cmd != "run":
        ap.print_help()
        return

    j = Journal(args.journal)
    geo = geoblock()
    if args.execute and geo.get("blocked") is not False:
        j.log("refuse", reason="geoblock", geo=geo)
        sys.exit("refusing to trade: Polymarket geoblock check returned %s" % geo)
    if not args.execute and geo.get("blocked"):
        j.log("warn", reason="geoblock", geo=geo, note="paper mode only reads public books")
    if args.execute and not (os.getenv("PM_RELAYER_API_KEY") and os.getenv("PM_RELAYER_API_KEY_ADDRESS")):
        # Without redemption every winning window stays locked and $50 is used up in a few windows.
        j.log("refuse", reason="PM_RELAYER_API_KEY / PM_RELAYER_API_KEY_ADDRESS not set")
        sys.exit("refusing to trade: set PM_RELAYER_API_KEY and PM_RELAYER_API_KEY_ADDRESS (auto-redeem)")
    asyncio.run(amain(args, j, geo))


if __name__ == "__main__":
    main()
