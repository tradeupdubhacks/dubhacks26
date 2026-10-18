# Model issues

Found on 2026-10-17 while connecting `model/live.py` to `npm start` on Linux and then measuring how the model trades. On Linux the server itself starts cleanly: the validator launches, `chain.ready` is `true`, and live fills settle as confirmed SPL transfers. Every problem below is in the model or in how the model fits the server's limits.

## Part 1. Connecting the model to the server

Items 1 to 4 are fixed in `model/live.py` and `model/policy.py`. Item 5 is only documented.

### 1. An idle server looked like a broken connection

With no live session, `/v1/stream` sends one snapshot and then nothing. `live.py` waited 10 seconds per message with `asyncio.wait_for(socket.recv(), timeout=10)`, raised `TimeoutError`, and printed:

```text
stream closed (). Reconnecting.
```

The message is empty because `str(TimeoutError())` is empty. On Windows, where `chain.ready` never becomes `true` and `POST /v1/session` returns `503`, this loop is all the bot ever prints.

**Fix.** On a quiet stream the bot sends `{"op": "ping"}` every 5 seconds and keeps the socket. It prints `waiting for a live session. Open one from the page.` once. Real errors print with `repr()`.

### 2. Cancel and place storm once a session opens

The server answers every stream command with a fresh snapshot (`publish()` in `src/app.ts`). A cancel or place waits `50ms` of market time, and in a live session market time only advances on the `200ms` tick. So the snapshot that answers a cancel still lists the order as open. The bot decided on every snapshot and had no record of what it had already sent, so it cancelled the same order again, which produced another snapshot, and so on.

Measured on a fresh `SOL-USD` session:

| | Before | After |
| --- | --- | --- |
| Commands sent | 6,177 cancels in 0.75s, then the socket stalled | 6 commands in 30s |
| Replies | Mostly `reject unknown_order` for orders already cancelled | All `ack` |

**Fix.** `live.py` keeps an `InFlight` record of places and cancels it has sent. It clears an entry when an `events` message reports a reject, `canceled`, or `filled`, or when the snapshot catches up. Entries expire after 2 seconds. The policy sees the book as it will look once those commands land. An order is never cancelled twice, and the bot decides at most every `100ms`.

### 3. Duplicate quotes at the same price

The same stale-snapshot race placed two orders at one price (`bot-2` and `bot-3` both sell at `10277`). `decide()` kept every resting order at the target price, so duplicates stayed on the book.

**Fix.** `decide()` keeps one resting quote per side and cancels the rest. The `InFlight` view also stops the race from creating them.

### 4. Order ids reused after a reconnect

`IdSource` started at `bot-1` on every connection. The server caches every `clientOrderId` for idempotency, so after a reconnect a new order with an old id came back as a duplicate of the earlier order.

**Fix.** `IdSource(prefix=...)` takes a prefix. `live.py` uses a millisecond timestamp, for example `bot1a14c38d8fc-1`. The default prefix is still `bot`, so the tests are unchanged.

### 5. Log noise on halt and pointless flattens

After `POST /v1/stop` the stream keeps sending snapshots of the halted session. The bot printed `session halted` on every one. Near a rail on a flat book it sent `flatten` every decision even with nothing to flatten.

**Fix.** The halt message prints once per halt. A `flatten` or `cancel_all` is skipped when there is no position and no working order.

### Verified after the fixes

- `pytest tests`: 14 passed.
- A live `BTC-USDT` session (deposit `$100`, floor `$0`, ceiling `$200`) got 22 maker fills in 45 seconds. Each fill's `signature` is a confirmed transaction on the local validator (`solana confirm` returns `Confirmed`).

## Part 2. How good the model is

### Question asked

With a `$1000` house fund and a `$5` user deposit, on data the model has not seen, how long does the stake take to reach `+$5`?

Stake is `deposit + pool PnL`, so the whole pool must earn `$5`. A session stops at the floor or the ceiling, so the test uses floor `$0` (lose the `$5`) and ceiling `$10` (gain `$5`).

### Method

- **Held-out data.** The server's BTC replay is hard-coded to `BTCUSDT_20261001_*` in `src/tape.ts`. That is the day the model was trained on. Other days could not be downloaded: `datasets.tardis.dev` now answers `403` (Cloudflare) for every day, including 2026-10-01, and Binance's public archive has no 2026 futures `bookTicker`. The test therefore uses the last part of 2026-10-01. The model's own split in `model/artifacts/eval_report.json` set that part aside as the test slice. It was not used to fit the network or to pick the threshold. It starts at timestamp `1790871729793000` and covers about 7.6 hours.
- **Replay.** A second server instance runs `buildApp()` with that tape, `clock: false`, and no chain. Matching, fees, latency, and rails are the same code. Only the SPL transfer is skipped. A driver opens a `BTC-USDT` session, calls `POST /v1/step` with `dtMs: 200` (the live tick), and passes each observation to the model's own `decide()` with the saved `signal.pt` weights. 7.6 market hours replay in about a minute.
- **Bigger fund.** A copy of `src/` with `HOUSE_CENTS` and `DEPOSIT_MAX` raised to `$10,000`. The repo's `src/` was not changed.

### Results

| Run | Data | Fund / deposit / floor / ceiling | Result |
| --- | --- | --- | --- |
| Stock bot | Held-out | `$1000` / `$5` / `$0` / `$10` | **0 orders.** See issue 6. |
| Bot rail guard bypassed | Held-out | `$1000` / `$5` / `$0` / `$10` | **Lost the `$5` in 13.3 min.** 325 fills, never above `$0`, halted on the floor at `-$5.25`. |
| Full window, wide rails | Held-out, 7.6 h | `$1000` / `$100` / `$0` / none | **`-$98.10`.** Never above `$0`. 6,416 fills, all maker, 0 taker. Fees `$64.16`. |
| Back-to-back `$5` episodes cut from that run | Held-out, 7.6 h | `$5` each | **0 of 19 reached `+$5`. 19 lost the `$5`.** Median time to lose it 14.9 min (4.6 to 63.6). Largest rise from any low point: `$0.64`. |
| Live data | Binance `BTCUSDT`, 2026-10-17, 1 h | `$1000` / `$100` / `$0` / none | **`-$2.58`.** Never above `$0`. 248 fills, all maker. Fees `$2.48`. |
| Training-day control | First 2 h of 2026-10-01 (trained on) | `$1000` / `$100` / `$0` / none | **`-$15.24`.** Never above `$0`. 1,390 fills, fees `$13.90`. |
| Stock bot, `$10,000` | Held-out, 7.6 h | `$10,000` / `$10,000` / `$0` / `+$5` | **0 orders.** See issue 6. |
| Stock bot, `$10,000`, wide ceiling | Held-out, 7.6 h | `$10,000` / `$10,000` / `$0` / none | **`-$98.10`**, the same to the cent as the `$1000` run. Never reached `+$5`. |

**Answer: on held-out data the model does not reach `+$5` at any fund size.** It loses about `$12.86` per hour on the pool.

The episode cut takes the next episode's start from where the last one ended in a single continuous run. The position is not flattened at each boundary. Positions are at most 10 lots and are marked to market, so the PnL is consistent, but the cut is close to back-to-back sessions, not exact.

**Live data.** One hour of Binance USD-M `BTCUSDT` quotes and trades was recorded on 2026-10-17 from 16:44 to 17:44 (645,798 quote updates, 61,813 trades, no gaps). It was written in the replay format and run through the same harness with wide rails. Result: **`-$2.58`, never above `$0`**. 248 fills, all maker, 0 taker. Fees `$2.48`, so `-$0.10` before fees. The calmer hour filled the bot about a third as often as the held-out day (248 per hour against about 840), so it lost more slowly. It still did not reach `+$5`. At `-$2.58` an hour, a `$5` stake would reach its floor in about 2 hours.

### 6. A `$5` stake, or a `$5` target, blocks every BTC order

`_near_rail()` in `model/policy.py` refuses to quote when one lot's notional reaches either rail. One `BTC-USDT` lot is `0.001` BTC, about `$84`.

- A `$5` deposit with a `$0` floor is `$5` from the floor. The bot never quotes.
- A `$10,000` deposit with the ceiling at `+$5` is `$5` from the ceiling. The bot never quotes.

Any `BTC-USDT` session with less than about `$84` of room to either rail makes no trades. `SOL-USD` lots are about `$0.10`, so it does not hit this limit. The page allows deposits of `$1` to `$100`, so most BTC sessions a user can create never trade.

### 7. The neural network never trades

A taker order needs `confidence >= threshold`, and the saved threshold is `0.99`. `eval_report.json` explains why: no taker threshold beat doing nothing after the 5 bps fee, so calibration chose a value that refuses taker orders. Every replay above had 0 taker fills.

The passive quoting branch of `decide()` does not use the score. It joins the best bid and ask with 1 lot. In practice the bot's behavior is a fixed market-making rule, and the network has no effect on any order.

### 8. Fees are most of the loss, and the strategy loses without them

The maker fee is 2 bps, rounded down to whole cents (`feeCents` in `src/types.ts`). On one BTC lot that is 1 cent per fill. The bot fills often, so fees add up:

| Window | Net PnL | Fees | PnL before fees |
| --- | --- | --- | --- |
| Held-out, 7.6 h | `-$98.10` | `$64.16` | `-$33.94` |
| Training day, 2 h | `-$15.24` | `$13.90` | `-$1.34` |

These runs have no binding rails, so the bot's orders do not depend on fees, and PnL before fees is net PnL plus fees. Even before fees the strategy loses on held-out data. Its quotes at the touch tend to fill just before the price moves against them (adverse selection).

### 9. A bigger fund does not help

The bot always sends 1 lot, and `maxPositionLots` for `BTC-USDT` is 10. The fund only sets how much loss fits before the floor. A larger fund helps only if order size scales with it, and with a strategy that loses per fill, that would scale the loss too.

## Other notes

- `model/train.py` and `eval_report.json` mention an RTX 4080. This machine has no GPU. A CPU build of PyTorch (`2.14.1+cpu` on Python 3.14) in `.venv` runs `model.live` and the tests.
- `model/__pycache__` and `tests/__pycache__` `.pyc` files are tracked in git and change whenever the tests run.
- `hft/download_sample.py` can no longer download data because of the Tardis `403`.

## What would need to change

1. Give the bot a signal that beats the fee. The current one does not do so even on the day it was trained on.
2. Stop quoting at the touch on every tick. Quote only when the signal favors the side, or quote wider than the touch.
3. Make the rail check fit the instrument. Either size BTC orders below one lot, or refuse BTC sessions with less than one lot of room to a rail at `POST /v1/session` instead of accepting a session that can never trade.
4. Evaluate on more than one day. That needs a working data source to replace Tardis, or longer live recordings.
