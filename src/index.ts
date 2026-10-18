import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildApp } from './app.js'
import { BinanceBtcFeed } from './binance.js'
import { BOT_MODELS, BotRunner, defaultBotCommand } from './bot.js'
import { LocalSolana, type Chain } from './chain.js'
import { FEE_MODELS, type FeeModel } from './types.js'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const port = Number(process.env.PORT ?? 3000)
const host = process.env.HOST ?? '0.0.0.0'
// Feature flag: FEE_MODEL=solana (default) charges no market fee, only each fill's Solana network fee (about 0.1 cent);
// FEE_MODEL=exchange charges the maker/taker basis points (2 / 5).
const feeModel = (process.env.FEE_MODEL ?? 'solana').trim().toLowerCase() as FeeModel
if (!FEE_MODELS.includes(feeModel)) {
  console.error(`Unknown FEE_MODEL ${JSON.stringify(process.env.FEE_MODEL)}. Use one of: ${FEE_MODELS.join(', ')}.`)
  process.exit(1)
}

// Feature flag: BTC_FEED=live (default) gives live BTC-USDT sessions the live Binance book.
// BTC_FEED=tape replays the recorded 2026-10-01 tape instead. Training always uses the tape.
const btcFeedMode = (process.env.BTC_FEED ?? 'live').trim().toLowerCase()
if (btcFeedMode !== 'live' && btcFeedMode !== 'tape') {
  console.error(`Unknown BTC_FEED ${JSON.stringify(process.env.BTC_FEED)}. Use one of: live, tape.`)
  process.exit(1)
}
const btcFeed = btcFeedMode === 'live' ? new BinanceBtcFeed() : null
btcFeed?.start()

const chain = process.env.USE_CHAIN
  ? new LocalSolana(process.env.SOLANA_RPC ?? 'http://127.0.0.1:8899', join(root, '.local-solana'))
  : null

if (chain) {
  try {
    await chain.start()
  } catch (error) {
    console.error(error instanceof Error ? error.message : error)
  }
}

// The page only opens and watches a session. This starts the model that trades it (python -m model.live session).
// MODEL_BOT=off runs the server without it, for a bot you start yourself.
const bot = process.env.MODEL_BOT === 'off'
  ? null
  : new BotRunner({
      command: defaultBotCommand(root),
      models: BOT_MODELS,
      selected: process.env.MODEL_BOT_KIND ?? 'transformer',
      cwd: root,
      url: `http://127.0.0.1:${port}`,
      env: { FEE_MODEL: feeModel },
      log: (message) => console.log(message),
    })

const { app } = await buildApp({ logger: true, chain, feeModel, btcFeed, bot })
app.addHook('onClose', async () => btcFeed?.shutdown())
console.log(`fee model: ${feeModel}, BTC feed: ${btcFeedMode}`)
// Stop the bot with the server on Ctrl-C or a plain kill; otherwise it would keep running with no server.
for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP'] as const) {
  process.on(signal, () => {
    bot?.stop()
    process.exit(0)
  })
}
process.on('exit', () => bot?.stop())
await app.listen({ port, host })
