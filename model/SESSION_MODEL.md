# Session model: reach the ceiling, protect the floor

A second policy beside the join-the-touch quoter. It is for a live session whose user stake is
`deposit + pool PnL`, with a floor and a ceiling (docs/client-api.md). It speaks only the model API.

```text
python -m model.tape_grid        # one-off: 50 ms replay grid from hft/data/raw (writes hft/data/out/*grid50ms.npz)
python -m model.train_session    # walk-forward fit, writes artifacts/session_signal.pt
python -m model.evaluate_session # calibrate on 10-14 h, test on 14-24 h, writes artifacts/session_eval_report.json
python -m model.live session     # trade it against the server (BTC-USDT only); `python -m model.live` is unchanged
```

## Inputs (one `Observation`, nothing else)

`features_session.session_features(observation)` returns 11 numbers from fields the server already sends:

| # | Feature | Source field |
| --- | --- | --- |
| 1 | `imbalance_top` = (bidSize - askSize) / (bidSize + askSize) | `bids[0]`, `asks[0]` |
| 2 | `micro_dev_bps` micro-price vs mid | `bids[0]`, `asks[0]`, `mid` |
| 3 | `spread_bps` | `bids[0]`, `asks[0]` |
| 4-7 | `ret_1/5/15/31_bps` mid return over the last 1, 5, 15, 31 ticks | `mids` |
| 8 | `vol_bps` RMS tick-to-tick mid move | `mids` |
| 9 | `log_top_size` | top-of-book sizes |
| 10 | `trade_signed_log_size` | `lastTrade.side`, `.size` |
| 11 | `trade_vs_mid_bps` | `lastTrade.price`, `mid` |

The replay has one book level per side, so deeper levels carry no information here. Position, equity and stake are
deliberately **not** inputs to the network. The policy reads them for sizing and exits (`positionLots`,
`unrealizedPnlCents`, `stakeCents`, `floorCents`, `ceilingCents`, `openOrders`). Features are standardised with
training-window statistics stored in the artifact (`mean`, `std`, clipped at +-5).

## Output

`SignalNet` (11 -> 32 -> 16 -> 1) returns **P(mid is higher 25 ticks = 5 s from now)** in [0, 1]. That is the same
contract as `live.load_scorer` (`score(features) -> float`), and the artifact has the same keys as `signal.pt`
plus `n_features`, `horizon_ticks`, `tick_ms` and `session_params`.

`session_policy.decide_session(...)` turns the score into the existing `policy.Decision`:

| When | Order sent (model-API shape) |
| --- | --- |
| Flat, confident, volatile enough, rails leave room | `ioc` at touch +- 2 ticks, size from `sizing.plan_trade` |
| Position open, no exit working | one resting `limit` take-profit on the other side (price on the tick) |
| Loss >= stop, hold time exceeded | `flatten` |
| Halted / stale feed / empty book | nothing / `cancel_all` / nothing |

Sizing: lots are the smallest that lets one take-profit reach the ceiling, capped so one stop-out (stop + 2 taker
fees + slippage) cannot cross the floor, and capped by `maxOrderLots` / `maxPositionLots`. It returns no trade if
the take-profit does not clear 5 + 2 bps of fees. The server still enforces the floor and ceiling itself.

## Fit with the framework

* Same `Observation`, `Order`, `Decision`, `IdSource`, `InstrumentLimits`, rate limit and tick rules; no new endpoint.
* Same live loop: `python -m model.live session` loads `session_signal.pt` and calls `decide_session`; default behaviour is untouched.
* `net.SignalNet` gained an optional `n_features` argument (default 4), so `signal.pt` still loads.
* Features are built for training by calling the same function on observation dicts, so train and live cannot drift apart.
* The live loop sends a fresh snapshot after every command and throttles decisions to 100 ms, so the policy times its hold limit and
  cooldown with the observation's market `ts`, not by counting calls; it also decides on the loop's in-flight view of open orders.
* The 200 ms tick matches the live wall tick, and `mids` (32 entries) is a 6.4 s window.
* The server is not run in this repo's environment, so evaluation uses `backtest.py`, which mirrors `src/market.ts`
  and `src/tape.ts` (50 ms delay, touch-price fills, 2/5 bps fees, crossing rest fills, floor/ceiling halts). Its fast
  engine matched a step-by-step engine that runs the real `decide_session` on 70 of 72 test sessions; the 2 misses are
  a 10 bps take-profit (below the fee breakeven) and differ by at most 12 cents. It has not been run against the live
  server; do that before relying on any number here.

## Verified against the running framework

Run with Node 22, the TypeScript server and an Agave 4.3.2 `solana-test-validator` (`chain.ready: true`):

* `npm run typecheck` clean; `npm test` 18/18; `pytest tests` 27 passed, 1 skipped (a contract probe that skips on a halted session).
* The team's `python -m model.train` runs against the live server (2000 `reset`/`step` calls).
* **Replica vs server** (`hft/research/13_server_vs_replica.py`): the policy ran 86,000 `step`s (4.8 h of tape) on the real server; the same commands
  replayed in `backtest.StepEnv` gave identical position on 100% of steps, mid/bid/ask prices equal on 99.9%, sizes equal on about 95%,
  and equity within 4 cents throughout (7 entries, a take-profit fill, 6 time-stop flattens). So the backtest tables above reflect this server's fills and fees.
* **Live session**: `POST /v1/session` (deposit $100, floor $98, ceiling $101), then `python -m model.live session` over `/v1/stream` opened a 9-lot short
  sized to the rails, placed the take-profit limit, and the stake tracked the mark. The fill carried a Solana signature and a 37-cent fee, the same
  as `sizing`/`backtest` compute. `POST /v1/stop` halted it and froze the stake.
  That smoke run used aggressive parameters (threshold 0, 15 bps) only to exercise the order path; the shipped parameters are far more selective.
* Not checked in a browser. Two issues found outside `model/`: `GET /style.css` and `GET /app.js` return 404 (the server serves only `/`),
  and the API has no CORS handling, so the page cannot load or call the API as committed (`docs/SETUP.md` says the server serves the page).

## Results (one recorded day, BTCUSDT 2026-10-01)

Signal quality, out of sample (all samples, rank/Pearson correlation of score with the realised 5 s mid move):
0.16 / 0.21 / 0.29 across the three walk-forward folds; mean absolute 5 s move is 0.35-1.4 bps. Gross edge is
about 0.3-0.6 bps per decision at best. The edge fades with horizon (60 s: IC ~0.04, 5 min: ~0, noisy beyond).

Sessions (deposit $100, start every 2 min, calibrated on 10-14 h, tested on 14-24 h; 270 overlapping sessions,
about 10 hours, CIs by hour-block bootstrap):

| Goal / floor | Policy | P(ceiling) | P(floor) | Mean stake change |
| --- | --- | --- | --- | --- |
| +1% / -2% | calibrated (deployed) | 7% [0, 24] | 0% | +0.00% |
| +1% / -2% | random direction, same params | 3% | 0% | -0.59% |
| +5% / -5% | calibrated | 3% [0, 8] | 0% | +0.01% |
| +5% / -5% | forced to trade (P(ceiling)-max params) | 3% | 4% | -3.76% |
| +5% / -10% | forced to trade | 3% | 0% | -6.32% |
| +1% / -1% | forced to trade | 5% | 0% | -0.53% |
| +1% / -1% (diagnostic) | forced to trade, **zero fees** | 29% [1, 54] | 0% | +0.06% |

Read this plainly: the calibrated policy mostly stays flat (0.2 trades per session), so it neither reaches 1% nor
loses money. Forcing it to trade loses 0.5-6% of the deposit, and random direction does about as badly. Even with
zero fees it reaches +1% in under a third of sessions with a wide interval. The calibration windows are one
trending stretch; the best calibration config (100% hits) did not carry over, which is why the hour-block
intervals are wide.

## More data (Binance public archive; details in hft/research/REPORT.md section 7)

* The 5 s book signal is stable: trained on 2026-10-01, it has positive rank correlation on all 10 unseen days from 2023-24
  (mean 0.18); leave-one-day-out over 11 days gives the same. Gross edge about 0.2 bps per decision (0.45 bps at top-10% confidence).
  After costs it is negative on every day (about -3.5 bps maker, -10.5 bps taker per round trip).
* Across 6 coins and 2022-2026 (10,398 day-sessions per cell, 1-minute data): +1% goal / -2% floor is reached in about 56-58% of
  day-sessions and the rest end near the floor (about -1.8%); mean stake change -0.2% to -0.3%. +5% / -5% is reached in 34-40%
  with mean change -0.6% to -1.6%. The model beats random direction by 1-3 points of hit rate; the rest is the payoff shape of the
  bracket, not forecasting skill.

## Why the 1-5% target is still not reachable here

The fee schedule (2 bps maker, 5 bps taker) means a round trip costs 7-10 bps. The signal is worth about 0.5 bps
over 5 s and nothing over the 15-60 bps horizons a take-profit needs. Deposit size does not change this: sizing
scales position with the target, so costs scale with it. What would change it: lower or negative fees (maker
rebates), a signal with real edge at minutes-to-hours horizons (needs far more than one day of data, and other
assets), or a different customer arrangement where the platform carries the shortfall.
