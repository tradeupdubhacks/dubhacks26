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
USE_CHAIN=1 npm start
```

Without `USE_CHAIN`, the server skips the validator and fills are faked (`/v1/health` reports `"execution":"fake"`).

The HTTP server listens on `http://127.0.0.1:3000`. On startup it launches `solana-test-validator` at `http://127.0.0.1:8899` and creates two dummy token accounts under `.local-solana/`. The first start can take about half a minute. Validator logs go to `.local-solana/validator.log`.

Check that the chain is up:

```bash
curl -s http://127.0.0.1:3000/v1/health
```

`execution` should be `"solana"` and `chain.ready` should be `true`. If the validator binary is missing, the HTTP server still starts, but `POST /v1/session` returns `503`.

Optional environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `USE_CHAIN` | unset | Set to any value to start the validator and settle fills on it |
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

### Models and several users

Each person picks a model for their own session on the page. The session is tagged with the model's id, and that model's bot trades only sessions with its id:

| Page card | `strategy` | Bot |
| --- | --- | --- |
| Faster, high risk | `hft` | `model/hft.py`, the rule bot (`BOT_STRATEGY=hft python -m model.live`) |
| Slower, low risk | `network` | the team network with the hybrid policy (`python -m model.live`) |
| Transformer, medium risk | `transformer` | `python -m model.live transformer` (`model/live_session.py`, [TRANSFORMER.md](../model/TRANSFORMER.md)) |
| Small network, rarely trades | `small` | `python -m model.live session` ([SESSION_MODEL.md](../model/SESSION_MODEL.md)) |

`POST /v1/session` still accepts the older names `fast` (for `hft`) and `safe` (for `network`).

**Starting the bots.** The server starts them itself (`src/bot.ts`). Each model's bot process starts when the first running session needs that model, and stops when its last session ends. One process per model trades all of that model's sessions, one task per session.

- `MODEL_BOT=off` starts nothing. You then run the commands in the table yourself; each one also trades only its own model's sessions.
- `GET /v1/bot` lists the models.
- `POST /v1/bot {"model": "..."}` sets the default for a session that names no model (`MODEL_BOT_KIND`, default `transformer`). It does not change sessions that already chose a model.

**Limits.** Up to 20 sessions trade at once. Each one is separate: its own stake, floor, ceiling, and model. All the models were tuned with `FEE_MODEL=solana`.

