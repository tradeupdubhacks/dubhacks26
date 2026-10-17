import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import websocket from '@fastify/websocket'
import Fastify, { type FastifyInstance } from 'fastify'
import type { Chain } from './chain.js'
import { Market, instrumentsBody, type Failure } from './market.js'

interface Socket {
  OPEN: number
  readyState: number
  bufferedAmount: number
  send(data: string): void
  close(): void
  on(event: 'message', listener: (raw: Buffer) => void): void
  on(event: 'close', listener: () => void): void
}
import { loadHistory } from './history.js'
import { fetchSolMid } from './price.js'
import { schemas } from './schema.js'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

export interface App {
  app: FastifyInstance
  market: Market
  close: () => Promise<void>
}

export async function buildApp(options?: {
  tapeDir?: string
  logger?: boolean
  clock?: boolean
  chain?: Chain | null
  historyFetch?: typeof fetch
}): Promise<App> {
  const market = new Market(options?.tapeDir ?? join(root, 'hft', 'data', 'raw'), options?.chain ?? null)
  const app = Fastify({ logger: options?.logger ?? false })
  const watchers = new Set<Socket>()
  const streams = new Set<Socket>()
  let clock: ReturnType<typeof setInterval> | undefined
  let feed: ReturnType<typeof setInterval> | undefined
  let sawHalt = false

  function send(socket: Socket, payload: string): void {
    if (socket.readyState !== socket.OPEN) return
    if (socket.bufferedAmount > 1_000_000) {
      socket.close()
      return
    }
    socket.send(payload)
  }

  function publish(): void {
    const session = market.sessionView()
    const halted = 'status' in session && session.status === 'halted'
    const type = halted && !sawHalt ? 'halt' : 'snapshot'
    sawHalt = halted
    if (!('status' in session) || session.status === 'idle') sawHalt = false
    const watchPayload = JSON.stringify({ type, session })
    for (const socket of watchers) send(socket, watchPayload)
    if (market.liveOpen) {
      const streamPayload = JSON.stringify({ type: 'snapshot', observation: market.observation() })
      for (const socket of streams) send(socket, streamPayload)
    }
  }

  async function pullPrice(): Promise<void> {
    const price = await fetchSolMid()
    if (!market.needsFeed) return
    const events = price ? await market.setReferenceMid(price.cents, price.source) : []
    if (!price) await market.markStale()
    if (events.length > 0) {
      const payload = JSON.stringify({ type: 'events', events, observation: market.observation() })
      for (const socket of streams) send(socket, payload)
    }
    publish()
  }

  function syncLoops(): void {
    if (clock) clearInterval(clock)
    if (feed) clearInterval(feed)
    clock = undefined
    feed = undefined
    if (options?.clock === false) return
    if (market.liveOpen) {
      clock = setInterval(() => {
        void market.tick(200).then((events) => {
          if (events.length > 0) {
            const payload = JSON.stringify({ type: 'events', events, observation: market.observation() })
            for (const socket of streams) send(socket, payload)
          }
          publish()
        })
      }, 200)
    }
    if (market.needsFeed) {
      feed = setInterval(() => {
        void pullPrice()
      }, 1_000)
      void pullPrice()
    }
  }

  app.removeAllContentTypeParsers()
  app.addContentTypeParser('application/json', { parseAs: 'string' }, (_request, body, done) => {
    const text = typeof body === 'string' ? body : ''
    if (text.trim() === '') {
      done(null, {})
      return
    }
    try {
      const parsed: unknown = JSON.parse(text)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        done(badJson(), undefined)
        return
      }
      done(null, parsed)
    } catch {
      done(badJson(), undefined)
    }
  })
  app.setErrorHandler((error, _request, reply) => {
    const code = typeof error === 'object' && error && 'code' in error ? String(error.code) : ''
    if (code === 'invalid_json') {
      return reply.code(400).send({ error: { code: 'invalid_json', message: 'Body is not a JSON object.' } })
    }
    app.log.error(error)
    return reply.code(500).send({ error: { code: 'internal', message: 'Internal error.' } })
  })

  await app.register(websocket)

  app.get('/', async (_request, reply) => {
    const html = await readFile(join(root, 'frontend', 'index.html'))
    return reply.type('text/html').send(html)
  })

  app.get('/v1/health', async () => ({
    ok: true,
    mode: 'localhost',
    execution: options?.chain ? 'solana' : 'fake',
    ...(options?.chain ? { chain: options.chain.snapshot() } : {}),
  }))
  app.get('/v1/instruments', async () => instrumentsBody())
  app.get('/v1/history', async (request, reply) => {
    const query = isRecord(request.query) ? request.query : {}
    const result = await loadHistory(
      {
        symbol: stringQuery(query.symbol),
        interval: stringQuery(query.interval),
        limit: stringQuery(query.limit),
      },
      options?.historyFetch,
    )
    return reply.code(result.status).send(result.body)
  })
  app.get('/v1/schema', async () => schemas)
  app.get('/v1/book', async () => {
    const view = market.observation()
    return {
      market: view.market,
      ts: view.ts,
      mid: view.mid,
      priceSource: view.priceSource,
      bids: view.bids,
      asks: view.asks,
      lastTrade: view.lastTrade,
    }
  })
  app.get('/v1/account', async () => market.observation())
  app.get('/v1/session', async () => market.sessionView())

  app.post('/v1/session', async (request, reply) => {
    const result = await market.openSession(bodyOf(request.body))
    if (!result.ok) return sendFailure(reply, result)
    syncLoops()
    publish()
    return reply.code(201).send(result.session)
  })

  app.post('/v1/stop', async (request, reply) => {
    const result = await market.stop()
    if (!result.ok) return sendFailure(reply, result)
    publish()
    return result.session
  })

  app.post('/v1/reset', async (request, reply) => {
    const result = await market.reset(bodyOf(request.body))
    if (!result.ok) return sendFailure(reply, result)
    syncLoops()
    return { observation: result.observation, done: result.done }
  })

  app.post('/v1/step', async (request, reply) => {
    const result = await market.step(bodyOf(request.body))
    if (!result.ok) return sendFailure(reply, result)
    publish()
    const payload = JSON.stringify({ type: 'events', events: result.events, observation: result.observation })
    for (const socket of streams) send(socket, payload)
    return { observation: result.observation, reward: result.reward, done: result.done, events: result.events }
  })

  app.post('/v1/orders', async (request, reply) => {
    const result = await market.place(bodyOf(request.body))
    if (!result.ok) return sendFailure(reply, result)
    publish()
    return { events: result.events, observation: result.observation }
  })

  app.delete('/v1/orders/:clientOrderId', async (request, reply) => {
    const { clientOrderId } = request.params as { clientOrderId: string }
    const result = await market.cancel(clientOrderId)
    if (!result.ok) return sendFailure(reply, result)
    publish()
    return { events: result.events, observation: result.observation }
  })

  app.post('/v1/orders/cancel-all', async (request, reply) => {
    const result = await market.cancelAll()
    if (!result.ok) return sendFailure(reply, result)
    publish()
    return { events: result.events, canceled: result.canceled, observation: result.observation }
  })

  app.post('/v1/flatten', async (request, reply) => {
    const result = await market.flatten()
    if (!result.ok) return sendFailure(reply, result)
    publish()
    const payload = JSON.stringify({ type: 'events', events: result.events, observation: result.observation })
    for (const socket of streams) send(socket, payload)
    return { events: result.events, observation: result.observation }
  })

  app.get('/v1/watch', { websocket: true }, (socket: Socket) => {
    watchers.add(socket)
    send(socket, JSON.stringify({ type: 'snapshot', session: market.sessionView() }))
    socket.on('message', (raw: Buffer) => {
      const message = parseMessage(raw)
      if (!message || message.op !== 'ping' || Object.keys(message).length !== 1) {
        send(
          socket,
          JSON.stringify({
            type: 'error',
            error: {
              code: 'watch_is_read_only',
              message: 'The watch socket cannot trade or stop a session. Use POST /v1/stop to stop.',
            },
          }),
        )
        return
      }
      send(socket, JSON.stringify({ type: 'pong' }))
    })
    socket.on('close', () => watchers.delete(socket))
  })

  app.get('/v1/stream', { websocket: true }, (socket: Socket) => {
    streams.add(socket)
    send(socket, JSON.stringify({ type: 'snapshot', observation: market.observation() }))
    socket.on('message', (raw: Buffer) => {
      void handleStream(socket, raw)
    })
    socket.on('close', () => streams.delete(socket))
  })

  async function replyCommand(
    socket: Socket,
    result: { ok: true; events: unknown[]; observation: unknown } | Failure,
  ): Promise<void> {
    if (result.ok) {
      send(socket, JSON.stringify({ type: 'events', events: result.events, observation: result.observation }))
    } else if (result.reason) {
      send(
        socket,
        JSON.stringify({
          type: 'events',
          events: [
            {
              type: 'reject',
              clientOrderId: result.clientOrderId ?? '',
              reason: result.reason,
              duplicate: result.duplicate ?? false,
            },
          ],
          observation: market.observation(),
        }),
      )
    } else {
      send(socket, JSON.stringify({ type: 'error', error: { code: result.code, message: result.message } }))
    }
    publish()
  }

  async function handleStream(socket: Socket, raw: Buffer): Promise<void> {
    const message = parseMessage(raw)
    if (!message || typeof message.op !== 'string') {
      send(socket, JSON.stringify({ type: 'error', error: { code: 'invalid_message', message: 'Unrecognized stream command.' } }))
      return
    }
    if (message.op === 'ping') {
      send(socket, JSON.stringify({ type: 'pong' }))
      return
    }
    if (message.op === 'place') {
      if (!isRecord(message.order)) {
        send(socket, JSON.stringify({ type: 'error', error: { code: 'invalid_message', message: 'place requires an order object.' } }))
        return
      }
      await replyCommand(socket, await market.place(message.order))
      return
    }
    if (message.op === 'cancel' && typeof message.clientOrderId === 'string') {
      await replyCommand(socket, await market.cancel(message.clientOrderId))
      return
    }
    if (message.op === 'cancel_all') {
      await replyCommand(socket, await market.cancelAll())
      return
    }
    if (message.op === 'flatten') {
      await replyCommand(socket, await market.flatten())
      return
    }
    send(socket, JSON.stringify({ type: 'error', error: { code: 'invalid_message', message: 'Unrecognized stream command.' } }))
  }

  app.addHook('onClose', async () => {
    if (clock) clearInterval(clock)
    if (feed) clearInterval(feed)
    await market.close()
    await options?.chain?.close()
  })

  return {
    app,
    market,
    async close() {
      await app.close()
    },
  }
}


function sendFailure(reply: { code: (status: number) => { send: (body: unknown) => unknown } }, result: Failure) {
  return reply.code(result.status).send({
    error: {
      code: result.code,
      message: result.message,
      ...(result.clientOrderId ? { clientOrderId: result.clientOrderId } : {}),
      ...(result.reason ? { reason: result.reason } : {}),
      ...(result.duplicate ? { duplicate: true } : {}),
    },
  })
}

function bodyOf(body: unknown): Record<string, unknown> {
  return isRecord(body) ? body : {}
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function stringQuery(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function parseMessage(raw: Buffer | ArrayBuffer | Buffer[]): Record<string, unknown> | null {
  try {
    const text = Array.isArray(raw) ? Buffer.concat(raw).toString() : raw.toString()
    const parsed: unknown = JSON.parse(text)
    return isRecord(parsed) ? parsed : null
  } catch {
    return null
  }
}

function badJson(): Error {
  const error = new Error('Body is not a JSON object.')
  ;(error as Error & { code: string; statusCode: number }).code = 'invalid_json'
  ;(error as Error & { statusCode: number }).statusCode = 400
  return error
}
