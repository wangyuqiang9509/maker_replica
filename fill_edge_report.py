#!/usr/bin/env python3
"""Per-fill edge report for BTC Up/Down markets.

Measures, for one or more wallets, how much each fill was worth at settlement
and shortly after it happened. The point is to compare a small test wallet
against a reference wallet (e.g. @crouchpredictions) on the same markets and
the same window, using per-fill metrics instead of noisy daily PnL.

Metrics (all as % of fill notional, size * price):
  gross   settlement payout minus price paid (sign-flipped for sells)
  fee     taker fee: size * rate * p * (1 - p), rate from the market's feeSchedule
  rebate  maker rebate estimate: rebateRate * the same fee-equivalent on maker fills
          (the pool is split by fee-equivalent, so a maker earns ~its own share)
  net     gross - fee + rebate + taker_rebate_pct * fee
  markout price of the same outcome H seconds after the fill minus fill price,
          from the public taker tape (complement price used for the other side)

The 90% interval is a bootstrap over markets, not fills: fills inside one
market share the same outcome, so fills are not independent samples.

Read-only: only public HTTP endpoints, no keys, no orders.
"""
import argparse
import bisect
import json
import random
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (fill-edge-report)"}
DURATIONS = {"5m": 300, "15m": 900}
PAGE = 1000
# data-api rejects offsets past this; markets beyond it are reported as truncated.
MAX_OFFSET = 3000


# ---------- pure helpers ----------

def market_slugs(start_ts, end_ts, kinds):
    """All btc-updown slugs whose window lies fully inside [start_ts, end_ts]."""
    out = []
    for kind in kinds:
        dur = DURATIONS[kind]
        t = (start_ts + dur - 1) // dur * dur
        while t + dur <= end_ts:
            out.append(("btc-updown-%s-%d" % (kind, t), t, dur))
            t += dur
    return out


def taker_fee(size, price, rate):
    return size * rate * price * (1.0 - price)


def build_rows(fills, market, taker_hashes):
    """One row per fill with settlement value, fee and rebate estimates."""
    rows = []
    for f in fills:
        size = float(f["size"])
        price = float(f["price"])
        sign = 1.0 if f["side"] == "BUY" else -1.0
        won = 1.0 if f["outcome"] == market["winner"] else 0.0
        role = "taker" if f["transactionHash"] in taker_hashes else "maker"
        fee_eq = taker_fee(size, price, market["fee_rate"])
        rows.append({
            "market": market["slug"],
            "ts": int(f["timestamp"]),
            "outcome": f["outcome"],
            "side": f["side"],
            "role": role,
            "price": price,
            "size": size,
            "notional": size * price,
            "gross": sign * size * (won - price),
            "fee": fee_eq if role == "taker" else 0.0,
            "rebate": market["rebate_rate"] * fee_eq if role == "maker" else 0.0,
            "secs_to_close": market["end"] - int(f["timestamp"]),
        })
    return rows


def tape_index(tape):
    """{outcome: (sorted ts list, prices)} with complement prices for the other side."""
    outcomes = sorted({t["outcome"] for t in tape})
    series = {}
    for o in outcomes:
        pts = sorted((int(t["timestamp"]), float(t["price"]) if t["outcome"] == o else 1.0 - float(t["price"]))
                     for t in tape)
        series[o] = ([p[0] for p in pts], [p[1] for p in pts])
    return series


def markout(row, series, horizon):
    """Signed per-share move of the filled outcome `horizon` seconds later; None if the tape has no print."""
    s = series.get(row["outcome"])
    # A truncated tape keeps only the newest prints; never mark against prints from long after the fill.
    if not s or s[0][0] > row["ts"]:
        return None
    i = bisect.bisect_left(s[0], row["ts"] + horizon)
    if i >= len(s[0]):
        return None
    move = s[1][i] - row["price"]
    return move if row["side"] == "BUY" else -move


def net_value(row, taker_rebate_pct):
    return row["gross"] - row["fee"] + row["rebate"] + taker_rebate_pct * row["fee"]


def bootstrap_ci(rows, value, iters=1000, seed=7):
    """5th/95th percentile of sum(value)/sum(notional), resampling whole markets."""
    by_m = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        acc = by_m[r["market"]]
        acc[0] += value(r)
        acc[1] += r["notional"]
    groups = list(by_m.values())
    if len(groups) < 2:
        return None, None
    rng = random.Random(seed)
    ests = []
    for _ in range(iters):
        v = n = 0.0
        for _ in groups:
            g = groups[rng.randrange(len(groups))]
            v += g[0]
            n += g[1]
        if n:
            ests.append(v / n)
    ests.sort()
    return ests[int(0.05 * len(ests))], ests[int(0.95 * len(ests)) - 1]


def summarize(rows, taker_rebate_pct):
    notional = sum(r["notional"] for r in rows)
    if not notional:
        return None

    def pct(x):
        return 100.0 * x / notional

    net = [net_value(r, taker_rebate_pct) for r in rows]
    lo, hi = bootstrap_ci(rows, lambda r: net_value(r, taker_rebate_pct))
    maker_notional = sum(r["notional"] for r in rows if r["role"] == "maker")
    return {
        "markets": len({r["market"] for r in rows}),
        "fills": len(rows),
        "notional": notional,
        "maker_share": maker_notional / notional,
        "gross_pct": pct(sum(r["gross"] for r in rows)),
        "fee_pct": pct(sum(r["fee"] for r in rows)),
        "rebate_pct": pct(sum(r["rebate"] for r in rows)),
        "taker_rebate_pct": pct(taker_rebate_pct * sum(r["fee"] for r in rows)),
        "net_pct": pct(sum(net)),
        "net_ci90": (None if lo is None else 100.0 * lo, None if hi is None else 100.0 * hi),
        "pnl_net": sum(net),
    }


def bucket_table(rows, key, taker_rebate_pct):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for k in sorted(groups):
        g = groups[k]
        n = sum(r["notional"] for r in g)
        out.append((k, len(g), n,
                    100.0 * sum(r["gross"] for r in g) / n,
                    100.0 * sum(net_value(r, taker_rebate_pct) for r in g) / n))
    return out


def price_bucket(r):
    b = min(9, int(r["price"] * 10))
    return "%.1f-%.1f" % (b / 10.0, b / 10.0 + 0.1)


def ttc_bucket(r):
    for edge in (30, 60, 120, 180, 300, 600, 900):
        if r["secs_to_close"] < edge:
            return "<%03ds" % edge
    return ">=900s"


# ---------- network ----------

def http_json(url, retries=4):
    err = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - public endpoints flap; retry then surface
            err = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("GET %s failed: %s" % (url, err))


def fetch_markets(slugs, workers):
    """slug list -> {conditionId: market dict}; skips unresolved markets."""
    meta = {s: (start, dur) for s, start, dur in slugs}
    names = [s for s, _, _ in slugs]
    chunks = [names[i:i + 40] for i in range(0, len(names), 40)]

    def one(chunk):
        q = urllib.parse.urlencode([("slug", s) for s in chunk] + [("closed", "true"), ("limit", "100")])
        return http_json("%s/markets?%s" % (GAMMA, q))

    markets = {}
    with ThreadPoolExecutor(workers) as ex:
        for batch in ex.map(one, chunks):
            for m in batch:
                outcomes = json.loads(m.get("outcomes") or "[]")
                prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                if m["slug"] not in meta or len(prices) != 2 or sorted(prices) != [0.0, 1.0]:
                    continue
                start, dur = meta[m["slug"]]
                fs = m.get("feeSchedule") or {}
                markets[m["conditionId"]] = {
                    "slug": m["slug"],
                    "start": start,
                    "end": start + dur,
                    "winner": outcomes[prices.index(1.0)],
                    "fee_rate": float(fs.get("rate", 0.0)) if m.get("feesEnabled") else 0.0,
                    "rebate_rate": float(fs.get("rebateRate", 0.0)) if m.get("feesEnabled") else 0.0,
                }
    return markets


def fetch_paged(base):
    rows, offset, truncated = [], 0, False
    while True:
        page = http_json("%s&limit=%d&offset=%d" % (base, PAGE, offset))
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
        if offset >= MAX_OFFSET:
            truncated = True
            break
    return rows, truncated


def fetch_wallet_market(wallet, cid):
    base = "%s/trades?user=%s&market=%s" % (DATA_API, wallet, cid)
    fills, t1 = fetch_paged(base + "&takerOnly=false")
    if not fills:
        return [], set(), False
    taker, t2 = fetch_paged(base + "&takerOnly=true")
    return fills, {t["transactionHash"] for t in taker}, t1 or t2


def fetch_tape(cid):
    tape, truncated = fetch_paged("%s/trades?market=%s&takerOnly=true" % (DATA_API, cid))
    return tape, truncated


# ---------- report ----------

def parse_ts(s):
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def fmt_ci(ci):
    if ci[0] is None:
        return "n/a"
    return "[%+.2f%%, %+.2f%%]" % ci


def print_report(wallet, rows, markouts, args, truncated):
    s = summarize(rows, args.taker_rebate_pct)
    print("\n=== %s ===" % wallet)
    if not s:
        print("no fills in window")
        return None
    print("markets %d  fills %d  notional $%.2f  maker share %.0f%%"
          % (s["markets"], s["fills"], s["notional"], 100 * s["maker_share"]))
    print("gross %+.2f%%  taker fee -%.2f%%  maker rebate +%.2f%%  taker rebate +%.2f%%"
          % (s["gross_pct"], s["fee_pct"], s["rebate_pct"], s["taker_rebate_pct"]))
    print("NET %+.2f%%  90%% CI %s  (net $%+.2f)" % (s["net_pct"], fmt_ci(s["net_ci90"]), s["pnl_net"]))
    if truncated:
        print("warning: %d markets hit the data-api offset cap; fills there are incomplete" % truncated)
    for h, vals in sorted(markouts.items()):
        if vals:
            w = sum(sz for _, sz in vals)
            print("markout %3ds: %+.2f c/share over %d fills (%d without a later print skipped)"
                  % (h, 100 * sum(m * sz for m, sz in vals) / w, len(vals), len(rows) - len(vals)))
    for title, key in (("role", lambda r: r["role"]), ("price", price_bucket), ("time to close", ttc_bucket)):
        print("-- by %s --" % title)
        for k, n, notional, gross, net in bucket_table(rows, key, args.taker_rebate_pct):
            print("  %-10s fills %6d  $%10.2f  gross %+6.2f%%  net %+6.2f%%" % (k, n, notional, gross, net))
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wallet", action="append", required=True,
                    help="proxy wallet address; repeat to compare wallets on the same markets")
    ap.add_argument("--hours", type=float, default=24.0, help="look-back window ending now (default 24)")
    ap.add_argument("--start", help="window start, unix seconds or ISO-8601 UTC (overrides --hours)")
    ap.add_argument("--end", help="window end, unix seconds or ISO-8601 UTC (default now)")
    ap.add_argument("--kinds", default="5m,15m", help="comma list of 5m,15m")
    ap.add_argument("--taker-rebate-pct", type=float, default=0.0,
                    help="fraction of taker fees refunded by your Taker Rebate tier, e.g. 0.18 for Gold")
    ap.add_argument("--markout", default="10,60",
                    help="comma list of markout horizons in seconds; empty to skip the tape fetch")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json-out", help="write fill rows and summaries here")
    args = ap.parse_args()

    end = parse_ts(args.end) if args.end else int(time.time())
    start = parse_ts(args.start) if args.start else int(end - args.hours * 3600)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    horizons = [int(h) for h in args.markout.split(",") if h.strip()]
    wallets = [w.lower() for w in args.wallet]

    slugs = market_slugs(start, end, kinds)
    markets = fetch_markets(slugs, args.workers)
    print("window %s -> %s UTC: %d windows, %d resolved"
          % (datetime.fromtimestamp(start, timezone.utc).strftime("%m-%d %H:%M"),
             datetime.fromtimestamp(end, timezone.utc).strftime("%m-%d %H:%M"), len(slugs), len(markets)),
          file=sys.stderr)

    jobs = [(w, cid) for w in wallets for cid in markets]
    with ThreadPoolExecutor(args.workers) as ex:
        fetched = list(ex.map(lambda j: (j, fetch_wallet_market(*j)), jobs))

    rows_by_wallet = defaultdict(list)
    truncated = defaultdict(int)
    traded = set()
    for (w, cid), (fills, taker_hashes, trunc) in fetched:
        if fills:
            rows_by_wallet[w].extend(build_rows(fills, markets[cid], taker_hashes))
            traded.add(cid)
            truncated[w] += int(trunc)

    tapes = {}
    if horizons and traded:
        with ThreadPoolExecutor(args.workers) as ex:
            for cid, (tape, _) in zip(traded, ex.map(fetch_tape, traded)):
                tapes[markets[cid]["slug"]] = tape_index(tape)

    out = {"start": start, "end": end, "wallets": {}}
    for w in wallets:
        rows = rows_by_wallet.get(w, [])
        mk = {h: [] for h in horizons}
        for r in rows:
            series = tapes.get(r["market"])
            for h in horizons:
                m = markout(r, series, h) if series else None
                if m is not None:
                    mk[h].append((m, r["size"]))
        s = print_report(w, rows, mk, args, truncated[w])
        out["wallets"][w] = {"summary": s, "rows": rows}

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f)


if __name__ == "__main__":
    main()
