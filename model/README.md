# Paper trading bot

This process places paper orders on the model API in [docs/model-api.md](../docs/model-api.md). It does not open a user session and it does not edit the page. Fills are the server's fake matches. `BTC-USDT` follows that server's recording. `SOL-USD` follows that server's public mid. Nothing here is a low-latency exchange connection.

Start the API server on `http://localhost:3000` first. `GET /v1/health` must report `execution: "fake"` or `"solana"`. `npm start` reports `"solana"` because live fills settle on the local validator. The bot exits for any other value.

```text
pip install -r model/requirements.txt
python -m model.train
python -m model.live
```

`python -m model.train` fits on the local `BTCUSDT_20261001` book ticker plus Binance aggTrades for September 2026 (downloaded into `hft/data/extra` when missing). The evening of 2026-10-01, from the timestamp in `docs/model-issues.md`, is not used to fit weights or to pick the entry rule. The log says whether CUDA is working and, when it is, the GPU name. Live scoring loads the weights on the CPU.

The network predicts a forward return. The bot sends a taker order only when that return clears the entry level chosen on the afternoon slice, then holds for the forecast horizon. It does not rest on both sides of the touch. A `$5` stake can take a BTC lot: the rail check is a 50 bp adverse move plus the flatten fee, not the full notional of the coin. The held-out report is `model/artifacts/eval_report.json`. It is one recorded day, not a claim about live trading. The join-the-touch figure, when present, comes from `hft/run_backtest.py` and is a different simulator.

The page owns the time limit and calls `POST /v1/stop`. The bot reads the floor and ceiling from the observation and stops quoting when the session is halted or the feed is `stale`.

A `BTC-USDT` lot is `0.001` BTC, about `$83` of notional. The bot will not add a lot when a 50 bp move plus the flatten fee would cross the floor or the ceiling. A `$5` deposit clears that check. The order is still one or more full lots, so a `$5` account can lose the whole deposit on a much smaller price move than the notional.

The live loop tracks its own in-flight places and cancels from the `events` replies, decides at most every `100ms`, and uses a new `clientOrderId` prefix on every connection, because the server remembers every id it has seen. With no session open it pings every `5s` and prints `waiting for a live session` instead of reconnecting.

## Choosing the bot

`BOT_STRATEGY` picks which bot `python -m model.live` runs. Leaving it unset is the same as `model`.

| `BOT_STRATEGY` | Bot |
| --- | --- |
| `model` (default) | The trained network in `model/score.py` and `model/policy.py`. |
| `hft` | A rule-based high-frequency bot in `model/hft.py`. It uses no model. |

```text
FEE_MODEL=solana USE_CHAIN=1 npm start   # server: charge only the Solana network fee
BOT_STRATEGY=hft python -m model.live    # high-frequency rule bot
python -m model.live                     # back to the model
```

The server's `FEE_MODEL` flag sets what a fill costs:

- `exchange` (default): Phoenix-style fees, 2 bps maker and 5 bps taker.
- `solana`: no exchange fee. Each fill pays the network fee of its settlement transaction, two 5,000-lamport signatures priced at the SOL mid (about 0.1 cents). Cash is kept in whole cents, so fractions of a cent carry over to later fills.

`GET /v1/instruments` reports the active model under `fees`.

The `hft` bot holds one long position at a time:

- **Buy** when flat and the mid ticks down from the last mid it saw, with an IOC at the ask.
- **Sell** the whole position with an IOC at the bid, as soon as the sale (after its fee) returns more than the buy cost (including its fee) plus `HFT_MIN_PROFIT_CENTS` (default `0`).

The Solana fee is per transaction, not per dollar, so size decides how small a move pays. The bot trades enough lots that one price tick covers a round trip's fees `HFT_TICK_COVER` times (default `2`): about 40 BTC lots, or about 400 SOL lots. Size is capped at `HFT_MAX_LEVERAGE` times the stake (default `50`). `HFT_LOTS` sets a fixed size instead. Under the exchange fee model no size lets one tick pay, so the bot trades one lot.

There is no stop-loss. The session floor is the only downside limit.
