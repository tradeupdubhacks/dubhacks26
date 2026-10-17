# Windows server issues

Observed on Windows 10 (build 10.0.26300) while following [SETUP.md](SETUP.md) from this repo. Node was v24.5.0. The HTTP server does start. A live session does not.

## What works

- `npm install` and `npm start` succeed.
- The process listens on `http://127.0.0.1:3000`.
- `GET /` returns the shop page (`200`, `text/html`).
- Agave can be installed. The Unix installer in SETUP.md cannot be used here; see the first issue.

## Health after startup

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

[SETUP.md](SETUP.md) says `execution` should be `"solana"` and `chain.ready` should be `true`. `ready` stays `false`, so `POST /v1/session` returns `503` `chain_unavailable`.

Startup also prints:

```text
The local validator exited before http://127.0.0.1:8899 accepted requests. See .local-solana/validator.log.
```

## 1. SETUP.md installer does not run on Windows

[docs/SETUP.md](SETUP.md) installs the CLI with:

```bash
curl -sSfL https://release.anza.xyz/stable/install | sh
```

That script rejects anything that is not Linux or macOS (`machine architecture is currently unsupported`). The same release publishes a Windows initializer:

```text
https://release.anza.xyz/stable/agave-install-init-x86_64-pc-windows-msvc.exe
```

Running `agave-install-init.exe stable` installed Agave 4.3.1 and added this directory to the user PATH:

```text
C:\Users\akash\.local\share\solana\install\active_release\bin
```

`solana-test-validator.exe --version` then printed `solana-test-validator 4.3.1`. The file without the `.exe` suffix is not present.

## 2. The server does not find the Windows validator binary

[src/chain.ts](../src/chain.ts) `findValidator()` checks this path and then runs `which`:

```text
%USERPROFILE%\.local\share\solana\install\active_release\bin\solana-test-validator
```

On this machine that path does not exist. The installed file is `solana-test-validator.exe`. `which` is not a Windows command, so the lookup returns null and the server reports that the validator is not installed.

Workaround used for the restart: set `SOLANA_TEST_VALIDATOR` to the full `.exe` path. After that, the process does launch. It still exits during ledger creation (next issue).

## 3. Validator child PATH is joined with `:`

In `LocalSolana.launch()` the child environment is built as:

```text
PATH = dirname(binary) + ":" + process.env.PATH
```

Windows PATH entries are separated by `;`. `path.delimiter` is the portable separator. This did not stop the `.exe` from starting, because it was spawned by full path, but any tool the validator looks up on PATH will not be found.

## 4. solana-test-validator exits while unpacking genesis

With a fresh `.local-solana\ledger`, Agave 4.3.1 prints:

```text
Error: failed to start validator: Failed to create ledger at C:\Akash\Coding\dubhacks26\.local-solana\ledger: io error: Error checking to unpack genesis archive: IO error: Access is denied. (os error 5)
```

The same error happens for a second ledger directory (`.local-solana\ledger2`) and when the validator is started by hand, not only when the Node process spawns it. The log stops at:

```text
agave_snapshots::unarchive Extracting "...\ledger\genesis.tar.bz2"...
```

The failed archive is left as `genesis.tar.bz2.failed`. It contains `genesis.bin` and a `rocksdb\` directory, including `rocksdb\LOCK`. The log shows the validator opening `ledger\rocksdb` before that extract. On Windows an open `LOCK` file cannot be replaced, which matches `os error 5`. This needs a Windows fix in the validator startup, or a supported way to run the validator (the Agave Windows issues point at WSL). WSL is not installed here.

Until the validator stays up on port 8899, `chain.ready` stays `false` and a live session cannot be created.

## 5. Model client disagrees with this server's health

[docs/model-api.md](model-api.md) and [docs/client-api.md](client-api.md) show:

```json
{ "ok": true, "mode": "localhost", "execution": "fake" }
```

The client API text says `execution` is always `"fake"`. [src/app.ts](../src/app.ts) returns `"solana"` whenever a chain object is passed, which [src/index.ts](../src/index.ts) always does. [model/client.py](../model/client.py) refuses to trade unless `execution` is `"fake"`.

So with the server from `npm start`:

- The paper bot exits before opening `/v1/stream`.
- Even if that check were removed, `POST /v1/session` still returns `503` until issue 4 is fixed.

The in-process suite `npm test` passed (14 tests). Those tests build the app with `buildApp()` and a stand-in chain. They do not launch `solana-test-validator`.
