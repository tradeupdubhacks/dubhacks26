import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildApp } from './app.js'
import { LocalSolana } from './chain.js'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const port = Number(process.env.PORT ?? 3000)
const host = process.env.HOST ?? '0.0.0.0'
const chain = new LocalSolana(process.env.SOLANA_RPC ?? 'http://127.0.0.1:8899', join(root, '.local-solana'))

try {
  await chain.start()
} catch (error) {
  console.error(error instanceof Error ? error.message : error)
}

const { app } = await buildApp({ logger: true, chain })
await app.listen({ port, host })
