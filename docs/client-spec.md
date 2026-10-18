# Client spec

The page contract is [client-api.md](client-api.md). This file records `GET /v1/history`, the chart route for whichever coin the user is looking at.

## `GET /v1/history`

Returns public candles for any coin that has a USD market on Binance, Coinbase, or Kraken. Those sources are tried in that order. It does not open a session, move the book, or settle a fill. A session still trades only a symbol from `GET /v1/instruments`. The chart can show a coin that is not tradable yet.

`symbol` accepts a bare ticker or an instrument symbol. These are the same request:

- `SOL`
- `SOL-USD`
- `SOLUSDT`

`BTC-USDT` is read as `BTC`. The response `symbol` is always the base ticker.

### Parameters

| Name | Required | Default | Meaning |
| --- | --- | --- | --- |
| `symbol` | yes | | Coin ticker, or an instrument symbol such as `SOL-USD`. Letters and digits only, plus an optional `-USD` or `-USDT` suffix. |
| `interval` | no | `1h` | One of `1m`, `5m`, `15m`, `1h`, `4h`, `1d`. |
| `limit` | no | `100` | How many candles to return, from `1` to `500`. Oldest first. |

```text
GET /v1/history?symbol=ETH&interval=1h&limit=100
```

### Response 200

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
| `market` | string | `{symbol}-USD`. A chart label, not proof that a session can trade it. |
| `interval` | string | The interval that was used. |
| `source` | `"binance"` \| `"coinbase"` \| `"kraken"` | Whichever public candle source answered. Tried in that order. A regional block on Binance falls through to the next source. Orders are not forwarded there. |
| `candles` | array | Oldest first. Empty when the market exists and the window has no prints. |
| `candles[].openTime` | string | Candle open time, ISO-8601. |
| `candles[].openCents` / `highCents` / `lowCents` / `closeCents` | integer | `round(usd * 100)`, same unit as `midCents`. A coin priced under one cent can be `0`. |
| `candles[].open` / `high` / `low` / `close` | string | USD price as decimal text from the exchange. Plot these for coins where cents round to `0`. |
| `candles[].volume` | string | Base-coin volume, decimal text. |

### Errors

```json
{
  "error": {
    "code": "unknown_symbol",
    "message": "No USDT market was found for that coin."
  }
}
```

| HTTP | `error.code` | When |
| --- | --- | --- |
| 400 | `bad_symbol` | `symbol` is missing, or it is not a ticker. Slashes, spaces, and paths are rejected. |
| 400 | `bad_interval` | `interval` is not one of `1m`, `5m`, `15m`, `1h`, `4h`, `1d`. |
| 400 | `bad_limit` | `limit` is not an integer from `1` to `500`. |
| 404 | `unknown_symbol` | None of Binance, Coinbase, or Kraken has a USD market for that coin. |
| 502 | `history_unavailable` | The candle source timed out or returned an unexpected body. The session is unchanged. |

### Use cases

- The coin picker changes to ETH. `GET /v1/history?symbol=ETH&interval=1h` redraws the chart before `POST /v1/session`.
- The picker value is already an instrument symbol. `GET /v1/history?symbol=SOL-USD` is valid.
- `404` clears the chart. `502` leaves the last successful candles on screen.
- Do not treat `closeCents` as the session `midCents`. The book mid is the live or replay quote used for trading. These candles are the chart only.
