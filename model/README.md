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

The `hft` bot trades with the top-of-book imbalance, `(bid size - ask size) / (bid size + ask size)`:

- **Enter** when flat: buy with an IOC at the ask when imbalance is at least `HFT_ENTER_IMBALANCE` (default `0.6`). Sell short at the bid when it is at most minus that.
- **Exit** when the book stops leaning its way (imbalance past `HFT_EXIT_IMBALANCE`, default `0`), or after `HFT_MAX_HOLD_S` seconds (default `5`).
- **Size** is `HFT_LOTS` (default `100`, about $8,400 of BTC), capped at `HFT_MAX_LEVERAGE` times the stake (default `100`).

There is no stop-loss beyond the hold limit. The session floor is the hard limit.

**Results.** The rule was tuned with 10-minute simulations run through the server on recorded BTCUSDT data, with a $100 deposit, $0 floor, $105 ceiling and `FEE_MODEL=solana`. Settings were picked on 4 windows and checked on 22 others (2026-10-01 at every hour used, plus two windows from 2026-10-17). It reached +$5 in all 26:

- The median time to +$5 was about 1.5 minutes. The slowest was 7.9 minutes.
- The deepest dip on the way was -$1.33.
- A live session on the running stack hit +$5.32 in 2 minutes 38 seconds, with every fill settled on the local validator.

The earlier rule, buying on any downtick, lost money in the same simulations. BTC's short-term moves continue rather than reverse.

**Limits:**

- Fills happen at the recorded top of book after the server's 50ms latency. There is no queue competition and no market impact beyond the displayed size.
- The edge depends on `FEE_MODEL=solana`. A round trip of 100 lots gains about 24 cents on average. With the exchange fee model's 5 bps per taker fill, the same round trip would pay about $8.35 in fees.
- `BTC-USDT` sessions replay 2026-10-01 from its start, so a live session is not new data.
- On `SOL-USD` the server's quotes are a synthetic ladder, so the imbalance carries no signal there.

## Session policy

`python -m model.live session` runs a goal-seeking policy for a live user session (reach the ceiling, protect the floor)
(`model/live_session.py`; the default `python -m model.live` is the network/HFT bot described above, chosen by `BOT_STRATEGY`). Inputs, outputs, sizing, results and limits are in [SESSION_MODEL.md](SESSION_MODEL.md).
Train with `python -m model.tape_grid && python -m model.train_session && python -m model.evaluate_session`.

## Transformer

`python -m model.live transformer` (or the page's Trading model dropdown) runs the tick transformer. See [TRANSFORMER.md](TRANSFORMER.md).
