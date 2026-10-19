# Proposal: cheap-side sizing and taker hedging

Status: design only, nothing implemented. Written after the first live run (2026-10-19).

## Why

Live, 17 settled windows, $188.60 spent: locked pairs **+$14.80**, one-sided leftovers
**−$45.54**. The reference wallet over the same 12 hours (137 windows): locked pairs
+$7,921, leftovers −$5,989, net +$1,932.

| per unit | reference | this bot |
|---|---|---|
| locked profit per pair | +9.4 c | +9 c |
| leftover side wins | 24% | 13% |
| leftover average cost | 0.33 | 0.45–0.64 |
| loss per leftover share | −14.6 c | −31 c |
| leftover shares / paired shares | 0.49 | 0.9 |

Pair economics are reproduced. The whole gap is the leftover: ours is twice as expensive
per share and twice as large relative to the pairs. The reference wallet nets about
2 c per pair (9.4 − 14.6 × 0.49); it lives on volume. Two structural changes aim at the
two leftover numbers. They do not touch the pair logic.

## Change 1: size orders in dollars, not shares

Today: 5 shares per side. The leftover is whichever side was falling when we got filled,
usually the 0.45–0.65 side, so losing it costs 0.45–0.65 per share.

Proposed: a dollar budget per order, `MR_ORDER_USD` (default 2.5), so

    shares(side) = max(5, round(MR_ORDER_USD / price))

At 0.80 / 0.20 this rests 5 Up and 12 Down. The expensive side cannot run ahead by much
(it is capped at 5 shares and `MR_MAX_IMBALANCE`), and the shares that end up unpaired
are mostly the cheap ones, which is what the reference wallet's 0.33 leftover cost shows
(a quarter of its shares are bought below 0.10).

Consequences to accept:
- `MR_MAX_IMBALANCE` becomes a dollar limit too (`MR_MAX_IMBALANCE_USD`, default 2.5):
  one unhedged order's worth, in either direction.
- The budget check (`max_market_usd`) and `committed` already work in dollars; nothing
  else in `compute_quotes` changes.
- Pair completion needs more cheap shares per expensive share; the hedge bid of the cheap
  side is sized to the dollar value of the naked expensive shares, not share for share.

## Change 2: hedge with a taker order when the pair is still worth it

Today the hedge is a resting bid at `pair_cap − avg(leading side)`; it fills in 21 s
median when it fills, but 44% of windows end with no hedge at all, and those windows
carry the whole loss.

Proposed: as soon as a side is naked (and the BTC pull is not active), look at the other
side's best ask. If

    avg(leading side) + best_ask + taker_fee(best_ask) <= MR_PAIR_CAP

buy the naked size at the ask with an immediate-or-cancel order (FAK). The pair is locked
at ≤ 0.97 including the fee, which is a smaller margin than a resting hedge but a sure
one. If the inequality fails, keep the resting hedge bid and stop adding to the naked side
(already the case). Never sell: the reference wallet never does, and a sale at the bid
pays the fee and the spread to lock a loss the leftover only has a 24% chance of
recovering.

Fee: the reference wallet paid 0.62% of all notional in taker fees with 31% of its
notional taken, so about 2% of taker notional. Use `MR_TAKER_FEE_PCT` (default 2.0) until
the exact schedule for 5m crypto markets is confirmed from the first live taker fill
(the user stream reports the fee per trade). The hedge is skipped when the fee tier is
unknown and `MR_TAKER_FEE_PCT` is 0.

SDK: `place_limit_order(..., post_only=False)` at the ask price with the FAK order type
(check the 0.13 client for the `order_type` argument; if only GTC exists, place at the ask
and cancel after one round trip if unfilled).

Budget: a taker hedge counts against `max_market_usd` like any fill; the per-window cap
stays at $12.

## What it should look like after a day

Measured by `./run.sh summary` and `./run.sh report`, against the reference wallet:

| metric | now | target | reference |
|---|---|---|---|
| paired windows | 56% | ≥ 85% | 98% |
| leftover shares / paired shares | 0.9 | ≤ 0.5 | 0.49 |
| leftover average cost | ~0.5 | ≤ 0.35 | 0.33 |
| loss per leftover share | −31 c | ≤ −15 c | −14.6 c |
| locked profit per pair | +9 c | ≥ +6 c (taker hedges lower it) | +9.4 c |

Net per pair = locked − (loss per leftover share × leftover ratio). With the targets:
6 − 15 × 0.5 = −1.5 c, i.e. still break-even at best; the reference wallet's +2 c per pair
needs its 9.4 c pair margin and its leftover profile together. So the honest expectation
is break-even, and the decision after a day is on the leftover numbers, not on PnL.

## What this does not fix

The 13% leftover win rate (against 24%) is adverse selection: our bids get hit later than
the reference wallet's get pulled. Cheap-side sizing makes those fills cost less; it does
not make them rarer. Only a faster cancel path (host near the CLOB, or a better BTC
signal than the current 48% coverage) changes that number. If after a day the loss per
leftover share is still above 20 c, stop; the strategy cannot be reproduced from this
host at this size.

## Stop criteria for the next run

- 100 windows (about 8 hours), then decide.
- `MR_MAX_LOSS_USD` stays at 15 on a $50 account.
- Stop early if leftover loss per share > 25 c after 50 windows.
