# Run the server

One process serves the model API, the user API, and the page. Live fills settle on a local Solana validator. Training stays in memory and does not send transactions.

Commands below are run from `dubhacks26/`.

## Install

1. **Node.js 20 or newer** and npm.

2. **JavaScript dependencies**

   ```bash
   npm install
   ```

3. **Agave Solana CLI**, which provides `solana-test-validator`. The server starts that validator itself when port `8899` is free.

   ```bash
   curl -sSfL https://release.anza.xyz/stable/install | sh
   export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"
   ```

   The installer also adds that `PATH` line to `~/.profile` and `~/.bash_profile`. Open a new shell, or run the `export` line, before the first start.

   On Windows, that script refuses to run. Download `https://release.anza.xyz/stable/agave-install-init-x86_64-pc-windows-msvc.exe`, run `agave-install-init.exe stable`, and open a new terminal so `solana-test-validator.exe` is on `PATH`. The server looks for the `.exe` under `%USERPROFILE%\.local\share\solana\install\active_release\bin`. Agave 4.3.1 cannot create a new ledger on Windows (`os error 5` while checking `genesis.tar.bz2`), so a Windows start copies `chain/fresh-ledger` and does not pass `--reset`. Linux and macOS still pass `--reset` and create a ledger themselves.

The BTC replay files are already in `hft/data/raw/` (`BTCUSDT_20261001_book_ticker.csv.gz` and `BTCUSDT_20261001_trades.csv.gz`). A live `SOL-USD` session reads a public mid from Binance, then Jupiter, then Coinbase, so that machine needs outbound HTTPS.

## Start

```bash
npm start
```

The HTTP server listens on `http://127.0.0.1:3000`. On startup it launches `solana-test-validator` at `http://127.0.0.1:8899` and creates two dummy token accounts under `.local-solana/`. The first start can take about half a minute. Validator logs go to `.local-solana/validator.log`.

Check that the chain is up:

```bash
curl -s http://127.0.0.1:3000/v1/health
```

`execution` should be `"solana"` and `chain.ready` should be `true`. If the validator binary is missing, the HTTP server still starts, but `POST /v1/session` returns `503`.

Optional environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT` | `3000` | HTTP and WebSocket port |
| `HOST` | `0.0.0.0` | Bind address |
| `SOLANA_RPC` | `http://127.0.0.1:8899` | Validator the gateway settles against |
| `SOLANA_TEST_VALIDATOR` | the Agave install path | Validator binary, if it is not in the default location |

`npm run dev` is the same server with file reload.

## Frontend

Open `http://127.0.0.1:3000/`. The page is served by this process. It can open a session, watch the stake, and stop. It cannot place orders.

A session is a deposit of `$1` to `$100` pooled with a `$100,000` house fund, plus a floor and a ceiling. While it is running, `ws://127.0.0.1:3000/v1/watch` pushes the stake, book, and fills. The full request shapes are in [docs/client-api.md](docs/client-api.md).

## Model

Point the model at `http://127.0.0.1:3000`. There is no API key.

On Windows, PyTorch also needs the Microsoft Visual C++ Redistributable (`https://aka.ms/vs/17/release/vc_redist.x64.exe`). Without it `import torch` fails with `WinError 126` and the model exits before it connects. Use `127.0.0.1`, not `localhost`: Windows tries `::1` first and adds about 2 seconds to every request.

- **Training**, with no page session open: `POST /v1/reset`, then `POST /v1/step`. These do not send Solana transactions.
- **Live trading**, after the page has created a session: `POST /v1/orders` and `ws://127.0.0.1:3000/v1/stream`. Each fill is an SPL token transfer on the local validator. The fill's `signature` is that transaction.

`POST /v1/reset` returns `409` while a live session exists. Stop the session from the page first. The full request shapes are in [docs/model-api.md](docs/model-api.md).

### Running both strategies

The page lets the user pick a strategy for each Bitcoin session. The server tags the session with it, and each bot trades only its own:

| Page option | `strategy` | Bot |
| --- | --- | --- |
| Fast, higher risk | `fast` | `BOT_STRATEGY=hft python -m model.live` (`model/hft.py`) |
| Steady, lower risk | `safe` | `python -m model.live` (the trained model, `MODEL_POLICY=hybrid`) |

Run both bots at the same time, next to the server:

```bash
FEE_MODEL=solana USE_CHAIN=1 npm start
python -m model.live                     # trades safe sessions
BOT_STRATEGY=hft python -m model.live    # trades fast sessions
```

Each bot ignores a session that belongs to the other strategy, so the two never trade the same session. Both were tuned with `FEE_MODEL=solana`.

Several people can trade at the same time, up to 20 sessions. Each one is separate: its own stake, floor, ceiling, and strategy. One process of each bot trades all of its strategy's sessions, one task per session.

### Live Bitcoin prices

By default (`BTC_FEED=live`), a live `BTC-USDT` session uses the live Binance USD-M BTCUSDT book: best bid and ask with sizes, plus trades, over the public websocket (`wss://fstream.binance.com`). This is the same feed the recorded tape and the bots' tuning data came from.

- Fills still settle on the local Solana validator.
- The server samples the feed every 200ms from startup and hands each new session the last two minutes as bar history.
- Until two minutes have passed since startup, `Slower` waits for its window to fill.
- If the feed goes quiet for 5 seconds, `priceSource` becomes `stale` and the bots stop sending orders.
- `POST /v1/session` returns `503 feed_unavailable` before the first price arrives.
- `POST /v1/step` is refused on a live-feed session (`409 step_not_for_live_feed`).

`BTC_FEED=tape` replays the recorded 2026-10-01 tape for live sessions instead. Training (`POST /v1/reset`) always uses the tape.

Binance's REST API refuses some regions. The websocket stream worked from the machine this was built on.
