# Tick transformer (short-hold scalper)

A transformer that reads the last 30 s of top-of-book ticks and predicts the BTC mid-price move over the next 5 s. The server can start it
from the page's **Trading model** dropdown, or by hand: `python -m model.live transformer`.

```text
python -m model.train_transformer      # ~10 min on the Apple GPU; writes artifacts/transformer_signal.pt
python .venv/bin/python hft/research/22_transformer_policy_eval.py   # policy on the replay day, fees off / on (repo root)
```

## Inputs and output
- **Input:** a window of 150 ticks (200 ms apart) x 11 features, all from `bids[0]`, `asks[0]` and `ts` of a model-API observation:
  book imbalance, micro-price offset, spread, log total/bid/ask size, 1-tick mid return, 1-tick change of the two log sizes, 5-tick change
  of imbalance and bid price. `transformer_signal.grid_features` is the one function used for training and live, and
  `tests/test_transformer_signal.py` checks that the live stream gives the same numbers as the training grid.
- **Output:** predicted mid move in bps over the next 25 ticks (5 s). `score = 0.5 + 0.5 * clip(pred / scale, -1, 1)` feeds `decide_session`.
  `scale` (0.80 bps) and the entry confidence (0.72, the validation day's top 10 % of ticks) are stored in the artifact.
- **Warm-up:** the model needs 155 ticks (31 s) of history. In a live session the bot loads it at once from `GET /v1/bars` (the server's 2-minute
  pre-roll of the tape, `live_session.prime`), so it can act from the first second. Without that endpoint it warms up from live snapshots.
- **When it first trades:** at entry confidence 0.40 it trades from the first second (first fill 0 to 12 s live). It used to wait for confidence 0.72 (the
  top ~6 % of ticks), and the replayed day is quiet right after the 2-minute pre-roll, so the first fill came at about 136 s. The first strong signal, at session 6 s,
  is a 3 bp drop in 50 ms that an IOC with a 2-tick cap cannot catch.

## Model and training
2 pre-norm encoder layers, width 64, 4 heads, learned positions, mean + last-token pooling, ~100 k parameters. Huber loss on the 5 s move clipped to +-15 bps,
3 epochs, AdamW. Train: 9 Binance days (2023-06 .. 2024-03, from `hft/download_extra.py`); validation 2023-12-15 (sets scale and threshold only);
test: 2026-10-01, the day the server replays, which the model never saw.

## Policy
Enter (IOC at the touch +- 2 ticks) when confidence >= 0.40 (`TRANSFORMER_ENTRY`, default in `live_session.DEFAULT_ENTRY`), sized to the largest position the floor allows; **no take-profit**; flatten after
25 ticks (5 s) or at a 25 bps stop; 1 s cooldown. It is the short hold that matches the 5 s forecast; the small network's 10-minute hold does not.

## Results on the replay day (out of sample), through `backtest.StepEnv`
These figures were measured at entry confidence 0.72, before the default moved to 0.40 (see the last section for the 0.40 numbers).
| | trades | price P&L | fees | stake change on $100 |
| --- | --- | --- | --- | --- |
| **fees off (platform default)** | 5,042 (210/h) | +$229.80 | $0 | **+$229.80** |
| fees 2 bps maker / 5 bps taker | 135 | +$5.30 | -$105.02 | **-$99.72** |
| Solana network fee only (0.1 cent per fill, `FEE_MODEL=solana` on origin/balls) | 5,042 | +$229.80 | -$10.08 | **+$219.72** (`hft/research/23_solana_fee_check.py`) |
| fees off, page defaults (-20% / +5%) | 114 | +$5.04 | $0 | +$5.04, goal reached after ~40 min |

With fees off: profitable in 23 of 23 hours (worst +$1.6, best +$21), 75.6 % of closed trades win, +5.5 cents average, largest stake dip $1.77.
Held-out rank correlation 0.22, gross edge 0.63 bps at the top 10 % of calls (0.75 bps at the top 1 %).

## What this does and does not show
- **It is profitable only because fees are off.** The edge is about 0.6 bps per trade; one round trip at 2/5 bps costs 4 to 10 bps, which is why the
  same policy loses almost everything with fees on. Fees are set in `src/types.ts` (`MAKER_BPS`, `TAKER_BPS`, default 0).
- The simulator is generous in ways a real venue is not: `flatten` executes instantly at the touch, there is no queue position or market impact
  (10 lots is about $835 against thousands of lots at the touch), and fills happen at the quoted price after only the 50 ms delay.
- One replay day for the end-to-end result. On the 4 held-out Binance days the edge was positive on all four (0.39 to 0.70 bps at the top 10 %), so the
  sign is stable, but the dollar figure above is one day.
- By default (`BTC_FEED=live`) a session now follows the live Binance book, which this model never saw: a first 6-minute test on a $20 stake made +$0.72 over ~36 fills (very small sample, same generous simulator assumptions). With `BTC_FEED=tape` the session replays the recording from its start (after a 2-minute pre-roll), so the page shows that day's first minutes and hours.

## Entry confidence: speed and profit (`hft/research/24_transformer_start_speed.py`, `25_threshold_validation.py`)
Full-day P&L on a $20 stake sized to a $16 floor (about 18 lots), Solana fee only, session started as the live server starts it. Chosen on the
validation day (never trained on), not on the replay day.

| Entry confidence | Validation day: first fill / trades per hour / P&L | Replay day: first fill / trades per hour / P&L |
| --- | --- | --- |
| 0.72 (old) | 0.6 s / 301 / +$146.64 | 134.6 s / 209 / +$220.47 |
| 0.60 | 0.0 s / 355 / +$171.24 | 108.0 s / 282 / +$267.29 |
| 0.50 | 0.0 s / 396 / +$167.77 | 11.8 s / 340 / +$308.60 |
| **0.40 (default)** | 0.0 s / 438 / **+$170.46** | 0.4 s / 393 / +$325.45 |
| 0.30 | 0.0 s / 482 / +$154.30 | 0.0 s / 445 / +$335.62 |
| 0.10 | 0.0 s / 559 / +$119.74 | 0.0 s / 550 / +$255.77 |

Lower is faster and busier; below about 0.3 each trade is too weak and profit falls. A wider order cap (up to 20 ticks) did not change the first fill.
More trades also means more fees: this table assumes the Solana fee only. Under `FEE_MODEL=exchange` the busier setting loses faster.
