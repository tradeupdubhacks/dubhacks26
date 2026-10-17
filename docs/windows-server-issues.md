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
