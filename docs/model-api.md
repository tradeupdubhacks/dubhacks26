# Model API

An external model or trainer uses this API to read the book and send orders. This repo does not ship a model. The live page cannot call these order endpoints; it uses the [client API](client-api.md) to open a session, watch the stake, and stop.

Base URL: `http://localhost:3000`

WebSocket: `ws://localhost:3000`

All JSON bodies use `Content-Type: application/json`. There is no authentication. Accounts, cash, and fills are dummy ledger entries on localhost. Market data may be read from a local tape or from a public mid. The order itself is never forwarded to Binance, Jupiter, or Coinbase. Training fills stay in memory. A live session fill is an SPL transfer on the local validator.

Two ways to run, and they do not overlap:

| | Training | Live session |
| --- | --- | --- |
| Who opens it | The model, with `POST /v1/reset` | The page, with `POST /v1/session` |
| How time moves | Only when the model calls `POST /v1/step` | A `200ms` wall-clock tick, or `step` on a `BTC-USDT` replay |
| Orders | Inside `step`, or on `/v1/stream` and the REST order routes | `/v1/stream` and the REST order routes |
| User stake | Absent. `GET /v1/session` is `idle` | Present. Floor, ceiling, and manual stop can halt trading |
| `POST /v1/reset` | Starts or restarts an episode | `409 session_open` |

`POST /v1/reset` while a live session exists is refused so a trainer cannot rewind a person's stake. Stop the session from the page first, or train when the server is idle.

## Units

| Field | Meaning |
| --- | --- |
| `*Cents` | Dummy USD cents. `100000` is the `$1000` house fund. |
| `price` | Integer USD cents for **one** whole base coin. Must be a multiple of the instrument's `priceTickCents`. |
| `size` | Integer lots. One lot is `0.001` base coin. |
| `ts` on events | Market time in milliseconds. In training this is the episode clock, starting at `0` on reset. In a live session this is the session clock, starting at `0` when the session is created. It is not a wall-clock timestamp. |
| `reward` | Integer cents. Change in pool equity across a `step`, fees already included. |

`BTC-USDT` tick is `10` cents. `SOL-USD` tick is `1` cent. Notional cents of a fill is `round(price * size / 1000)`.

Fees on every instrument: `2` bps maker, `5` bps taker, rounded down to a cent on the fill notional.

Latency: an accepted order waits `50ms` of **market** time before it can match. A `step` with `dtMs: 10` leaves it `pending`. A later step that carries the clock past `50ms` releases it. On a live session the wall clock releases it. Flatten, halt, and a user Stop skip this delay.

## Instruments and limits

`GET /v1/instruments` returns the live list. This build's values:

| | `BTC-USDT` | `SOL-USD` |
| --- | --- | --- |
| Data in a live session | Replay `hft/data/raw/BTCUSDT_20261001_*.csv.gz` | Public mid: Binance, then Jupiter, then Coinbase |
| Data in training | The same tape, advanced only by `step` | Synthetic mid and counterparty quotes. No public HTTP calls |
| `priceTickCents` | `10` | `1` |
| Lot | `0.001` BTC | `0.001` SOL |
| `maxOrderLots` | `10` | `2000` |
| `maxPositionLots` | `10` | `10000` |
| Orders per `1000ms` of market time | `20` | `20` |

The rate limit counts accepted `place` requests, not cancels and not duplicate ids. The 21st new id inside the window is rejected with `rate_limit`. Training that sends one order per `100ms` step stays under the limit. A burst of 21 different ids in one step does not.

Position headroom includes resting lots and lots still `pending`. A buy is rejected when `positionLots + buyLotsWorking + size` would exceed `maxPositionLots`. A sell is rejected when `positionLots - sellLotsWorking - size` would fall below `-maxPositionLots`.

## Shared types

### Order

The body of a place, whether it arrives by REST, WebSocket, or `step`.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `clientOrderId` | string | yes | `1`–`64` characters from `[A-Za-z0-9._:-]`. Idempotency key for the episode or the live session. |
| `side` | `"buy"` \| `"sell"` | yes | |
| `size` | integer | yes | Positive lots, at most `maxOrderLots`. |
| `type` | `"limit"` \| `"market"` \| `"post_only"` \| `"ioc"` | yes | See below. |
| `price` | integer \| null | depends | Required for `limit`, `post_only`, and `ioc`. Omit or `null` for `market`. |
| `timeInForce` | `"gtc"` \| `"ioc"` | no | Default `gtc` for `limit`. Ignored for the other types: `market` and `ioc` do not rest, `post_only` rests or rejects. |

| `type` | Price | What happens at release |
| --- | --- | --- |
| `limit` | required, multiple of the tick, `>= 1` | Matches what it can, rests the remainder unless `timeInForce` is `ioc`. |
| `market` | omitted or `null` | Takes the book with no price cap. Unfilled size is canceled. Never rests. |
| `post_only` | required | Rejects with `post_only_would_take` if it would trade immediately. Otherwise rests. |
| `ioc` | required | Matches what it can up to `price`. Remainder is canceled. Never rests. |

### Ack

```json
{
  "type": "ack",
  "clientOrderId": "b1",
  "orderId": "o17",
  "status": "pending",
  "duplicate": false
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `clientOrderId` | string | Echo of the request. |
| `orderId` | string | Server id. Empty string `""` when the cached result is a reject that never created an order. Present on every ack. |
| `status` | `"pending"` \| `"open"` \| `"filled"` \| `"canceled"` \| `"rejected"` | |
| `duplicate` | boolean | `true` when this is the stored result for an id the server has already seen. |

Status meaning:

| Status | Meaning |
| --- | --- |
| `pending` | Accepted, waiting out the `50ms` delay. |
| `open` | Resting. A partial fill can still have this status; the fill events carry the traded lots. |
| `filled` | Nothing left. |
| `canceled` | Gone, and not replaced. Also used when an IOC or market order finishes with unfilled size, including when some lots did trade. |
| `rejected` | Not working. The paired event is a `reject`, not an `ack`. |

### Reject

```json
{
  "type": "reject",
  "clientOrderId": "b1",
  "reason": "rate_limit",
  "duplicate": false
}
```

| `reason` | When |
| --- | --- |
| `session_halted` | Live session is halted. No new orders until the page starts a new session. |
| `session_required` | Order routes were called while idle. Call `POST /v1/reset` for training, or wait until the page has created a session. |
| `bad_client_order_id` | Missing, empty, too long, or illegal characters. |
| `bad_side` | Not `buy` or `sell`. |
| `bad_type` | Not one of the four types. |
| `bad_size` | Not a positive integer, or above `maxOrderLots`. |
| `price_required` | `limit`, `post_only`, or `ioc` without a positive integer price. |
| `price_forbidden` | `market` sent with a non-null price. |
| `bad_tick` | Price is not a multiple of `priceTickCents`. |
| `position_limit` | The order would push the signed position past `maxPositionLots`, counting resting and pending lots. |
| `rate_limit` | Too many new ids in the last `1000ms` of market time. |
| `post_only_would_take` | At release, the post-only order would have taken liquidity. |
| `unknown_order` | Cancel of an id that is not pending or open. |
| `invalid_message` | WebSocket payload was not a documented command. |

A reject is cached under `clientOrderId` when one was present. Sending that id again returns the same reject with `duplicate: true` and does not consume rate-limit budget. Risk may have changed; a new attempt still needs a new id.

### Fill

```json
{
  "type": "fill",
  "clientOrderId": "b1",
  "orderId": "o17",
  "side": "buy",
  "price": 10400,
  "size": 10,
  "fee": 1,
  "liquidity": "taker",
  "ts": 50
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `size` | integer | Lots in this fill, not the order's original size. |
| `fee` | integer | Cents charged on this fill. |
| `liquidity` | `"maker"` \| `"taker"` | The pool's role. |
| `ts` | integer | Market time in milliseconds. |

One order can emit several fills as it walks the book. Self-trade does not emit a fill. If the pool's own resting order is at the front of the level, the server cancels that resting order and continues to the next order. The aggressor never trades with itself.

### Trade

Public print, including prints that did not involve a new model order (tape prints on BTC, counterparty prints on synthetic SOL).

```json
{ "type": "trade", "price": 10400, "size": 10, "side": "buy", "ts": 50 }
```

`side` is the taker side.

### Observation

The book and the pool account. Returned by `GET /v1/account`, `POST /v1/reset`, `POST /v1/step`, and the stream.

| Field | Type | Meaning |
| --- | --- | --- |
| `market` | string | Instrument symbol. |
| `clock` | `"step"` \| `"wall"` | `step` during training. `wall` during a live session. |
| `ts` | integer | Market time in milliseconds. |
| `mid` | integer | Mark, in cents per base coin. |
| `priceSource` | `"tape"` \| `"synthetic"` \| `"binance"` \| `"jupiter"` \| `"coinbase"` \| `"stale"` | |
| `bids` | `{ price, size }[]` | Up to 10, best (highest) first. |
| `asks` | `{ price, size }[]` | Up to 10, best (lowest) first. |
| `lastTrade` | Trade without `type`, or `null` | Most recent print. |
| `mids` | integer[] | Oldest first, at most 32 mids, including the current one. |
| `positionLots` | integer | Signed pool position. |
| `freeCashCents` | integer | Dummy cash. |
| `equityCents` | integer | Cash plus position marked at `mid`. This is the pool, not the user's stake. |
| `realizedPnlCents` | integer | |
| `unrealizedPnlCents` | integer | |
| `openOrders` | OpenOrder[] | `pending` and `open` only. Same fields as the client spec's open order. |
| `halted` | boolean | `true` only when a live session is halted. Training is never halted by floor or ceiling. |
| `haltReason` | `"floor"` \| `"ceiling"` \| `"manual"` \| `null` | |
| `stakeCents` | integer \| `null` | User stake when a live session exists. `null` in training. |
| `floorCents` | integer \| `null` | |
| `ceilingCents` | integer \| `null` | |
| `step` | integer | Steps since reset. `0` in a live session that has not been stepped. |
| `done` | boolean | Training episode finished. Always `false` in a live session. |

### HTTP error

```json
{
  "error": {
    "code": "session_open",
    "message": "Reset is refused while a live session exists."
  }
}
```

| HTTP | `error.code` | When |
| --- | --- | --- |
| 400 | `invalid_json` | Body is not a JSON object. |
| 400 | `unknown_field` | Undocumented field. |
| 400 | `invalid_type` | Wrong JSON type, including a numeric string. |
| 400 | `bad_order` | An order in the body failed shape checks. `error.clientOrderId` and `error.reason` identify which. `reason` is one of the reject reasons. |
| 400 | `bad_dt` | `dtMs` is not an integer from `1` to `60000`. |
| 400 | `bad_seed` | `seed` is not an integer from `0` to `2147483647`. |
| 400 | `bad_episode_steps` | `episodeSteps` is not an integer from `1` to `1000000`. |
| 400 | `bad_cancels` | `cancels` is not an array of client order id strings. |
| 404 | `unknown_order` | REST cancel of an id that is not pending or open. |
| 409 | `session_open` | `POST /v1/reset` while a live session exists. |
| 409 | `session_required` | REST place, cancel, cancel-all, or flatten while idle and no training episode is active. |
| 409 | `session_halted` | Those same routes after a live halt. |
| 409 | `step_not_for_live_mid` | `POST /v1/step` during a live `SOL-USD` session. Time there is the wall clock. |

Shape failures on a single REST place return `400` with `error.reason` set to the reject reason and `error.clientOrderId` set. The id is cached the same way as a stream reject.

## Endpoints

### `GET /v1/health`

Same body as the client API.

```json
{ "ok": true, "mode": "localhost", "execution": "solana" }
```

`npm start` reports `"solana"`. A process built without a chain, including `npm test`, reports `"fake"`. Training orders stay in memory either way. Live session fills are SPL transfers only when `execution` is `"solana"`.

**Use cases**

- Fail a trainer fast when the server is down.
- Confirm `execution` is `fake` or `solana` before sending orders. Any other value means the fills are not this gateway.

### `GET /v1/instruments`

Same body as the client API. Use `priceTickCents`, and do not assume a symbol exists because it existed in an older build.

**Use cases**

- Read tick size before rounding a limit price.
- Branch training on `data`. `replay` means `step` walks the BTC tape. `live` means a training episode for that symbol is synthetic, while a live session for that symbol follows a public mid.

### `GET /v1/schema`

JSON Schema draft 2020-12 for the model-facing bodies. The page does not need this.

**Parameters.** None.

**Response 200**

```json
{
  "order": {},
  "stepRequest": {},
  "stepResponse": {},
  "observation": {},
  "clientMessage": {},
  "serverMessage": {}
}
```

Each value is a schema object. `clientMessage` is the `/v1/stream` command. `serverMessage` is one stream message. Schemas use `additionalProperties: false`.

**Use cases**

- Generate a client or validate outbound orders in the model process.
- Detect a server upgrade by diffing the schema. Unknown fields are rejected, so a model built against an old schema fails closed.

### `GET /v1/book`

A snapshot of the book only.

**Parameters.** None.

**Response 200**

```json
{
  "market": "BTC-USDT",
  "ts": 1200,
  "mid": 6000000,
  "priceSource": "tape",
  "bids": [{ "price": 5999990, "size": 40 }],
  "asks": [{ "price": 6000010, "size": 25 }],
  "lastTrade": { "price": 6000000, "size": 1, "side": "buy", "ts": 1100 }
}
```

`lastTrade` is `null` before the first print.

**Use cases**

- Poll once before subscribing, to seed a strategy that does not want the account block.
- Reconcile a stream gap. The book route is the current book, not a delta.

### `GET /v1/account`

The full [Observation](#observation).

**Parameters.** None.

**Use cases**

- On process start, see whether the server is idle (`halted: false`, `stakeCents: null`, `step: 0`, `ts: 0`) or already inside a live session.
- Read `floorCents` and `ceilingCents` so a live strategy can stop quoting before the server halts it.
- After a reconnect, replace local open orders with `openOrders`. Do not merge.

### `POST /v1/reset`

Start a training episode. Refused while a live session exists.

**Body.** All fields optional. `{}` is legal.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `instrument` | string | `"BTC-USDT"` | Must be a known symbol. |
| `seed` | integer | `1` | `0`–`2147483647`. Drives the synthetic SOL mid and counterparties. Ignored for `BTC-USDT`, because the tape is the randomness. |
| `episodeSteps` | integer | `1000` | `done` becomes `true` when `step` reaches this count. |

**Response 200**

```json
{
  "observation": {},
  "done": false
}
```

`observation.step` is `0`, `observation.ts` is `0`, `observation.clock` is `"step"`, position is `0`, and equity is the `$1000` house fund (`100000` cents) with no user deposit. The idempotency cache and open orders are cleared.

**Response 409.** `session_open`.

**Use cases**

- Start a BTC episode with defaults. The first observation is the book at the start of `BTCUSDT_20261001`. `priceSource` is `tape`. `seed` does not change that book.
- Start a SOL training episode with `instrument: "SOL-USD"` and a `seed`. `priceSource` is `synthetic`. Two counterparties quote around the mid: a market maker and a noise taker. No Jupiter or Binance call is made.
- Call reset again mid-episode. The clock returns to `0`, PnL returns to `0`, and order ids from the previous episode are no longer idempotent.
- Call reset while the page has a session. Handle `409` and do not retry in a loop. Wait until `GET /v1/session` reports `idle`.

### `POST /v1/step`

Advance training time, or advance a live `BTC-USDT` replay, and apply orders at the new clock.

**Body.** All fields optional. `{}` advances time and submits nothing.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `orders` | Order[] | `[]` | Enqueued at the clock **before** time advances, then released if the advance covers their `50ms` delay. |
| `cancels` | string[] | `[]` | Client order ids. A cancel is also delayed `50ms`, except that a halt flatten is not a step cancel. |
| `dtMs` | integer | `100` | Market time to add. Range `1`–`60000`. |

Processing order inside one call:

1. Reject the whole request with `400` if any order fails a shape check. No time passes and no earlier order in the array is kept.
2. Cache idempotent duplicates and append new orders and cancels to the delay queue.
3. Advance the clock by `dtMs`. On BTC, play that much of the tape. On synthetic SOL, move the mid and refresh counterparty quotes.
4. Release queued commands whose delay has elapsed, in enqueue order.
5. Compute `reward` from pool equity.

**Response 200**

```json
{
  "observation": {},
  "reward": 0,
  "done": false,
  "events": []
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `observation` | Observation | State after the advance. |
| `reward` | integer | `equityCents` after minus `equityCents` before, in cents. Fees are already in equity. Do not subtract them again. |
| `done` | boolean | `true` once `observation.step` has reached `episodeSteps`. Further `step` calls still run, and `done` stays `true` until reset. |
| `events` | array | Acks, rejects, fills, and public trades produced by this call, in order. |

`observation.step` increments by `1` on every successful call, regardless of `dtMs`.

**Response 409**

- `step_not_for_live_mid` during a live `SOL-USD` session.
- `session_halted` during a halted live session.

A live `BTC-USDT` session accepts `step`. That is how a demo can move the tape faster than the wall clock while the page is watching. `reward` is still the pool equity change. The page's stake moves by the same amount because stake equals deposit plus pool PnL.

**Use cases**

- Warm up the tape. Call `step` with `{}` until `bids` and `asks` are non-empty, then start quoting.
- Send a market buy and give it time to leave the delay queue:

  ```json
  {
    "dtMs": 100,
    "orders": [
      { "clientOrderId": "m1", "side": "buy", "size": 1, "type": "market" }
    ]
  }
  ```

  `dtMs` of `100` covers the `50ms` delay, so this response can contain a `pending` ack is not what happens: the order is accepted and released inside the same call. Events include an ack of `pending` only if you need the intermediate state — the server emits the terminal events for work that finished inside the call (`filled` or `canceled`, plus `fill` events). It does not also emit a leftover `pending` for an order that already released.

- Prove latency. First call `dtMs: 10` with a market buy. The ack status is `pending`, there is no fill, and `positionLots` is unchanged. Second call `dtMs: 100` with no orders. The buy releases and fills.
- Rest a limit. Buy at a price strictly below the best ask, `type: "limit"`, `dtMs: 100`. The ack status is `open`. The order appears in `openOrders`.
- Cancel that order. Next step sends `cancels: ["that-id"]` and `dtMs: 100`. The ack status becomes `canceled`.
- Cancel too soon. `dtMs: 10` on the cancel leaves the order `open` until a later step. Cancels wait `50ms` too.
- IOC that only partly trades. `type: "ioc"` with a size larger than the displayed ask. Events contain one or more fills and a final ack of `canceled`. `openOrders` does not contain the id.
- Post-only that would cross. `type: "post_only"` with a buy price greater than or equal to the best ask, and enough `dtMs` to release it. The event is `reject` with `post_only_would_take`. Position is unchanged.
- Post-only that is accepted. Buy below the best ask. After release the status is `open`.
- Duplicate id. Repeat `clientOrderId` inside a later step, or twice in one `orders` array. The second event is the cached result with `duplicate: true`. Position does not increase twice. If the two copies are in one array, the second is duplicate immediately and is not queued.
- Reuse an id after cancel. Still a duplicate of the canceled ack. Choose a new id.
- Self-trade. Rest a sell, then send a market buy that would hit it. The sell is canceled. The buy fills against the next seller if there is one. There is no fill whose two sides are both the pool.
- Rate limit. Twenty-one new limit ids in one step, each size `1`, priced so they would rest. The first twenty ack. The twenty-first is `rate_limit`.
- Position limit. On `BTC-USDT`, `maxPositionLots` is `10`. An order that would make `11` is `position_limit` and is not queued.
- Bad tick. BTC price `6000001` is `bad_tick` because the tick is `10` cents.
- Market order with a price. `price_forbidden`, and the clock does not move because shape checks run first.
- Read `reward` while flat and before any fill. On a step that only replays tape, a flat position has `reward` of `0` even if `mid` changes.
- Read `reward` while long. A mid move changes `unrealizedPnlCents` and therefore `reward`, with no fill required.
- Episode end. After `episodeSteps` calls, `done` is `true`. Keep calling `step` only to drain the book for logging; start the next episode with `reset`.
- Live SOL session. `step` returns `409`. Use `/v1/stream` and let the wall clock run.

### `POST /v1/orders`

Place one order outside of `step`. Same shape checks, idempotency, latency, and risk as a stream `place`.

**Body.** One [Order](#order).

**Response 200**

```json
{ "events": [], "observation": {} }
```

`events` holds the immediate ack or reject. A new valid order acks `pending`. The fill arrives later on `/v1/stream`, or inside a later `step` during training. This response does not wait for the fill.

**Response 400.** Shape or risk reject. Body:

```json
{
  "error": {
    "code": "bad_order",
    "clientOrderId": "b1",
    "reason": "bad_tick",
    "message": "Price is not a multiple of the instrument tick."
  }
}
```

**Response 409.** `session_required` or `session_halted`.

**Use cases**

- From a shell, submit one order while a live session is on screen and watch the stake move after the latency delay.
- Retry a timed-out POST with the same `clientOrderId`. The second response is `duplicate: true` and does not add a second order.
- Place during training without using the `orders` array of `step`. The order stays `pending` until a `step` or, if a live clock is running, until wall time passes. During pure training the wall clock does not run, so something must call `step` to release it.

### `DELETE /v1/orders/:clientOrderId`

Queue a cancel for one order.

**Path**

| Field | Meaning |
| --- | --- |
| `clientOrderId` | The id to cancel. Must match the id grammar. A bad path id is `400 bad_order` with `reason: "bad_client_order_id"`. |

**Body.** None.

**Response 200**

`events` contains an ack. If the order is pending or open, the cancel is queued and the immediate status stays what it was; a follow-up event reports `canceled` after `50ms` of market time. If the delay has already been satisfied because this is processed after the wait, the ack in this response is `canceled`.

The practical rule: do not assume the order is gone until an ack with `status: "canceled"` or until it disappears from `openOrders`.

**Response 404.** `unknown_order` when the id is not pending or open. That `404` is not cached as an order reject.

**Response 409.** `session_required` or `session_halted`.

**Use cases**

- Pull one quote.
- Cancel an id that already filled. `404`. The fill stands.
- Cancel during the `50ms` delay. The order never rests, provided market time reaches the cancel before you need it gone. In training, pass time with `step`.

### `POST /v1/orders/cancel-all`

Queue cancels for every pending and open order of the pool.

**Body.** None.

**Response 200**

```json
{
  "events": [],
  "observation": {},
  "canceled": ["b1", "b2"]
}
```

`canceled` lists the ids that were queued. It can be empty. Completion is still subject to the `50ms` delay unless a halt happens first.

**Response 409.** `session_required` or `session_halted`.

**Use cases**

- Pull every quote before a data gap.
- Confirm an empty book of your own orders when `canceled` is `[]` and `openOrders` is empty.

### `POST /v1/flatten`

Cancel every working order and, if the position is not flat, send a market order that closes it. The market order does **not** wait `50ms`. This does not halt the session. The model may trade again on the next message. A user Stop is different: it flattens and sets `halted` to `true`.

**Body.** None.

**Response 200**

`events` contains cancel acks and, when the position was not zero, the closing fill or fills and a final ack. `observation.positionLots` is `0` when the book had enough size to close. If the book cannot absorb the close, the remainder stays as a position and the ack for the unfilled size is `canceled`. The caller should read `positionLots` and not assume success from HTTP 200 alone.

**Response 409.** `session_required` or `session_halted`. Flatten on an already halted session is `409` because the halt already flattened.

**Use cases**

- End a quote cycle flat without ending the user's session.
- Training: close the position on the last step before `done`, then read `reward`.
- Distinguish this from the page's Stop. After flatten, `halted` is still `false` and new orders are accepted.

### `WS /v1/stream`

Live commands and clock updates for one model connection. Several connections may be open. They all trade the same pool. There is no per-socket account.

**Query parameters.** None.

**On connect**

```json
{ "type": "snapshot", "observation": {} }
```

**Client commands**

| Body | Effect |
| --- | --- |
| `{ "op": "place", "order": Order }` | Same as `POST /v1/orders`. |
| `{ "op": "cancel", "clientOrderId": "b1" }` | Same as `DELETE /v1/orders/b1`. |
| `{ "op": "cancel_all" }` | Same as `POST /v1/orders/cancel-all`. |
| `{ "op": "flatten" }` | Same as `POST /v1/flatten`. |
| `{ "op": "ping" }` | Server replies `pong`. |

**Server messages**

| `type` | When | Fields |
| --- | --- | --- |
| `snapshot` | On connect, and on each `200ms` wall tick while a live session is running | `observation` |
| `events` | After a command, and when a delayed order releases | `events`, `observation` |
| `pong` | Reply to `ping` | none |
| `error` | Malformed command | `error` with `code` and `message` |

An `events` message is also sent to every other `/v1/stream` socket, so a second connection sees fills it did not originate. Snapshots are sent to every stream socket.

During pure training there is no wall tick. Nothing arrives until the model sends a command or some other client calls `step`. A trainer that only uses `step` does not need this socket.

`error.code` for a bad socket payload is `invalid_message`. The socket stays open. HTTP-style `409` reasons (`session_required`, `session_halted`) are delivered as a `reject` event inside `events`, not as a socket close, when the command was well formed. `session_required` uses `clientOrderId` of `""` for `cancel_all` and `flatten`.

**Use cases**

- Quote in a live session. On each `snapshot`, diff `openOrders` against the desired bid and ask, then `cancel` and `place`.
- Wait for the release. A `place` returns `events` with `status: "pending"` immediately. About `50ms` of market time later an `events` message carries the fill or the `open` ack. Do not mark the order filled from the first ack.
- Handle a halt. The next command's reject reason is `session_halted`, and snapshots show `halted: true` plus `haltReason`. Stop sending orders until `GET /v1/session` is `idle` and a new session exists, or until you `reset` for training.
- Watch the floor. If `stakeCents` is within one order's notional of `floorCents`, flatten from the model before the server halts you. This is optional. The server enforces the rail either way.
- Ignore the watch socket. Connecting a model to `/v1/watch` cannot place orders. Use `/v1/stream`.
- Keepalive with `ping`. A missing `pong` means reconnect, then trust the new snapshot over local state.
- Two models at once. Both hit the same position limit and the same idempotency cache. Coordinate ids or they will collide.

**Dropped sockets**

A slow stream socket is closed rather than allowed to delay matching. Reconnect and call `GET /v1/account` if the first snapshot is missed. Matching does not depend on a subscriber being connected.

## Worked training episode

```text
POST /v1/reset
{ "instrument": "BTC-USDT", "episodeSteps": 100 }

POST /v1/step
{ "dtMs": 100 }

POST /v1/step
{
  "dtMs": 100,
  "orders": [
    {
      "clientOrderId": "bid-1",
      "side": "buy",
      "size": 1,
      "type": "post_only",
      "price": 5990000
    }
  ]
}
```

The first step loads tape with no orders. The second releases the post-only buy if `100ms` covers the delay, which it does. If `5990000` is below the ask and on the `10`-cent tick, the ack is `open`. If it would take, the event is `post_only_would_take`.

`reward` on the second call is the equity change from that step only. `done` becomes `true` on the 100th successful step.

## Worked live trade

The page has already created a session. The model then:

```http
POST /v1/orders
{ "clientOrderId": "live-1", "side": "buy", "size": 1, "type": "market" }
```

The HTTP body acks `pending`. The page's watch snapshot still shows the old stake. After `50ms`, `/v1/stream` emits the fill and the next watch snapshot shows the new `stakeCents`. If that stake is at or below the floor, or at or above the ceiling, the same moment produces a halt: position flattened, `halted: true`, further places rejected with `session_halted`.
