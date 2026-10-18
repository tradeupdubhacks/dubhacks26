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
