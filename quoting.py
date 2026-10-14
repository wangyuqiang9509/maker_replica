"""Pure quoting logic for the two-sided BTC Up/Down maker.

Reconstructed from @crouchpredictions' fills (Oct 2026 sample):
  - buys both outcomes in almost every window and holds to settlement,
  - ~70% of fills are resting bids (maker), pair cost (avg Up + avg Down) median ~0.96,
  - the side that just fell gets filled more, so inventory drifts toward it.

The rule set here: rest one post-only bid per outcome, priced at the best bid
(or one tick better when the spread allows) but never above
`pair_cap - reference price of the other outcome`. When one side is ahead in
shares, the reference for the lagging side is the leading side's average cost,
so completing a pair always locks a pair cost <= pair_cap. The leading side
stops bidding once it is `max_imbalance` shares ahead. No taker orders: at a
small Taker Rebate tier the ~2% taker fee eats the edge.

Everything here is side-effect free so it can be unit tested and replayed.
"""
import math
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from statistics import NormalDist

OUTCOMES = ("Up", "Down")
EPS = 1e-9
SECONDS_PER_YEAR = 365 * 24 * 3600
_N = NormalDist()


@dataclass(frozen=True)
class Params:
    order_shares: float = 5.0        # per resting order; Polymarket minimum is 5
    max_market_usd: float = 12.0     # total spend cap per window
    pair_cap: float = 0.97           # max avg(Up) + avg(Down) we accept
    max_imbalance: float = 15.0      # shares one side may lead before it stops bidding
    min_price: float = 0.03
    max_price: float = 0.97
    start_delay_secs: int = 3        # skip the opening seconds of a window
    stop_secs: int = 5               # cancel everything this long before close
    improve: bool = True             # step one tick inside the spread when possible


@dataclass
class Inventory:
    shares: dict = field(default_factory=lambda: {o: 0.0 for o in OUTCOMES})
    cost: dict = field(default_factory=lambda: {o: 0.0 for o in OUTCOMES})

    def add(self, outcome, side, size, price):
        sign = 1.0 if side == "BUY" else -1.0
        self.shares[outcome] += sign * size
        self.cost[outcome] += sign * size * price

    def avg(self, outcome):
        s = self.shares[outcome]
        return self.cost[outcome] / s if s > EPS else None

    @property
    def spent(self):
        return self.cost["Up"] + self.cost["Down"]


def best_bid_ask(book):
    """(best_bid, best_ask) from a book with .bids/.asks levels in any order."""
    bids = [float(l.price) for l in book.bids if float(l.size) > 0]
    asks = [float(l.price) for l in book.asks if float(l.size) > 0]
    return (max(bids) if bids else None, min(asks) if asks else None)


class Book:
    """Price-level book for one token, kept from a snapshot plus streamed level updates."""

    def __init__(self):
        self.bids, self.asks = {}, {}
        self.ready = False

    def snapshot(self, bids, asks):
        self.bids = {round(float(l.price), 6): float(l.size) for l in bids if float(l.size) > 0}
        self.asks = {round(float(l.price), 6): float(l.size) for l in asks if float(l.size) > 0}
        self.ready = True

    def level(self, side, price, size):
        """Set the aggregate size at one level (size 0 removes it)."""
        levels = self.bids if side == "BUY" else self.asks
        price = round(float(price), 6)
        if float(size) > 0:
            levels[price] = float(size)
        else:
            levels.pop(price, None)

    def trim(self, best_bid, best_ask):
        """Drop levels beyond the server's reported top; matched levels can be removed a message late."""
        if best_bid:
            for p in [p for p in self.bids if p > float(best_bid) + EPS]:
                del self.bids[p]
        if best_ask and float(best_ask) < 1:
            for p in [p for p in self.asks if p < float(best_ask) - EPS]:
                del self.asks[p]

    def top(self, own_bids=None):
        """(best_bid, best_ask) after removing our own resting bid size, {price: size}.

        Without this the bot would see its own improved bid as the market and step above it again.
        """
        own = own_bids or {}
        bids = [p for p, sz in self.bids.items() if sz - own.get(p, 0.0) > EPS]
        return (max(bids) if bids else None, min(self.asks) if self.asks else None)


def floor_tick(price, tick):
    t = Decimal(str(tick))
    return float((Decimal(str(price)) / t).to_integral_value(rounding=ROUND_FLOOR) * t)


def other(outcome):
    return "Down" if outcome == "Up" else "Up"


def compute_quotes(tops, tick, inv, params, secs_left, secs_in, committed=None):
    """Target bid per outcome.

    tops: {outcome: (best_bid, best_ask)}; committed: {outcome: USD resting in our open bid}.
    A quote replaces its own outcome's bid, so only the other outcome's resting USD counts.
    Returns {outcome: (price | None, reason)}.
    """
    if secs_left <= params.stop_secs:
        return {o: (None, "closing") for o in OUTCOMES}
    if secs_in < params.start_delay_secs:
        return {o: (None, "opening") for o in OUTCOMES}

    out = {}
    for o in OUTCOMES:
        bb, ba = tops.get(o, (None, None))
        ob, _ = tops.get(other(o), (None, None))
        if bb is None or ba is None or ob is None:
            out[o] = (None, "no_book")
            continue
        if bb >= ba - EPS:
            out[o] = (None, "crossed")  # transient while a match is still being streamed
            continue
        lead = inv.shares[o] - inv.shares[other(o)]
        if lead >= params.max_imbalance - EPS:
            out[o] = (None, "imbalance")
            continue

        px = bb + tick if params.improve and ba - bb > tick + EPS else bb
        px = min(px, ba - tick)  # post-only: never touch the ask
        # Lagging side completes pairs against our own average; otherwise price against the other bid.
        ref = inv.avg(other(o)) if lead < -EPS else ob
        px = floor_tick(min(px, params.pair_cap - ref) + EPS, tick)

        if px < params.min_price - EPS or px > params.max_price + EPS:
            out[o] = (None, "price_band")
        elif inv.spent + (committed or {}).get(other(o), 0.0) + px * params.order_shares > params.max_market_usd + EPS:
            out[o] = (None, "budget")
        else:
            out[o] = (round(px, 6), "ok")
    return out


def fair_move(spot_ref, spot, mid_up, secs_left, vol):
    """Estimated change in an Up/Down outcome's fair price after BTC moves spot_ref -> spot.

    Up is a binary option on BTC finishing above the window's open price. Its price
    sensitivity is pdf(z) / (sigma * sqrt(tau)) per unit of log return, and the market's
    own Up mid gives z = inv_cdf(mid) without knowing the strike.
    """
    if not spot_ref or not spot:
        return 0.0
    m = min(max(mid_up if mid_up is not None else 0.5, 0.01), 0.99)
    sigma_tau = vol * math.sqrt(max(secs_left, 1.0) / SECONDS_PER_YEAR)
    return _N.pdf(_N.inv_cdf(m)) * abs(math.log(spot / spot_ref)) / sigma_tau


def _get(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def fills_from_trades(trades, wallet, api_key=None):
    """Our fills from CLOB trades (REST or user stream): ({fill_key: (outcome, side, size, price, role)}, failed_ids).

    As taker the trade row is ours. As maker only our entries in maker_orders count (matched by
    maker address, or by API key when the stream omits the address); the top-level row then
    describes the counterparty's taker order. Trades that FAILED on chain are returned as ids so
    a fill counted while MATCHED can be dropped again.
    """
    wallet = wallet.lower()
    out, failed = {}, set()
    for t in trades:
        tid = _get(t, "id")
        if str(_get(t, "status") or "").upper() == "FAILED":
            failed.add(tid)
            continue
        if str(_get(t, "trader_side") or "").upper() == "TAKER":
            out[(tid, "taker")] = (_get(t, "outcome"), str(_get(t, "side")).upper(),
                                   float(_get(t, "size")), float(_get(t, "price")), "taker")
            continue
        for mo in _get(t, "maker_orders") or ():
            addr = str(_get(mo, "maker_address") or "").lower()
            if addr != wallet and not (api_key and _get(mo, "owner") == api_key):
                continue
            out[(tid, _get(mo, "order_id"))] = (_get(mo, "outcome"), str(_get(mo, "side")).upper(),
                                                float(_get(mo, "matched_amount")), float(_get(mo, "price")),
                                                "maker")
    return out, failed


def inventory_from(fills):
    inv = Inventory()
    for outcome, side, size, price, _role in fills.values():
        inv.add(outcome, side, size, price)
    return inv


def paper_fill(resting_price, best_ask):
    """Dry-run fill rule: a resting bid fills only once the ask trades through it.

    Stricter than touching, because a real bid at that price would sit behind the queue.
    """
    return resting_price is not None and best_ask is not None and best_ask < resting_price - EPS
