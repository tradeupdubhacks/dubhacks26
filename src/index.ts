import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildApp } from './app.js'
import { BOT_MODELS, BotRunner, defaultBotCommand } from './bot.js'
import { MAKER_BPS, TAKER_BPS } from './types.js'
import { LocalSolana } from './chain.js'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const port = Number(process.env.PORT ?? 3000)
const host = process.env.HOST ?? '0.0.0.0'

const chain = process.env.NO_CHAIN
  ? null
  : new LocalSolana(process.env.SOLANA_RPC ?? 'http://127.0.0.1:8899', join(root, '.local-solana'))

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
      selected: process.env.MODEL_BOT_KIND ?? 'small',
      cwd: root,
      url: `http://127.0.0.1:${port}`,
      env: { MAKER_BPS: String(MAKER_BPS), TAKER_BPS: String(TAKER_BPS) },
      log: (message) => console.log(message),
    })

const { app } = await buildApp({ logger: true, chain, bot })
await app.listen({ port, host })
