# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A dummy-money trading platform: a TypeScript server (`src/`) that simulates a market and a stake ledger, a static web page (`frontend/`) where a person opens and watches a session, and a Python trading model (`model/`) that trades that session over an API. Accounts, cash and fills are ledger entries on localhost; nothing is sent to an exchange. `hft/` holds market data and offline research.

## Commands

```bash
npm install
npm start                      # server on :3000 (HOST/PORT env); starts the Solana validator itself if it can find one
npm run dev                    # same, with file reload
npm run typecheck              # tsc --noEmit
npm test                       # node:test via tsx, test/*.test.ts
npx tsx --test --test-name-pattern="<name>" test/api.test.ts      # one server test

pip install -r model/requirements.txt pytest
python -m pytest tests -q      # run from the repo root; modules are imported as `model.*`
python -m pytest tests/test_policy.py::test_name

python -m model.live                 # teammates' join-the-touch quoter (artifacts/signal.pt)
python -m model.live session         # small session network (artifacts/session_signal.pt), BTC-USDT only
python -m model.live transformer     # tick transformer (artifacts/transformer_signal.pt), BTC-USDT only; train with python -m model.train_transformer
python -m model.train                # needs the server idle: uses POST /v1/reset + /v1/step
python -m model.tape_grid && python -m model.train_session && python -m model.evaluate_session
```

Some `tests/test_api_client.py` cases skip unless a server is on `:3000`. The server tests use `buildApp()` directly and need no network. The validator is optional: without `solana-test-validator` (or `SOLANA_TEST_VALIDATOR=/path`) the server still starts, training works, and `POST /v1/session` returns 503.

Environment switches: `MAKER_BPS` / `TAKER_BPS` (trading fees, **default 0**; Binance's rates are 2 and 5), `MODEL_BOT=off` (server does not spawn the bot), `MODEL_BOT_KIND` (default model, see below), `MODEL_BOT_PYTHON`, `MODEL_PROFILE=idle|active` (default `active`), `MODEL_API_URL` (bot's server address), `SOLANA_RPC`.

## Architecture

**Server (`src/`)** is a single in-memory `Market` (`market.ts`, the matching/risk/stake core) behind Fastify routes (`app.ts`). Two modes that never overlap: *training* (`/v1/reset` + `/v1/step`, time moves only when called) and a *live session* (the page calls `POST /v1/session`, wall clock ticks every 200 ms). Stake = deposit + pool PnL, pooled with a $1000 house fund; the session halts at the floor or ceiling. Instruments: `BTC-USDT` replays the recorded tape `hft/data/raw/BTCUSDT_20261001_*` (`tape.ts`, top of book only); `SOL-USD` follows a public mid in live mode and a synthetic book in training. `chain.ts` settles live fills as SPL transfers on a local validator. `docs/model-api.md` and `docs/client-api.md` are the contracts; units are integer cents, price in cents per coin, size in lots of 0.001.

**Who talks to what.** The page uses REST + the read-only `/v1/watch` socket and cannot trade. The model is a separate Python process on `/v1/stream` (orders, cancels, flatten). `src/bot.ts` ties them together: `publish()` in `app.ts` calls `BotRunner.sync()`, which spawns the selected model (`BOT_MODELS`: `small`, `transformer`, `quoter`, `none`, each a `python -m model.live <kind>`) when a session it can trade is running, and kills it when the session ends. The page's **Trading model** dropdown reads `GET /v1/bot` and sends `POST /v1/bot {model}` just before `POST /v1/session`. Nothing else starts a bot.

**Model (`model/`)** has two policies on the same plumbing (`client.py`, `live.py`, `policy.py` types):
- Quoter: `features.py` (4 inputs), `net.py`, `policy.decide`, `train.py`.
- Session policy: `features_session.py` (11 inputs, all read from one `Observation`), `sizing.py` (lots sized to the floor/ceiling), `session_policy.decide_session` (IOC entry, resting limit take-profit, flatten stop), `train_session.py`, `evaluate_session.py`. `SignalNet(n_features)` is shared; artifacts hold weights, normalisation and params. `live.PROFILES` overrides the saved params (`idle` barely trades by design).
- `backtest.py` re-implements the server's fill rules (50 ms delay, touch-price fills, resting orders fill when the quote crosses, 2/5 bps fees, floor/ceiling halts) on a 50 ms grid built by `tape_grid.py`. It was checked against the real server (`hft/research/13_server_vs_replica.py`). If you change matching, fees or latency in `src/market.ts` / `src/types.ts`, update `backtest.py` and `sizing.py` too.

**Research (`hft/research/`)** is numbered scripts (01..20) with results in `results/` and the write-up in `REPORT.md`. Extra data is downloaded by `hft/download_extra.py` into `hft/data/ext/` (git-ignored, ~3 GB). The scripts also need pandas, scipy, scikit-learn and matplotlib, which are not in any requirements file.

## Things that will bite you

- **Fees are in `src/types.ts`** and are **off by default** (env `MAKER_BPS` / `TAKER_BPS`; Python reads the same env, and the server passes its values to the bot). The model has a real but tiny edge (about 0.6 bps per 5 s decision): `model/TRANSFORMER.md` shows the transformer profitable with fees off and losing nearly everything at 2/5 bps. All older research numbers (`hft/research/REPORT.md`, `SESSION_MODEL.md`) assume 2/5 bps. Read both before trying to "improve" a model.
- **The page only works at `http://localhost:3000`.** `frontend/app.js` hard-codes `API_BASE`; `/`, `/app.js` and `/style.css` are served by `app.ts` (the page is not built or bundled).
- **Port 3000 can hold a stale server** from an earlier run that still has old code; restart it after server changes.
- **Git LFS** tracks `*.npz` and `*.gz`. `hft/data/raw` and `hft/data/out` are real files; a linked worktree or `git archive` yields pointer files, not data. `.venv/`, `hft/data/ext/` and generated grids are git-ignored; do not commit them. `chain/fresh-ledger/**` must stay LF (`-text`) or RocksDB rejects it on Windows.
- **Branches differ.** Work was split across `main` and `balls`; check `git branch` before assuming a file exists, and commit rather than leave changes uncommitted when switching branches.
- **Python version:** the repo's `.venv` is 3.9, teammates use newer. Keep `from __future__ import annotations` in `model/` and avoid runtime 3.10+ syntax.
- **macOS validator:** `solana-test-validator` needs `libusb`; the `DYLD_*` variables are stripped when launched through `npm`, `nohup` or `/usr/bin/env`, so use `brew install libusb`.
- Sessions replay one recorded BTC day at wall-clock speed, so hour-long strategies cannot be demonstrated live (use `/v1/step` in training mode).
