# TradeUp: pay less by letting a model trade for you

## Inspiration
Buy-now-pay-later lets you split a price over time. We asked a different question: what if part of your payment could *work* while you shop? At checkout a customer can **activate a discount**: put a slice of the order into a short, rail-protected automated trading session. If the model grows the stake to the customer's goal, they pay less; if it falls to their loss limit, trading stops automatically. We wanted to build the whole loop (market, ledger, models, storefront) and find out honestly whether a model can carry it.

## What it does
- A storefront where the customer picks a trading model, an amount ($1 to $100), a **goal** and a **loss limit**, then starts a live Bitcoin session.
- Four models to choose from: **Faster** (an order-book rule bot, high risk), **Slower** (the team's neural network gated by the order book, low risk), **Transformer** (medium risk), and **Small network** (rarely trades).
- A simulated exchange that follows the **live Binance BTCUSDT order book** (best bid/ask, sizes and trades over a websocket), or a recorded day for reproducible runs. A session's stake is the deposit plus the pool's profit or loss (pooled with a $100,000 house fund), and it **halts itself at the floor or ceiling**.
- **Several people can trade at once** (up to 20 sessions), each in an isolated session with its own stake, rails and model. The server starts each model's bot when the first session needs it and stops it when the last one ends.
- Fills settle as SPL token transfers on a local Solana validator. Each fill is charged its Solana network fee (two signatures, about 0.1 cent) and no exchange fee; a switch turns on Phoenix-style 2/5 bps exchange fees.

## How we built it
- **Server:** TypeScript/Fastify, an in-memory matching engine with a 50 ms order delay and floor/ceiling halts, one market per live session, a live Binance feed with a 2-minute price history for warm-up, a tape replay of Binance `BTCUSDT`, and a per-model bot launcher (`src/bot.ts`).
- **Settlement:** a local Solana validator. Every fill is a real transaction on it (dummy tokens), and the stake is charged the network fee of that transaction.
- **Page:** a static single-page app, served by the server, talking to the client API and a read-only watch socket.
- **Models:** PyTorch and plain Python. Two neural networks (the team's return forecaster, and a 929-parameter goal-seeking network), one transformer over 30 seconds of order-book ticks, and one rule-based order-book bot.
- **Backtester:** a replica of the server's fill rules, checked against the real server (same positions on every step, equity within 4 cents over 4.8 hours). We also ran parameter sweeps through the real server on 26 windows of recorded data.
- **Research:** about 21 million one-minute candles for six coins (BTC, ETH, SOL, BNB, XRP, DOGE, 2020 to 2026), ten days of Bitcoin top-of-book data and positioning data, all from Binance's public archive.

## Challenges we ran into
- **The edge is tiny.** A 5-second forecast is worth about 0.6 bps per trade; a normal exchange fee costs 4 to 10 bps. With 2/5 bps fees, the transformer and the rule bot lose nearly everything.
- **Fooling ourselves.** We tested many ideas on longer horizons. Several looked good in one year or on one coin; the most promising (positioning data) **failed to replicate** on five other coins, so we reported it as noise. A controlled test also showed the neural network adding little over the order-book signal alone: it mostly cuts drawdowns rather than adding profit.
- **Live data:** Binance's REST API is region-blocked from our machine, so the live feed runs on the public websocket, and a new server needs about 2 minutes of live data before the slower model can trade.
- **Merging four people's work:** four bots, a model launcher designed for one session at a time, a force-pushed branch, and stray test servers on the same port, all reconciled into one multi-user app.
- **Hosting:** deployed on a Vultr server.

## Accomplishments we're proud of
- A working path from a click on the page to a live bot trading a protected stake, for several people at once, with tests (74 Python, 39 server).
- On the replayed day, which the transformer never saw, it made **+$219.72 on a $100 deposit** after the Solana fee.
- In 10-minute simulations on 26 windows of recorded Bitcoin data, the Faster bot reached +$5 on a $100 stake in all 26 (median 1.5 minutes). The Slower model reached it within 20 minutes in all 26 (median 2.3 minutes). Live, they hit +$5.32 in 2 min 38 s and +$5.68 in 5 min 55 s.
- A research record that includes the negative results, with out-of-sample tests, held-out days and replication checks.

## What we learned
- Fees, not model quality, decide whether a short-horizon strategy works. The same transformer earns +$219.72 or loses about $100 depending on the fee schedule, and the rule bot goes from +$5 in every window to hitting its floor.
- Better architectures (GRU, LSTM, TCN, boosted trees, a transformer) all land near the same accuracy; new *information* and *time* matter more than new model types.
- Train/serve parity tests, controls (reversed and random strategies) and an honest simulator are worth more than another percentage point of accuracy.

## What's next
- Make the simulator harsher: queue position, market impact, delayed exits, and real exchange fees. Today each session fills at the displayed best price against its own copy of the book.
- Test on more days: our tuning used one recorded day plus one live hour.
- Maker-only execution or a low-fee venue, since the edge only pays at roughly 0.25 bps per side.
- A real Solana order book, so Solana sessions can have their own model; the Solana market today is a synthetic book.
- Customer protection and legal review before any real money touches a stake.
