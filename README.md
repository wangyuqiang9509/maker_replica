# maker_replica

A small, self-contained test of whether the two-sided maker strategy observed on
[@crouchpredictions](https://polymarket.com/@crouchpredictions) can be reproduced
with a ~$50 wallet on BTC 5m Up/Down markets.

It is separate from the momentum bot in the repository root and shares no code with it.

## What the reference wallet does (Oct 2026 sample, 1,095 windows)

- Buys **both** Up and Down in ~98% of windows, never sells, redeems ~65s after close.
- ~70% of fills are resting bids (maker); pair cost (avg Up + avg Down) median 0.941 weighted
  by pairs; 11% of its shares are bought at 0.01–0.02 (the cheap leg of 0.95 + 0.02 pairs).
- Per-fill settlement edge **+2.45%** gross, **-0.62%** taker fees, **+0.28%** maker rebate.
- Roughly half of its profit is trading edge and half is maker/taker rebates.
- Splitting each window into locked pairs and the one-sided leftover (5m markets, 823 windows):
  pairs **+$40k** (90% CI 35–45k), leftovers **−$26k** (CI −30k to −22k). The whole edge is in
  completing pairs; the hedge leg fills a median 21s after the first leg. Price buckets such as
  bids below 0.10 or between 0.50 and 0.70 show no significant edge on their own.

## What this bot does

- Rests one post-only bid per outcome, at the best bid or one tick inside the spread.
- Caps every bid at `MR_PAIR_CAP - other side's price`; once one side leads, the lagging
  side is priced against the leading side's average cost, so each completed pair costs
  at most `MR_PAIR_CAP`.
- Stops bidding the leading side at `MR_MAX_IMBALANCE` shares and stops a window at
  `MR_MAX_MARKET_USD`. Never sends taker orders.
- Re-prices on every streamed book update (see Latency).
- Cancels everything `MR_STOP_SECS` before close, holds to settlement, and redeems through
  the gasless relayer.
- Stops when equity falls `MR_MAX_LOSS_USD` below its starting value, and on `./run.sh stop`
  it cancels all open orders.

## Quick start

```bash
cd maker_replica
./run.sh                    # paper mode, foreground: live books, simulated fills, no keys
./run.sh summary            # settled paper windows: spent, PnL, pair cost

./run.sh latency            # network check of this machine; run before going live
cp .env.example .env        # fill in PM_PRIVATE_KEY, PM_FUNDER, relayer key
./run.sh start --execute    # live, background
./run.sh status | logs | stop
./run.sh report             # per-fill edge of this wallet vs the reference wallet
./run.sh test
```

`run.sh` creates a Python 3.12 venv with [uv](https://docs.astral.sh/uv/) on first use
and installs the official `polymarket-client` SDK.

Live mode refuses to start unless `https://polymarket.com/api/geoblock` reports
`blocked: false` for the machine it runs on, and unless the relayer key is set (winnings
must be redeemed every window for $50 to last). The account holder runs it on their own
account and funds.

Use a dedicated account: live mode calls `cancel_all` on startup and on stop, which
cancels every open order on the account, not only this bot's.

## Latency

The bot is event driven. Order books stream from the CLOB market WebSocket and are kept
locally (our own resting size is subtracted, so the bot never steps above its own bid).
Fills and order updates stream from the authenticated user WebSocket. On every book
update it cancels stale bids in one request, then posts the replacements concurrently.
REST only reconciles fills and open orders every 5 seconds and at settlement.

Most pick-offs follow a BTC move on other venues, and the Polymarket book reflects it
only later. So the bot also streams BTC trades from Binance: when BTC's move over the last
2 seconds implies a fair-price move of `MR_PULL_TICKS` or more (binary-option sensitivity,
with moneyness read from the Up mid), it cancels every bid that would add one-sided exposure
and keeps those bids off for `MR_PULL_COOLDOWN_SECS`. The hedge bid (the lagging side,
priced to complete pairs at a locked cost) is left alone: a BTC move cannot hurt a locked
pair, and the reversal that fires the pull is exactly when the hedge fills.

Measured over 14 minutes (440 two-tick moves of the Up mid), with the default settings:

| | Cancel round trip 330ms | Cancel round trip 50ms |
|---|---|---|
| book moves our cancel arrives before | 48% | 52% |
| bids off the market | 22% | 22% |
| pulls with no book move within 3s | 11% | 11% |

Once a BTC signal fires, the book usually follows 0.3–1.2s later, so network latency
matters less than signal coverage. The other half of the book moves have no BTC move
ahead of them on Binance, and resting bids stay exposed to those. The reference wallet
cancels in ~0.25s median. `./run.sh latency` measures the delays from the host it runs on:
Polymarket WebSocket, Binance stream, CLOB round trip, and how far the book lags the
Binance-triggered pull.

## Reading the result

`./run.sh report` wraps `fill_edge_report.py` and prints `NET` with a 90% interval that
is bootstrapped over markets. `NET` already includes fees and rebates.

| After 7–14 days | Meaning |
|---|---|
| lower bound > 0 | edge reproduced at this size; consider scaling |
| upper bound < 0 | not reproduced; stop |
| interval spans 0 | keep running up to 14 days |

Also compare the `maker share`, the pair cost (`./run.sh summary`), and the 10s markout
against the reference wallet. A markout well below the reference's -0.65 c/share means
your bids are being picked off more often than theirs.

Paper fills use a strict trade-through rule: the ask must fall below the bid. A paper bid
therefore only fills when the price moves against it, and never gets the fills a real bid
gets from takers hitting it while the price stands still. Paper mode checks the plumbing;
it cannot estimate the edge. Only a live run can.

## Files

- `bot.py` — event-driven engine (WebSocket books and fills), paper/live backends, settlement,
  redemption, risk stop, journal
- `quoting.py` — pure quoting and inventory logic (unit tested)
- `fill_edge_report.py` — read-only per-fill edge report from public endpoints
- `latency.py` — network check: stale-bid exposure vs the reference wallet
- `run.sh` — one-click runner; `.env.example` — every setting with defaults
- `runtime/` — `journal.jsonl` (every action), `bot.log`, `edge_report.json`
