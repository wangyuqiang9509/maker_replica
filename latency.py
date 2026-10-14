#!/usr/bin/env python3
"""Network latency check for the machine that will run the bot. No keys needed.

The bot is event driven: a book update arrives on the market WebSocket, the
stale bid is cancelled (one CLOB round trip), then both replacements are posted
concurrently (one more). So the time from a price move on the exchange to our
stale bid being gone is about

    WebSocket one-way delay + one CLOB round trip

A BTC move on Binance usually reaches the Polymarket book later, so the bot also
pulls its bids straight from the Binance trade stream. That path's exposure is

    Binance one-way delay + one CLOB round trip

and the "BTC lead" section measures how long after our Binance-triggered pull the
Polymarket book itself moves by the same amount (that is the head start we get).

The reference wallet cancels a stale bid in ~0.25s median. CLOB calls here are
unauthenticated and return 401, but still make the full round trip to the CLOB origin.
"""
import asyncio
import json
import statistics as st
import time

import httpx

import quoting as q
from bot import SPOT_URL

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
REF = "0x21f4c1a1be34a39a7bc20985df4b7e5a39fb691d"
REFERENCE_CANCEL_MS = 250
LEAD_SECS = 180
PULL_TICKS, VOL, TICK = 1.5, 0.35, 0.01  # bot defaults (SpotSettings)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


async def bench(name, f, n=15):
    await f()
    xs = []
    for _ in range(n):
        t = time.perf_counter()
        await f()
        xs.append((time.perf_counter() - t) * 1000)
    print("%-36s p50 %5.0fms  p90 %5.0fms  max %5.0fms" % (name, st.median(xs), pct(xs, 0.9), max(xs)))
    return st.median(xs)


async def ws_delay(tokens, secs=30):
    import websockets
    lags = []
    t0 = time.perf_counter()
    async with websockets.connect(WS) as ws:
        connect = (time.perf_counter() - t0) * 1000
        await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
        end = time.time() + secs
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), 5)
            except asyncio.TimeoutError:
                continue
            now = time.time() * 1000
            msg = json.loads(raw)
            for ev in msg if isinstance(msg, list) else [msg]:
                # Book snapshots carry the time of the last change, not of the push.
                if isinstance(ev, dict) and ev.get("timestamp") and ev.get("event_type") != "book":
                    lags.append(now - float(ev["timestamp"]))
    return connect, lags


async def spot_delay(secs=15):
    import websockets
    lags = []
    async with websockets.connect(SPOT_URL) as ws:
        end = time.time() + secs
        while time.time() < end:
            m = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if m.get("E"):
                lags.append(time.time() * 1000 - m["E"])
    return lags


async def btc_lead(m, secs=LEAD_SECS):
    """Fire the bot's pull rule on Binance trades; time until the Polymarket Up mid moves as much."""
    import websockets
    from polymarket import AsyncPublicClient
    from polymarket.streams import MarketSpec
    toks = dict(zip(json.loads(m["outcomes"]), json.loads(m["clobTokenIds"])))
    end_ts = int(m["end_ts"])
    up = q.Book()
    hist = []                       # (local time, up mid)
    pending = []                    # [trigger time, mid at trigger, direction]
    leads, misses, triggers = [], 0, 0
    state = {"ref": None}
    c = AsyncPublicClient()
    sub = await c.subscribe(MarketSpec(token_ids=[toks["Up"]]))

    async def book():
        async for ev in sub:
            p = ev.payload
            if ev.type == "book" and p.asset_id == toks["Up"]:
                up.snapshot(p.bids, p.asks)
            elif ev.type == "price_change":
                for pc in p.price_changes:
                    if pc.asset_id == toks["Up"]:
                        up.level(pc.side, pc.price, pc.size)
                        up.trim(pc.best_bid, pc.best_ask)
            bb, ba = up.top()
            if bb is not None and ba is not None:
                now, mid = time.time(), (bb + ba) / 2
                hist.append((now, mid))
                for pnd in list(pending):
                    if (mid - pnd[1]) * pnd[2] >= PULL_TICKS * TICK - 1e-9:
                        leads.append((now - pnd[0]) * 1000)
                        pending.remove(pnd)

    async def spot():
        nonlocal misses, triggers
        async with websockets.connect(SPOT_URL) as ws:
            async for raw in ws:
                msg = json.loads(raw)
                if "p" not in msg or not hist:
                    continue
                now, price, mid = time.time(), float(msg["p"]), hist[-1][1]
                for pnd in list(pending):          # no matching book move within 5s
                    if now - pnd[0] > 5:
                        pending.remove(pnd)
                        misses += 1
                if state["ref"] is None:
                    state["ref"] = price
                    continue
                if q.fair_move(state["ref"], price, mid, end_ts - now, VOL) >= PULL_TICKS * TICK:
                    triggers += 1
                    pending.append([now, mid, 1 if price > state["ref"] else -1])
                    state["ref"] = price

    tasks = [asyncio.create_task(book()), asyncio.create_task(spot())]
    await asyncio.sleep(min(secs, max(10, end_ts - time.time() - 10)))
    for t in tasks:
        t.cancel()
    await sub.close()
    await c.close()
    return triggers, leads, misses


async def main():
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (maker-replica)"}, timeout=10) as c:
        start = int(time.time()) // 300 * 300
        m = (await c.get("%s/markets" % GAMMA, params={"slug": "btc-updown-5m-%d" % start})).json()[0]
        toks = json.loads(m["clobTokenIds"])
        print("geoblock:", (await c.get("https://polymarket.com/api/geoblock")).json())
        print()

        rtt = await bench("CLOB DELETE /orders (cancel)", lambda: c.request("DELETE", CLOB + "/orders", json=[]))
        await bench("CLOB POST /order (place)", lambda: c.post(CLOB + "/order", json={}))
        await bench("CLOB GET /data/trades (sync)", lambda: c.get(CLOB + "/data/trades"))
        await bench("gamma market", lambda: c.get("%s/markets" % GAMMA, params={"slug": "btc-updown-5m-%d" % start}))
        await bench("data-api /value", lambda: c.get("%s/value" % DATA_API, params={"user": REF}))

        async def reprice():
            await c.request("DELETE", CLOB + "/orders", json=[])
            await asyncio.gather(c.post(CLOB + "/order", json={}), c.post(CLOB + "/order", json={}))
        await bench("re-price (cancel, then 2 places)", reprice, n=8)
        nxt = start if time.time() - start < 200 else start + 300
        mm = (await c.get("%s/markets" % GAMMA, params={"slug": "btc-updown-5m-%d" % nxt})).json()[0]
        mm["end_ts"] = nxt + 300

    ws_ms = None
    try:
        connect, lags = await ws_delay(toks)
        if lags:
            ws_ms = st.median(lags)
            print("%-36s p50 %5.0fms  p90 %5.0fms  (%d events, connect %.0fms)"
                  % ("WS market one-way (clock-based)", ws_ms, pct(lags, 0.9), len(lags), connect))
    except Exception as e:  # noqa: BLE001
        print("WS market: failed", repr(e)[:200])

    spot_ms = None
    try:
        lags = await spot_delay()
        spot_ms = st.median(lags)
        print("%-36s p50 %5.0fms  p90 %5.0fms  (%d trades)" % ("Binance BTC one-way", spot_ms, pct(lags, 0.9),
                                                               len(lags)))
    except Exception as e:  # noqa: BLE001
        print("Binance stream: failed", repr(e)[:200], "- set MR_SPOT_URL to a reachable BTC trade stream")

    if nxt > time.time():
        await asyncio.sleep(nxt - time.time() + 5)
    print()
    print("BTC lead: firing the pull rule on Binance for up to %ds ..." % LEAD_SECS)
    triggers, leads, misses = await btc_lead(mm)
    if leads:
        print("%-36s p10 %5.0fms  p50 %5.0fms  p90 %5.0fms  (%d of %d pulls; %d with no book move in 5s)"
              % ("Polymarket book follows our pull by", pct(leads, 0.1), st.median(leads), pct(leads, 0.9),
                 len(leads), triggers, misses))
    else:
        print("no pull fired (%d triggers); BTC was quiet" % triggers)

    book_path = (ws_ms if ws_ms is not None else rtt / 2) + rtt
    react = min(book_path, (spot_ms + rtt) if spot_ms is not None else book_path)
    print()
    print("stale-bid exposure: book path ~%.0fms, BTC path ~%s; reference wallet ~%dms"
          % (book_path, "%.0fms" % (spot_ms + rtt) if spot_ms is not None else "n/a", REFERENCE_CANCEL_MS))
    if leads:
        ahead = sum(1 for x in leads if x > rtt) / len(leads)
        print("our cancel lands before the Polymarket book moves in %.0f%% of pulls "
              "(lead > one CLOB round trip of %.0fms)" % (100 * ahead, rtt))
    print("(one-way delays rely on this machine's clock; check it is NTP-synced)")
    if react <= REFERENCE_CANCEL_MS:
        print("verdict: OK (as fast as or faster than the reference wallet)")
    elif react <= 2 * REFERENCE_CANCEL_MS:
        print("verdict: MARGINAL (expect more adverse selection than the reference wallet)")
    else:
        print("verdict: TOO SLOW for resting bids on 5m markets; run from a host closer to the CLOB")


if __name__ == "__main__":
    asyncio.run(main())
