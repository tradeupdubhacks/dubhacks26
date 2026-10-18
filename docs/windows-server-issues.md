# Windows server issues

Followed [SETUP.md](SETUP.md) on Windows 10 (build 10.0.26300), Node v24.5.0. Agave 4.3.1 is installed at `%USERPROFILE%\.local\share\solana\install\active_release\bin`. `npm start` listens on `http://127.0.0.1:3000` and `GET /` returns the page. `chain.ready` never becomes `true`.

## What succeeds

`POST /v1/reset` and `POST /v1/step` for `BTC-USDT` return 200 while no session is open. One step came back with `reward: 0` and a one-level book. That path does not use the validator.

## Health

```json
{
  "ok": true,
  "mode": "localhost",
  "execution": "solana",
  "chain": {
    "rpc": "http://127.0.0.1:8899",
    "ready": false,
    "pool": "56hqnJSs7CQ2a4nYRaazJcYgaLuF7qNPkrNd598CmWMX"
  }
}
```

`POST /v1/session` then returns `503`:

```text
airdrop to 56hqnJSs7CQ2a4nYRaazJcYgaLuF7qNPkrNd598CmWMX failed: Internal error
```

Startup prints that airdrop error and still binds the HTTP port. The validator is not left running.

## 1. Git rewrites the Windows ledger template

`core.autocrlf` is `true` on this machine. A checkout turns text under `chain/fresh-ledger/rocksdb` into CRLF:

- `CURRENT` was `MANIFEST-000005\r\n` (17 bytes). RocksDB rejects that with `Corruption: CURRENT file corrupted`.
- `OPTIONS-000007` had a carriage return on every line (3024 of them).

`chain/fresh-ledger/** -text` is now in `.gitattributes`, and the working copies of `CURRENT` and `OPTIONS-000007` were rewritten to LF. The blobs already committed may still contain CRLF, so a fresh Windows clone can break the same way until those files are recommitted as LF.

## 2. The committed RocksDB directory is not a full ledger

`chain/fresh-ledger/rocksdb` in git has `CURRENT`, `IDENTITY`, `MANIFEST-000005`, and `OPTIONS-000007` only. It does not have `000004.log`. That log is inside `chain/fresh-ledger/genesis.tar.bz2`.

[src/chain.ts](../src/chain.ts) copies this directory and starts `solana-test-validator` without `--reset`, because `--reset` on Agave 4.3.1 hits `os error 5` while unpacking the genesis archive. With the incomplete copy, the validator exits:

```text
failed to process blockstore from genesis: failed to replay bank 0, did you forget to provide a snapshot
```

Extracting `rocksdb/000004.log` (and the matching `CURRENT`, `MANIFEST`, and `OPTIONS` files) from `genesis.tar.bz2` into the template gets past that. The validator then opens the ledger and produces slots.

## 3. Airdrop fails, so the chain never becomes ready

Once the validator is actually up, `getSlot` returns a slot, but funding the pool key does not. `solana airdrop 1` reports `airdrop request failed. This can happen when the rate limit is reached.` The server calls `requestAirdrop` for 2 SOL in `LocalSolana.fund()` and logs:

```text
airdrop to 56hqnJSs7CQ2a4nYRaazJcYgaLuF7qNPkrNd598CmWMX failed: Internal error
```

`chain.ready` stays `false`, and `POST /v1/session` stays `503`. The validator process then exits on its own (RocksDB shuts down cleanly). A later start replays the ledger and exits again within a few seconds, still during `Waiting for first slot`.

## Installer note

The `curl | sh` line in SETUP.md still refuses Windows. The Windows initializer from that same page works:

```text
https://release.anza.xyz/stable/agave-install-init-x86_64-pc-windows-msvc.exe
```

`agave-install-init.exe stable` installed 4.3.1 and put `solana-test-validator.exe` on the user PATH. The server now looks for that `.exe` itself.

## Causes and fixes (reproduced 2026-10-17)

Reproduced on Windows 11 build 10.0.26300 in a `dockurr/windows` VM, with Node 24.18.0, Python 3.12.10, Git for Windows 2.56 (default `core.autocrlf=true`), and Agave 4.3.1 from `agave-install-init.exe stable`. The repo was a fresh `git clone`, followed by `npm start` as in [SETUP.md](SETUP.md). Each failure below was seen in that VM and was gone after the fix.

### 1. Fresh clones have no `000004.log`

`.gitignore` has `*.log`. That rule also matches `chain/fresh-ledger/rocksdb/000004.log`, so the file was never committed, and every clone gets the incomplete ledger. The same error appears on Linux when the validator gets that directory without `--reset`:

```text
failed to process blockstore from genesis: failed to replay bank 0, did you forget to provide a snapshot
```

**Fix.** `.gitignore` now has `!chain/fresh-ledger/rocksdb/*.log`, and `rocksdb/000004.log` is the copy from `genesis.tar.bz2`. The template's `LOG` and `OPTIONS-000011`/`OPTIONS-000013` show that a validator was once run directly on `chain/fresh-ledger`. Its working copy of `000004.log` no longer matched the archive. Start validators on a copy, as `prepareLedger()` does.

### 2. Every airdrop fails on Windows

The validator log in `.local-solana/ledger/validator-*.log` shows the real error behind `Internal error`:

```text
request_airdrop_transaction: faucet_addr=0.0.0.0:9900 ...
request_airdrop_transaction failed: IoError(Os { code: 10049, kind: AddrNotAvailable,
  message: "The requested address is not valid in its context." })
```

`solana-test-validator` binds its faucet to `0.0.0.0:9900`, and its RPC then connects to that same address. Linux treats a connection to `0.0.0.0` as local. Windows rejects it (`WSAEADDRNOTAVAIL`). No flag changes the address. `solana airdrop` fails the same way, with the misleading message `This can happen when the rate limit is reached`.

**Fix.** `LocalSolana.fund()` in `src/chain.ts` now pays the pool and counterparty with a `SystemProgram.transfer` from `.local-solana/ledger/faucet-keypair.json`. The validator writes that key, and genesis funds it with 1,000,000 SOL, on Linux and Windows alike. `requestAirdrop` is only used for a validator the server did not start, which has no key file there.

With fixes 1 and 2, a fresh Windows clone reaches `chain.ready: true` on the first `npm start`. The validator stays up, and `POST /v1/session` returns `201`. The validator exits reported above were not seen after the fixes.

### 3. The model waits 2 seconds per request on `localhost`

The server binds IPv4 (`HOST=0.0.0.0`). Windows resolves `localhost` to `::1` first and waits about 2 seconds on the refused connection before it tries `127.0.0.1`. Measured in the VM with `ModelClient`:

| Base URL | 10 `GET /v1/health` calls |
| --- | --- |
| `http://localhost:3000` | 20.49 s |
| `http://127.0.0.1:3000` | 0.03 s |

`python -m model.train` makes one request per step, so it barely moves. `tests/test_api_client.py` probes with a 0.5 s timeout, so on Windows it always skipped the API tests.

**Fix.** `model/client.py`, `model/live.py`, `model/train.py`, and `tests/test_api_client.py` default to `http://127.0.0.1:3000`.

### 4. PyTorch needs the Visual C++ Redistributable

On a clean Windows install, `import torch` fails, so `python -m model.live` exits before it connects:

```text
OSError: [WinError 126] ... Error loading "...\torch\lib\c10.dll" or one of its dependencies.
Microsoft Visual C++ Redistributable is not installed
```

**Fix.** Install `https://aka.ms/vs/17/release/vc_redist.x64.exe`. SETUP.md now says so.

### Result

The VM test used a fresh clone with these fixes and Defender real-time protection on. `npm start` reached `chain.ready: true`, and a `BTC-USDT` session opened. `python -m model.live` connected to `ws://127.0.0.1:3000/v1/stream` and got 13 fills in 40 seconds. Each fill has a Solana transaction signature from the local validator.
