# Client API

The live page uses this API. It opens a dummy session, watches one person's stake, and stops that session. It cannot place, cancel, or flatten orders. Those calls belong to the [model API](model-api.md).

Base URL: `http://localhost:3000`

WebSocket: `ws://localhost:3000`

All JSON bodies use `Content-Type: application/json`. There is no authentication. The server is a single localhost process with one session at a time. Balances and fills are dummy ledger entries. Nothing is sent to a real exchange or chain.

## Units

| Field | Meaning |
| --- | --- |
| `*Cents` | Dummy USD cents. `500` is `$5.00`. The house fund is `10000000` (`$100,000`). |
| `price` / `midCents` | Integer USD cents for **one** whole base coin. `$104.00` SOL is `10400`. A `$60,000.00` BTC is `6000000`. |
| `size` / `positionLots` | Integer lots. One lot is `0.001` of the base coin. `10` lots is `0.01` BTC or SOL. |
| Timestamps | ISO-8601 UTC strings, millisecond precision. |

Prices on the wire are cents, not ticks. `BTC-USDT` prices must be a multiple of `10` cents (`$0.10`). `SOL-USD` prices must be a multiple of `1` cent.

Notional of a fill, in cents:

```text
round(priceCents * lots / 1000)
```

## Session rules

The house fund is `$100,000`. The user adds a deposit. The model trades the pooled cash. The page shows the user's stake, not the house fund.

```text
stakeCents = depositCents + poolPnlCents
```

`poolPnlCents` is the pool's session profit and loss, fees included. It is not a pro-rata slice. A `$5` deposit moves by the full dollar profit or loss of the pool, and trading stops at the rails below so the user does not lose more than the deposit and does not gain past the ceiling.

Rails:

- **Floor.** Optional. Default `0`. Valid range is `0 <= floorCents < depositCents`. Trading halts when `stakeCents` falls to the floor or below.
- **Ceiling.** Required. Must be strictly greater than `depositCents`. Trading halts when `stakeCents` reaches the ceiling or above.
- **Stop.** `POST /v1/stop` flattens and halts immediately.

Floor and ceiling are fixed when the session is created. The page cannot move them while the session is running. A different deposit, instrument, floor, or ceiling is a new session.

A halt cancels every open order, flattens the position with a fake market fill that does not wait for latency, freezes `stakeCents`, and rejects later model orders. `halt.reason` is `floor`, `ceiling`, or `manual`.

Deposit bounds: `100` through `10000` cents (`$1.00` through `$100.00`).

## Multiple sessions

Up to 20 people can trade at once. Each `POST /v1/session` creates a separate session with its own position, cash, stake, floor, ceiling, and strategy. All sessions read the same live Bitcoin book, but each matches orders against its own copy, so one session's orders never fill against or move another's.

A page should name its own session on every later call:

| Call | How to name the session |
| --- | --- |
| `GET /v1/session` | `?sessionId=sess_7` |
| `POST /v1/stop` | Body `{ "sessionId": "sess_7" }` |
| `ws://…/v1/watch` | `?sessionId=sess_7` |

Without a `sessionId`, these calls use the most recently created session, which may belong to someone else.

- An unknown or expired id returns `404 unknown_session`. The watch socket sends that error and closes.
- A halted session stays readable for 10 minutes, then it is dropped.
- When 20 sessions are already running, `POST /v1/session` returns `409 too_many_sessions`.
- `GET /v1/sessions` lists every session with `sessionId`, `status`, `strategy`, `instrument`, and `startedAt`.
- `GET /v1/health` reports `sessions: { running, max }`.

## Instruments

| Symbol | Data the book follows | Fills |
| --- | --- | --- |
| `BTC-USDT` | Local replay of `hft/data/raw/BTCUSDT_20261001_*.csv.gz` | Fake, on localhost |
| `SOL-USD` | Live mid, read-only, from Binance, then Jupiter, then Coinbase | Fake, on localhost |

`GET /v1/instruments` is the source of truth for tick size, lot size, and which symbols exist. The page should render the picker from that response rather than hard-coding the table.

## Shared types

### BookLevel

```json
{ "price": 10400, "size": 500 }
```

| Field | Type | Meaning |
| --- | --- | --- |
| `price` | integer | Cents per one base coin. |
| `size` | integer | Lots resting at that price. |

### OpenOrder

Shown for context. The page cannot cancel these.

| Field | Type | Meaning |
| --- | --- | --- |
| `clientOrderId` | string | Id the model sent. |
| `orderId` | string | Server id, stable for the life of the order. |
| `side` | `"buy"` \| `"sell"` | |
| `price` | integer \| null | `null` for a market order still waiting out latency. |
| `size` | integer | Original lots. |
| `remaining` | integer | Lots still working. |
| `status` | `"pending"` \| `"open"` | `pending` is inside the 50ms delay. `open` is resting. |
| `type` | `"limit"` \| `"market"` \| `"post_only"` \| `"ioc"` | |

### Fill

A fill of the pool. The stake moves because of these, plus marks on an open position.

| Field | Type | Meaning |
| --- | --- | --- |
| `clientOrderId` | string | |
| `orderId` | string | |
| `side` | `"buy"` \| `"sell"` | Side of the pool's order. |
| `price` | integer | Cents per base coin. |
| `size` | integer | Lots filled. |
| `fee` | integer | Fee in cents, already removed from pool cash. |
| `liquidity` | `"maker"` \| `"taker"` | |
| `ts` | string | When the fake fill was booked. |

### Halt

`null` while the session is running.

```json
{
  "reason": "ceiling",
  "stakeCents": 800,
  "poolPnlCents": 300,
  "at": "2026-10-17T21:04:11.120Z"
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `reason` | `"floor"` \| `"ceiling"` \| `"manual"` | Which rail fired. |
| `stakeCents` | integer | Frozen stake after the flatten. |
| `poolPnlCents` | integer | Frozen pool PnL after the flatten. |
| `at` | string | When the halt was booked. |

### Session

Returned by `POST /v1/session`, `POST /v1/stop`, `GET /v1/session`, and every watch snapshot.

| Field | Type | Meaning |
| --- | --- | --- |
| `sessionId` | string | New id for each successful create. |
| `status` | `"running"` \| `"halted"` | |
| `halt` | Halt \| null | |
| `instrument` | string | Symbol from `GET /v1/instruments`. |
| `strategy` | `"fast"` \| `"safe"` | Which bot trades the session. `fast` is the rule-based high-frequency bot. `safe` is the trained model. |
| `depositCents` | integer | What the user put in. |
| `floorCents` | integer | |
| `ceilingCents` | integer | |
| `stakeCents` | integer | The number the page plots. |
| `poolPnlCents` | integer | `stakeCents - depositCents`. |
| `houseFundCents` | integer | Always `10000000` in this build. |
| `poolEquityCents` | integer | Cash plus marked position for the whole pool. |
| `positionLots` | integer | Signed. Positive is long the base coin. |
| `freeCashCents` | integer | Dummy cash not reserved by the open position's cost. |
| `realizedPnlCents` | integer | Closed-trade PnL minus fees. |
| `unrealizedPnlCents` | integer | Open position marked at `midCents`. |
| `midCents` | integer | Mark price. |
| `priceSource` | `"tape"` \| `"binance"` \| `"jupiter"` \| `"coinbase"` \| `"stale"` | Where `midCents` came from. `tape` is the local BTC file. |
| `feed` | `"ok"` \| `"stale"` | `stale` means every live source failed and the last mid is being held. Replay sessions stay `"ok"`. |
| `bids` | BookLevel[] | Up to 10 prices, best first. |
| `asks` | BookLevel[] | Up to 10 prices, best first. |
| `openOrders` | OpenOrder[] | Working orders only. |
| `fills` | Fill[] | Newest first, at most 50. |
| `stakeHistory` | `{ "ts": string, "stakeCents": integer }[]` | Newest last, at most 180 points. Kept so a refresh can redraw the curve. |
| `startedAt` | string | |

### Error

HTTP errors use this body. Watch-socket errors use the same `error` object inside a message.

```json
{
  "error": {
    "code": "session_running",
    "message": "A session is already running. Stop it before starting another."
  }
}
```

| HTTP | `error.code` | When |
| --- | --- | --- |
| 400 | `invalid_json` | Body is not a JSON object. |
| 400 | `unknown_field` | A field outside the documented set was sent. |
| 400 | `invalid_type` | A field has the wrong JSON type. |
| 400 | `deposit_out_of_range` | Deposit is missing or not an integer from `100` to `10000`. |
| 400 | `unknown_instrument` | Symbol is not in `GET /v1/instruments`. |
| 400 | `bad_symbol` | `GET /v1/history` symbol is missing or not a coin ticker. |
| 400 | `bad_interval` | `GET /v1/history` interval is not one of the documented values. |
| 400 | `bad_limit` | `GET /v1/history` limit is not an integer from `1` to `500`. |
| 404 | `unknown_symbol` | `GET /v1/history` coin has no USD market on Binance, Coinbase, or Kraken. |
| 400 | `floor_out_of_range` | Floor is not an integer, is below `0`, or is greater than or equal to the deposit. |
| 400 | `ceiling_required` | Ceiling was omitted. |
| 400 | `ceiling_not_above_deposit` | Ceiling is not an integer strictly above the deposit. |
| 404 | `no_session` | `GET /v1/session` or `POST /v1/stop` while idle. |
| 502 | `history_unavailable` | The public candle source did not answer `GET /v1/history`. |
| 409 | `session_running` | `POST /v1/session` while a session is still `running`. |

A halted session does not block `POST /v1/session`. Creating a session replaces the halted one.

## Endpoints

### `GET /v1/health`

Liveness for the page's connection badge.

**Parameters.** None.

**Response 200**

```json
{
  "ok": true,
  "mode": "localhost",
  "execution": "solana",
  "chain": { "rpc": "http://127.0.0.1:8899", "ready": true, "pool": "<pubkey>" }
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `ok` | boolean | Process is serving. |
| `mode` | `"localhost"` | Always this value. |
| `execution` | `"solana"` or `"fake"` | `"solana"` when this process settles live fills on the local validator. `"fake"` when no chain is attached. |
| `chain` | object | Present when `execution` is `"solana"`. `ready` is true once the local validator accepts requests. |

**Use cases**

- On load, before opening the watch socket, confirm the server is up.
- Show a disconnected state when this call fails. Do not treat a failed health check as a halted session.

### `GET /v1/instruments`

The instrument picker and the legal ranges for the session form.

**Parameters.** None.

**Response 200**

```json
{
  "houseFundCents": 10000000,
  "deposit": { "minCents": 100, "maxCents": 10000 },
  "fees": { "makerBps": 2, "takerBps": 5 },
  "instruments": [
    {
      "symbol": "BTC-USDT",
      "base": "BTC",
      "quote": "USD",
      "priceTickCents": 10,
      "lotSize": "0.001",
      "data": "replay",
      "tape": "BTCUSDT_20261001"
    },
    {
      "symbol": "SOL-USD",
      "base": "SOL",
      "quote": "USD",
      "priceTickCents": 1,
      "lotSize": "0.001",
      "data": "live",
      "sources": ["binance", "jupiter", "coinbase"]
    }
  ]
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `houseFundCents` | integer | Dummy fund the deposit is pooled with. |
| `deposit.minCents` / `deposit.maxCents` | integer | Inclusive deposit bounds. |
| `fees.makerBps` / `fees.takerBps` | integer | Fee rates, in basis points, applied to fake fills. |
| `instruments[].symbol` | string | Value sent as `instrument`. |
| `instruments[].base` | string | Coin the position is in. |
| `instruments[].quote` | string | Always `USD` for the stake. |
| `instruments[].priceTickCents` | integer | Prices must be a multiple of this. |
| `instruments[].lotSize` | string | Base-coin size of one lot. Decimal string so it is exact. |
| `instruments[].data` | `"replay"` \| `"live"` | `replay` reads the local tape. `live` polls a public mid. |
| `instruments[].tape` | string | Present when `data` is `replay`. File prefix under `hft/data/raw`. |
| `instruments[].sources` | string[] | Present when `data` is `live`. Tried in order. |

**Use cases**

- Build the currency dropdown from `instruments`. Do not offer a symbol that is absent.
- Prefill the deposit field inside `deposit.minCents` and `deposit.maxCents`.
- Label `BTC-USDT` as recorded data and `SOL-USD` as a live mid with fake fills.
- Show the `$100,000` fund next to the deposit so the pooling is visible before start.

### `GET /v1/session`

Current session, or an idle body when the user has not started one.

**Parameters.** None.

**Response 200, idle**

```json
{ "status": "idle" }
```

**Response 200, running or halted**

A [Session](#session) object.

**Use cases**

- On refresh, decide whether to show the setup form (`idle`) or the live view (`running` or `halted`).
- After a reconnect, restore the curve from `stakeHistory` instead of starting it from zero.
- A `halted` body is how the page learns the reason if it missed the watch message. Leave the frozen stake on screen and offer a new session.

### `POST /v1/session`

Create the one live session. This is the only way a stake starts. The model cannot call this successfully as a substitute for the page: if a session is already running, it gets `409`, and the page is what collects deposit, instrument, floor, and ceiling.

**Body**

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `depositCents` | integer | yes | Dummy deposit, `100`–`10000`. |
| `instrument` | string | no | A `symbol` from `GET /v1/instruments`. Default `"BTC-USDT"`. |
| `strategy` | string | no | `"fast"` or `"safe"`. Default `"safe"`. Any other value is `400 bad_strategy`. |
| `floorCents` | integer | no | Default `0`. Must satisfy `0 <= floorCents < depositCents`. |
| `ceilingCents` | integer | yes | Must be an integer strictly greater than `depositCents`. |

Unknown fields are `400 unknown_field`. Numbers must be JSON integers, not strings and not floats.

**Response 201**

A [Session](#session) with `status: "running"`, `halt: null`, `stakeCents` equal to `depositCents`, and `poolPnlCents` equal to `0`. `sessionId` is new.

**Errors.** The table in [Error](#error). Replacing a halted session returns `201`, not `409`.

**Use cases**

- Start with the default floor. Body omits `floorCents`. Stake starts at the deposit and the loss rail is `$0`.
- Start with a tighter floor. Deposit `500`, floor `250`, ceiling `800`. The session stops if the stake falls to `$2.50` or rises to `$8.00`.
- Reject a floor equal to the deposit (`floor_out_of_range`). That session could never trade, because the stake starts on the floor.
- Reject a ceiling equal to the deposit (`ceiling_not_above_deposit`).
- Reject a second start while the first is still running (`session_running`). The page disables the form until Stop, or until a halt.
- After a halt, submit the form again. The new `sessionId` replaces the old session. Stake history starts over.
- Pick `BTC-USDT`. The book follows the local tape. `priceSource` is `tape`.
- Pick `SOL-USD`. The book is quoted around a public mid. `priceSource` is whichever source answered. Fills stay fake either way.
- Deposit `$0.50` (`50`) or `$150` (`15000`). Both are `deposit_out_of_range`.

**Example**

```http
POST /v1/session
Content-Type: application/json

{
  "depositCents": 500,
  "instrument": "BTC-USDT",
  "ceilingCents": 800
}
```

Floor defaults to `0`.

### `POST /v1/stop`

Stop the running session. The server cancels working orders, flattens the position, freezes the stake, and sets `halt.reason` to `manual`.

**Parameters.** None. No body.

**Response 200**

A [Session](#session) with `status: "halted"` and `halt.reason` of `manual`.

**Response 200, already halted**

The same halted session, unchanged. A second Stop does not rewrite `halt.reason`. A floor halt stays a floor halt.

**Response 404**

`no_session` when status is `idle`.

**Use cases**

- The user presses Stop while watching. The next watch snapshot is the halted session, and a `halt` message arrives immediately so the page does not wait for the next tick.
- The user presses Stop after the stake already hit the ceiling. The response is the existing halt. The page keeps the reason `ceiling`.
- The user presses Stop on the setup screen before a session exists. Show `no_session` and leave the form up.

### `WS /v1/watch`

Read-only stream of the session the page is showing.

**Query parameters.** None.

**On connect**

The server sends one message immediately.

Idle:

```json
{ "type": "snapshot", "session": { "status": "idle" } }
```

Running or halted: a snapshot whose `session` is a [Session](#session).

**Server messages**

| `type` | When | Fields |
| --- | --- | --- |
| `snapshot` | On connect, then about every `200ms` while the socket is open | `session` |
| `halt` | The moment a floor, ceiling, or manual stop is booked | `session` |
| `pong` | Reply to `ping` | none |

`halt` is also a full session, so the page can render it without waiting for the next snapshot. Later snapshots repeat the halted session and do not move `stakeCents`.

While `running`, each snapshot may change `stakeCents`, `midCents`, `bids`, `asks`, `fills`, `openOrders`, and `stakeHistory`. `stakeHistory` is the full retained window, not a delta. Replace the curve from it.

**Client messages**

The only accepted client message is:

```json
{ "op": "ping" }
```

Anything else, including `place`, `cancel`, `cancel_all`, `flatten`, or `stop`, gets:

```json
{
  "type": "error",
  "error": {
    "code": "watch_is_read_only",
    "message": "The watch socket cannot trade or stop a session. Use POST /v1/stop to stop."
  }
}
```

The socket stays open. Stop is `POST /v1/stop`, not a watch message.

**Use cases**

- Subscribe after `POST /v1/session` and plot `stakeCents` against `floorCents` and `ceilingCents`.
- Refresh mid-session. The first snapshot's `stakeHistory` redraws the curve, and `fills` reprints the tape.
- Show the book from `bids` and `asks`, best first. Empty arrays mean the book has not printed yet.
- Show working orders from `openOrders`. `pending` means the model's order is inside the latency delay and is not on the book yet.
- Append nothing locally for fills. Replace the tape from `session.fills` on each snapshot so a dropped message cannot duplicate a row.
- When `feed` is `stale`, show that the mid is held and that fills are still fake. Do not treat `stale` as a halt.
- When a `halt` message arrives, freeze the chart, show `halt.reason`, and re-enable the setup form.
- Send `ping` every few seconds. If `pong` does not return, close and reconnect. Reconnect always starts with a fresh snapshot.
- A model order sent on this socket by mistake is rejected with `watch_is_read_only` and does not change the stake.

**Dropped sockets**

The server does not wait on a slow watch client. If the socket's send buffer backs up, the server closes it. The page reconnects and takes the next snapshot as the truth.

### `GET /v1/history`

Candles for a coin the page wants to chart. This is not the session book and it does not start a session. Any coin with a USD market on Binance, Coinbase, or Kraken is accepted, including coins that are not in `GET /v1/instruments`. A session can still trade only a symbol from that list.

`SOL`, `SOL-USD`, and `SOLUSDT` are the same coin. `BTC-USDT` is `BTC`. The response `symbol` is the base ticker.

**Parameters**

| Name | Required | Default | Meaning |
| --- | --- | --- | --- |
| `symbol` | yes | | Coin ticker, or an instrument symbol such as `SOL-USD`. |
| `interval` | no | `1h` | One of `1m`, `5m`, `15m`, `1h`, `4h`, `1d`. |
| `limit` | no | `100` | Number of candles, from `1` to `500`. Oldest first. |

**Response 200**

```json
{
  "symbol": "ETH",
  "market": "ETH-USD",
  "interval": "1h",
  "source": "coinbase",
  "candles": [
    {
      "openTime": "2026-10-17T15:00:00.000Z",
      "openCents": 350012,
      "highCents": 351000,
      "lowCents": 349500,
      "closeCents": 350550,
      "open": "3500.12",
      "high": "3510.00",
      "low": "3495.00",
      "close": "3505.50",
      "volume": "12.5"
    }
  ]
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `symbol` | string | Base coin, without `-USD`. |
| `market` | string | `{symbol}-USD`. A label for the chart, not a session instrument. |
| `interval` | string | The interval that was used. |
| `source` | `"binance"` \| `"coinbase"` \| `"kraken"` | Whichever public candle source answered. Tried in that order. The order is not sent there. |
| `candles` | array | Oldest first. |
| `candles[].openTime` | string | Candle open, ISO-8601. |
| `candles[].openCents` / `highCents` / `lowCents` / `closeCents` | integer | `round(usd * 100)`. A coin under one cent can be `0`. |
| `candles[].open` / `high` / `low` / `close` | string | USD price from the exchange, decimal text. Plot these when cents would round away the price. |
| `candles[].volume` | string | Base-coin volume, decimal text. |

**Errors.** `bad_symbol`, `bad_interval`, `bad_limit`, `unknown_symbol`, `history_unavailable`. See [Error](#error).

**Use cases**

- The user changes the coin picker. Request `GET /v1/history?symbol=ETH&interval=1h` and draw `close` against `openTime` before they start a session.
- Pass the instrument symbol directly: `GET /v1/history?symbol=SOL-USD`.
- A coin none of those sources lists returns `404` `unknown_symbol`. Leave the chart empty.
- `502` `history_unavailable` means the candle source failed. The session, stake, and book are unchanged.

## What this API will not do

- Place or cancel orders. Use the model API.
- Change floor, ceiling, deposit, or instrument on an open session.
- Return real exchange order ids, wallet addresses, transaction signatures, or live balances.
- Stream a second user's stake. One localhost session is the whole product.
