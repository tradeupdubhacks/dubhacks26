# TradeUp: pay less by letting a model trade for you

## Inspiration
Buy-now-pay-later lets you split a price over time. We asked a different question: what if part of your payment could *work* while you shop? At checkout a customer can **activate a discount**: put a slice of the order into a short, rail-protected automated trading session. If the model grows the stake to the customer's goal, they pay less; if it falls to their loss limit, trading stops automatically. We wanted to build the whole loop (market, ledger, models, storefront) and find out honestly whether a model can carry it.

## What it does
- A storefront where the customer picks a coin, an amount, a **goal** and a **loss limit**, then starts a live session.
- A simulated exchange follows the **live Binance Bitcoin order book** (or replays a recorded day) and a live Solana price. A session's stake is the deposit plus the pool's profit or loss, and it **halts itself at the floor or ceiling**.
- A **Trading model** dropdown on the page: a small neural network, a **transformer** over the last 30 seconds of order-book ticks, the team's forecasting network, an order-book HFT rule, or none. The server launches the chosen bot and stops it when the session ends.
- Fills settle as token transfers on a local Solana validator; each fill pays a ~0.1 cent network fee and no exchange fee.

## How we built it
- **Server:** TypeScript/Fastify, an in-memory matching engine with a 50 ms order delay, floor/ceiling halts, a live Binance `BTCUSDT` feed (or a recorded tape), and a bot launcher (`src/bot.ts`).
- **Page:** a static single-page app talking to the client API and a read-only watch socket.
- **Models:** PyTorch. One shared feature function builds inputs for training *and* live use, and a test proves the two paths give identical numbers.
- **Backtester:** a replica of the server's fill rules, checked against the real server (same positions on every step, equity within 4 cents over 4.8 hours).
- **Research:** about 20 million one-minute candles for six coins (2020 to 2026), ten days of order-book data and open-interest/positioning data, all from Binance's public archive.

## Challenges we ran into
- **The edge is tiny.** A 5-second forecast is worth about 0.6 bps per trade; a normal exchange fee costs 4 to 10 bps. With 2/5 bps fees the transformer loses nearly everything.
- **Fooling ourselves.** We tested about 130 ideas on longer horizons. Several looked good in one year or on one coin; the most promising (positioning data) **failed to replicate** on five other coins, so we reported it as noise.
- **Reproducibility.** Live sessions follow the real market, so no two runs match (a recorded day is available for repeatable tests), and the recording's first minutes are quiet. A strict entry rule meant a two-minute wait for the first trade; we measured the trade-off and lowered the entry bar, so it now trades within seconds.
- **Merging four people's work:** two fee models, two `SignalNet` classes, a rewritten bot loop, and stray test servers on the same port.

## Accomplishments we're proud of
- A working path from a click on the page to a live bot trading a protected stake, with tests (60 Python, 28 server).
- On a recorded day the transformer never saw, it made **+$219.72 on a $100 deposit** (after the Solana fee), profitable in all 23 hours with a $1.77 worst dip. A first test on the live Binance book (dummy money, 6 minutes, a $20 stake) ended **+$0.72**; a very small sample.
- A research record that includes the negative results, with out-of-sample tests, held-out days and replication checks.

## What we learned
- Fees, not model quality, decide whether a short-horizon strategy works. The same model earns +$219.72 or loses ~$100 depending on the fee schedule.
- Better architectures (GRU, TCN, boosted trees, a transformer) all land near the same accuracy; new *information* matters more than new model types.
- Train/serve parity tests and an honest simulator are worth more than another percentage point of accuracy.

## What's next
- Make the simulator harsher: queue position, market impact, delayed exits, and real exchange fees.
- Replay more than one day, and run against live order-book data.
- Maker-only execution or a low-fee venue, since the edge only pays at roughly 0.25 bps per side.
- A real Solana order book so Solana sessions can have their own model.
- Customer protection and legal review before any real money touches a stake.
