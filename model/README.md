# Paper trading bot

This process places paper orders on the model API in [docs/model-api.md](../docs/model-api.md). It does not open a user session and it does not edit the page. Fills are the server's fake matches. `BTC-USDT` follows that server's recording. `SOL-USD` follows that server's public mid. Nothing here is a low-latency exchange connection.

Start the API server on `http://localhost:3000` first. `GET /v1/health` must report `execution: "fake"` or `"solana"`. `npm start` reports `"solana"` because live fills settle on the local validator. The bot exits for any other value.

```text
pip install -r model/requirements.txt
python -m model.train
python -m model.live
```

`python -m model.train` calls `POST /v1/reset` and `POST /v1/step` on `BTC-USDT` when the server is idle. If a live session is open, or the server is down, it builds the same features from `hft/data/raw/BTCUSDT_20261001_book_ticker.csv.gz` instead. The network fits on CUDA when PyTorch can see the GPU, and the log prints the device name. Live quoting loads those weights on the CPU.

The held-out report is `model/artifacts/eval_report.json`. It compares the signal, after a 5 bps taker fee, with doing nothing. It covers one recorded day and is not a claim that the strategy makes money. The join-the-touch figure, when present, comes from `hft/run_backtest.py` and is a different simulator.

The page owns the time limit and calls `POST /v1/stop`. The bot reads the floor and ceiling from the observation and stops quoting when the session is halted or the feed is `stale`.

A `BTC-USDT` lot is `0.001` BTC, about `$83` of notional. The bot flattens instead of quoting when one lot would cross the floor or ceiling, so a `BTC-USDT` session needs room for a full lot on both sides, for example deposit `$100`, floor `$0`, ceiling `$200`. A `$50` deposit with a `$25` floor never quotes.

The live loop tracks its own in-flight places and cancels from the `events` replies, decides at most every `100ms`, and uses a new `clientOrderId` prefix on every connection, because the server remembers every id it has seen. With no session open it pings every `5s` and prints `waiting for a live session` instead of reconnecting.
