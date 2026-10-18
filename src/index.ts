import { appendFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildApp } from './app.js'
import { LocalSolana, type Chain } from './chain.js'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const port = Number(process.env.PORT ?? 3000)
const host = process.env.HOST ?? '0.0.0.0'
const local = new LocalSolana(process.env.SOLANA_RPC ?? 'http://127.0.0.1:8899', join(root, '.local-solana'))
let chain: Chain | null = local

try {
  await local.start()
} catch (error) {
  const reason = error instanceof Error ? error.message : String(error)
  console.error(reason)
  console.error('Continuing with in-memory fills. Live sessions will not settle on Solana.')
  // #region agent log
  appendFileSync(join(root, 'debug-706954.log'), JSON.stringify({sessionId:'706954',hypothesisId:'E',location:'index.ts:fallback',message:'chain start failed, using in-memory fills',data:{reason},timestamp:Date.now()})+'\n')
  // #endregion
  await local.close()
  chain = null
}

const { app } = await buildApp({ logger: true, chain })
await app.listen({ port, host })
