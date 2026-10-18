import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildApp } from './app.js'
import { BinanceBtcFeed } from './binance.js'
import { LocalSolana, type Chain } from './chain.js'
import { FEE_MODELS, type FeeModel } from './types.js'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const port = Number(process.env.PORT ?? 3000)
const host = process.env.HOST ?? '0.0.0.0'
// Feature flag: FEE_MODEL=exchange (default) charges maker/taker basis points,
// FEE_MODEL=solana charges only each fill's Solana network fee.
const feeModel = (process.env.FEE_MODEL ?? 'exchange').trim().toLowerCase() as FeeModel
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

const { app } = await buildApp({ logger: true, chain, feeModel, btcFeed })
app.addHook('onClose', async () => btcFeed?.shutdown())
console.log(`fee model: ${feeModel}, BTC feed: ${btcFeedMode}`)
await app.listen({ port, host })
