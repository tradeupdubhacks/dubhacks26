# Can an ML crypto strategy fund a 10–20% purchase top-up? Empirical findings

**Short answer: No — not on the evidence available, and the evidence that exists points firmly against it.**
The models have no out-of-sample predictive skill, every ML strategy loses money after realistic costs, and the
dataset cannot support the multi-day/multi-asset claims the business case needs.

## 0. What the data can and cannot support (read first)

| Requested | Available | Consequence |
|---|---|---|
| Multi-cryptocurrency | **BTCUSDT perpetual only** (Binance futures) | No cross-asset results. |
| 7/30/60/90-day horizons | **One calendar day** (2026-10-01, 00:00–23:59 UTC), 86,400 s | Cannot measure any 7–90 day outcome empirically. |
| Market regimes | One day, range 83,141–85,241 (2.55%), realised vol ≈ 31% annualised | Single regime; no regime analysis possible. |
| Time-to-target statistics | Max OOS horizon = 12 h | Empirical time-to-target only for ≤12 h. |

Raw data: 3.25 M trades, 35.4 M top-of-book quotes, funding/OI/mark/index ticker. Aggregated to 1 s bars (`bars_1s.csv.gz`),
modelled on 5 s bars. Any multi-day number below that is not labelled *empirical* is a **scenario/sensitivity result under
stated assumptions**, not an estimate from this data.

## 1. Method (what was actually run)

- **Features (15)**: 5 s mid return, range, trade-flow imbalance, signed volume, log volume/trades/quote count, book imbalance,
  micro-price deviation, spread, mark–index basis, ΔOI, rolling vol, 1- and 5-min momentum. Standardised with **train-fold statistics only**, clipped ±5.
- **Label**: mid-price return over the next 60 s, **entered 5 s after the decision** (latency), so features never see the label window.
- **Models**: GRU(48), LSTM(48), TCN (4 dilated residual blocks); baselines: ridge, gradient boosting, 12-bar momentum, 12-bar mean reversion,
  book-imbalance rule, random, buy-and-hold. 60-bar (5 min) lookback.
- **Walk-forward (expanding)**: 3 folds, test windows 12–16 h, 16–20 h, 20–24 h; the preceding 2 h is validation; **embargo of 73 bars** between splits.
  Early stopping on validation loss. Total OOS = 12 h (8,627 samples, 719 non-overlapping 60 s decisions).
- **Signal**: trade only the top 20% most confident predictions, threshold fixed on *validation*; position ±100% of capital, no leverage, decisions every 60 s.
- **Costs per side**: taker fee 5 bps + 0.5 bp slippage (spread is 1 tick ≈ 0.012 bp, negligible vs fees). Also shown: optimistic 2 bps maker-fee case
  (assumes every order fills passively — unrealistic for a signal-chasing strategy) and zero cost (gross).
- CIs: block bootstrap (30-decision blocks, 2000 resamples).

## 2. Results — out-of-sample (empirical, 12 h)

**Predictive skill** (`results/predictive_skill.csv`): Spearman IC between prediction and realised 60 s return — GRU −0.003, LSTM −0.051, TCN −0.029, ridge −0.012, GBM +0.014;
directional accuracy 46.9–48.9% (below 50%). **No model shows skill.** Validation loss was minimised after 0–6 epochs, i.e. training only fitted noise.

**Net returns, taker costs** (`results/oos_backtest.csv`):

| Strategy | trades | win rate | mean net bps/decision [95% CI] | total net return (= max drawdown) |
|---|---|---|---|---|
| GRU | 113 | 28% | −1.03 [−1.60, −0.50] | −7.1% |
| LSTM | 140 | 30% | −1.29 [−1.89, −0.76] | −8.9% |
| TCN | 131 | 40% | −1.06 [−1.40, −0.68] | −7.4% |
| Ridge | 148 | 40% | −1.19 [−1.60, −0.81] | −8.2% |
| GBM | 181 | 37% | −1.31 [−1.83, −0.88] | −9.0% |
| Momentum | 394 | 24% | −6.10 | −35.5% |
| Random | 353 | 26% | −5.63 | −33.3% |
| Buy & hold | 1 | – | +0.15 [−0.17, +0.50] | +1.1% (just the day's drift) |

- **Gross (zero cost)**, all ML strategies' CIs include 0 (e.g. GBM +0.15 bps [−0.17, +0.44]; GRU −0.13 [−0.32, +0.04]). There is no gross edge to lose to fees.
- **Optimistic 2 bps maker costs**: still negative for all ML models (−2.7% to −4.0% over 12 h).
- **Best any strategy reached: +1.1% (buy-and-hold). Nothing came within an order of magnitude of +10%.** Empirical probability of reaching 10/15/20% in 12 h: 0 of the 5 ML strategies (one path each — not a statistically meaningful probability).
- Sharpe/Sortino are in the CSV but **should not be read**: annualising 12 h of 60 s returns gives values like −200 that are arithmetic artefacts of a tiny sample, not information.
- Cost drag is the story: the ML models trade rarely (≈113–181 position changes in 12 h) yet each round trip costs ~11 bps against a typical 60 s move that the model cannot predict.

Figures: `results/oos_equity.png`, `results/training_curves.png`.

## 3. First-passage / customer simulations (**scenario analysis, not empirical**)

Daily wealth paths, Student-t(4) returns, 20,000 paths each, target = first time W ≥ P, capital-preservation stop at −15%,
horizon T days (`results/first_passage_scenarios.csv`, `results/first_passage.png`). First-passage ≠ end-above-target; both are in the CSV.

Probability of **first reaching** the target by day T (stop at −15%; the 95% CI half-width from simulation noise is ≤0.7 pts, but model-assumption uncertainty is far larger):

| Scenario (assumed) | +10% / 30 d | +10% / 90 d | +20% / 30 d | +20% / 90 d | P(end < deposit), +20%/90 d | P(stop-out) +20%/90 d |
|---|---|---|---|---|---|---|
| **ML as measured here (net)** | 0% | 0% | 0% | 0% | 100% | 100% |
| Hypothetical net Sharpe 0, 25% vol | 13.6% | 37.6% | 0.8% | 10.6% | 52% | 18% |
| Hypothetical net Sharpe 1 | 19.7% | 55.0% | 1.6% | 22.7% | 33% | 9% |
| Hypothetical net Sharpe 2 | 27.9% | 71.9% | 2.9% | 39.1% | 18% | 4% |
| Hypothetical net Sharpe 3 | 37.9% | 85.6% | 5.2% | 57.2% | 8% | 2% |
| BTC long, zero drift, 45% vol | 35.6% | 54.4% | 10.6% | 32.2% | 55% | 44% |

("ML as measured" extrapolates the 12 h loss rate — an illustration that a strategy with no gross edge and 5.5 bps/side costs ratchets toward the stop, not a forecast.)

Take-aways that hold regardless of data limitations:

1. **A driftless strategy reaches +10% within 90 days ~38–54% of the time purely from variance — and ends below the deposit ~50% of the time.** "Probability of touching the target" is
   a misleading success metric on its own; reaching +20% within 30 days needs a net Sharpe ≫ 3 (5.2% at Sharpe 3, 14% at Sharpe 5).
2. Even with a *genuinely excellent* sustained net Sharpe of 2, there's a ~72% chance of not reaching +10% in 30 d and a ~61% chance of not reaching +20% in 90 d.
3. Real strategies with Sharpe >2 net of costs and capacity for retail-size accounts are rare and decay; **this data gives zero evidence that the models here are one**.

### Customer journeys (`results/customer_journeys.csv`, 30-day horizon)

Two financing structures are separated from trading P&L:

- **Deferred purchase** (buy only once W ≥ P): the platform takes no credit risk; the customer bears trading losses; the product is bought only in the lucky case.
- **Advance (BNPL-style)**: platform pays P − C₀ up front; if W < P at the end, the platform's shortfall is P − W (no customer recourse assumed).

| Journey (P / C₀, R) | Net-Sharpe-1 strategy: P(funded) | E[platform shortfall] / advance | BTC zero-drift 45% vol: P(funded) | E[platform shortfall] / advance |
|---|---|---|---|---|
| $600 / $500, 20% | 1.7% | 90% | 10.7% | 102% |
| $1,100 / $1,000, 10% | 19.4% | 84% | 35.5% | 107% |
| $1,200 / $1,000, 20% | 1.6% | 90% | 10.8% | 101% |

Under the advance structure the expected shortfall is roughly the **whole advance** even in a Sharpe-1 world; the platform is effectively lending unsecured on an
essentially coin-flip outcome. Under the *measured* ML strategy the expected shortfall is 1.9–2.9× the advance because the deposit itself is also eroded. Mitigations
(customer recourse, guarantee fund, haircuts) transfer risk; they don't create returns, and customer-capital-loss exposure raises regulatory questions (investment-product/
securities, lending and consumer-credit rules, custody) that need legal review separate from this study.

## 4. Limitations

- **One day, one asset.** The most important caveat. Results about 60 s prediction on a 2.5%-range day say nothing definitive about other days/regimes; they are, however, the *wrong
  kind of evidence for* a 7–90 day purchase product, which needs multi-month, multi-regime data.
- 12 h OOS and ~8.6 k overlapping samples is small; deep nets have limited training data (≈7–10 k windows per fold). A larger dataset might reveal weak signal that is absent here — but weak microstructure
  signals are usually far smaller than 11 bps round-trip costs.
- Backtest fills assume mid-price entry ± fixed cost, no market impact (fine at <$10 k per account) and no queue/maker modelling. Funding payments ignored (funding ≈ 0.005%/8h, negligible here).
- Scenario simulations use i.i.d. t-distributed daily returns with volatility and Sharpe **assumed**; they ignore vol clustering, jumps and regime shifts.
- Hyper-parameters were not tuned (and tuning on 12 h would overfit anyway). Backtest-overfitting risk grows with any additional search.

## 5. Answer to the core question and what would have to change

**With the attached data: no.** No model had predictive skill; all strategies were net-negative vs. 5.5 bps/side costs; none approached +10%; and the data cannot show reliability over useful horizons.

Even setting this data aside, the structure has a basic problem: the target return (10–20% in ≤90 days, near-certain) implies an annualised net Sharpe in the high single digits — not sustainable with any ML on public crypto data. Fixed
targets with a loss-bearing customer or platform are a first-passage gamble, not a savings product.

What would need to change:
1. **Data**: ≥2–3 years of multi-asset (BTC, ETH, majors, ideally perps+spot) tick/minute data across bull/bear/chop regimes, with purged/embargoed walk-forward and pre-registered evaluation so the 7–90 d claims can be tested.
2. **Edge/costs**: demonstrate a gross edge well above round-trip costs (maker-side execution with measured fill/adverse-selection, lower fee tier), and validate on live paper trading before any customer money.
3. **Business structure**: do not promise a purchase outcome. Safer shapes: customer keeps their money and gets the product on a normal timeline (deposit held in a segregated, low-risk account with any yield used as a *discount*, not a requirement); or deferred purchase with a
   guaranteed principal (platform-funded, priced as an option, with the cost shown above); or a traditional BNPL credit product underwritten on customer credit risk rather than trading outcomes.
4. **Legal/regulatory** review of investing customer deposits (licensing, disclosure, client-money segregation) before any pilot.

## Reproduce

```
cd hft/research
python3 01_build_bars.py      # raw tick .csv.gz -> bars_1s.csv.gz (~2 min)
python3 02_train_eval.py      # walk-forward training + OOS backtest -> models/, results/ (~4 min, CPU)
python3 03_customer_sim.py    # first-passage + customer-journey scenarios -> results/
```
Checkpoints: `models/{gru,lstm,tcn}_fold{0,1,2}.pt` (one per walk-forward fold). Seeds fixed; Apple-Accelerate matmul warnings during sklearn fits are benign.

## 6. Follow-up: seconds-scale, volatility-gated variant (`04_short_horizon.py`)

Changes: 1 s bars, 30 s lookback, **5 s hold** (1 s latency), trade only when trailing-30 s volatility is in the top 20% (cut-off from train data only), per-fold |prediction| threshold chosen on validation to maximise *net* PnL at the given cost (no trading if none is profitable). Same walk-forward folds/embargo; 12 h OOS.

- **Real signal exists at this horizon**: Spearman IC ≈ 0.17 (GRU/LSTM/TCN) and 0.145 (GBM) vs the 5 s return; lower inside volatile periods (≈0.07–0.08). This contrasts with the 60 s result (IC ≈ 0).
- **It is far too small to pay fees**: mean |5 s move| is 0.94 bps overall, 1.55 bps when volatile. Gross edge ≈ +0.2 bps/trade vs 5.5 bps/side taker (≈11 bps round trip) or 2 bps/side maker. Break-even cost ≈ 0.1–0.2 bps/side.
- Net result: with taker or maker fees the validation-calibrated rule **chooses not to trade** (0 trades, 0% return); at 0.5 bps/side the best case is ≈ −0.35%; only at **zero cost** is it positive (+3.0–3.3% over 12 h, ≈1,600 trades, win rate ≈50%).
- Conclusion: the short horizon fixes prediction, not economics. Making money here would need cost ≲ 0.1 bps/side — i.e. exchange maker rebates with real queue priority, which a mid-price backtest like this cannot validate (adverse selection on passive fills typically erases such an edge).

## 7. Follow-up with much more data (Binance public archive, `hft/download_extra.py`)

**Data added** (all SHA-256 verified): 1-minute USDS-M klines for BTC, ETH, SOL, BNB, XRP, DOGE, Jan 2020 – Sep 2026 (20.9 M bars; taker-buy volume included), and 10 full days of BTCUSDT top-of-book (`bookTicker`, one per month, Jun 2023 – Mar 2024). Tardis (the source of the original sample) returned 403 from this machine, and top-of-book is not published after Apr 2024.

**7a. Multi-asset, multi-year signal test** (`09_kline_signal.py`, walk-forward by year 2022–2026, pooled across 6 coins, features from past price, volume and taker flow):

| Horizon | Rank corr. with return | Gross bps / trade, top-20% confidence | Net, taker (11 bps round trip) | Net, maker (4 bps) |
|---|---|---|---|---|
| 1 h | 0.053 | +0.6 | -10.4 | -3.4 |
| 4 h | 0.050 | -0.4 | -11.4 | -4.4 |
| 24 h | 0.038 | -10.5 | -21.5 | -14.5 |

The 1 h correlation is positive in all 30 asset-years but fades over time (about 0.09 in 2023, 0.03-0.05 in 2025-26), and by 2025-26 the top-confidence gross edge is zero or negative in most coins. Coins move together, so asset-years are not independent.

**7b. Goal sessions, 10,398 day-sessions per cell** (`10_kline_sessions.py`: $100 deposit, start 00:00 UTC, <= 24 h, pool cap $840 notional, 5/2 bps fees, bracket trades sized to the rails):

| Goal / floor | Take-profit | P(goal) model | P(goal) random dir. | Mean stake change (model) | Ends near floor |
|---|---|---|---|---|---|
| +1% / -2% | 80 bps | 57.8% | 56.5% | -0.18% | 42% at about -1.8% |
| +1% / -2% | 40 bps | 55.7% | 53.1% | -0.31% | 44% at about -2.0% |
| +5% / -5% | 80 bps | 39.8% | 39.1% | -0.61% | 60% at about -4.3% |

Model minus random, day-block bootstrap: +2.7 pts P(goal) at 40 bps (95% CI +1.1 to +4.3), +1.2 pts at 80 bps (CI -0.3 to +2.9). Almost all of the hit rate comes from the bracket and sizing mechanics (a roughly 2:1 payoff against a floor), not from the signal. The floor is never crossed (sizing prevents it); losing sessions run out of room and stop near the floor. Expected value is negative in every cell.

**7c. Does the 5-second book signal hold on other days?** (`12_book_multiday.py`, 9 book-only features): yes. Trained on 2026-10-01 and tested on the 10 unseen 2023-24 days, rank correlation is 0.07-0.28 and positive on all 10 (mean 0.18); leave-one-day-out over all 11 days is the same (mean 0.18, positive on 11 of 11). Gross edge is +0.22 bps per 5 s decision (all), +0.45 bps (top 10% confidence), +0.54 bps (top 1%). It does not grow with volatility. Net after costs for non-overlapping 5 s trades (top 10%): about -10.5 bps with taker fees and -3.5 bps with maker fees, negative on every day. Break-even cost is about 0.25 bps per side.

**Bottom line with the extra data:** the earlier one-day result was not a fluke of that day. A small real edge exists at 5 s (stable across 11 days) and at 1 h (decaying), but it is 10-40 times too small to pay 2-5 bps per side, and the product's +1%/+5% goal behaves like a roughly 55/45 gamble with negative expected value.
