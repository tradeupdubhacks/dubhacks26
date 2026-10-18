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
import type { LiveBtcSource } from './tape.js'
import type { FeeModel } from './types.js'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')

export interface App {
  app: FastifyInstance
  /** The latest live session's market, or the training market when there is none. */
  readonly market: Market
  close: () => Promise<void>
}

/** At most this many sessions trade at once. Each one borrows the house fund's leverage. */
export const MAX_SESSIONS = 20
/** A halted session stays readable this long, so its page can show the result, then it is dropped. */
const HALTED_TTL_MS = 10 * 60_000

/** One market and the sockets watching it. Every live session gets its own. */
interface Slot {
  id: string
  market: Market
  watchers: Set<Socket>
  streams: Set<Socket>
  sawHalt: boolean
  haltedAt: number | null
  release?: () => void
}

export async function buildApp(options?: {
  tapeDir?: string
  logger?: boolean
  clock?: boolean
  chain?: Chain | null
  historyFetch?: typeof fetch
  feeModel?: FeeModel
  btcFeed?: LiveBtcSource | null
}): Promise<App> {
  const tapeDir = options?.tapeDir ?? join(root, 'hft', 'data', 'raw')
  const feeModel = options?.feeModel ?? 'exchange'
  const newSlot = (id: string, live: boolean): Slot => {
    const feed = live && options?.btcFeed ? (options.btcFeed.view?.() ?? options.btcFeed) : null
    return {
      id,
      market: new Market(tapeDir, options?.chain ?? null, feeModel, feed),
      watchers: new Set(),
      streams: new Set(),
      sawHalt: false,
      haltedAt: null,
      release: feed?.release ? () => feed.release?.() : undefined,
    }
  }
  // Training (reset/step) has its own market. Live sessions never share it.
  const training = newSlot('training', false)
  const slots = new Map<string, Slot>()
  let current: Slot | null = null
  let sessionSeq = 0
  const app = Fastify({ logger: options?.logger ?? false })
  let clock: ReturnType<typeof setInterval> | undefined
  let feed: ReturnType<typeof setInterval> | undefined

  const running = () => [...slots.values()].filter((slot) => slot.market.liveOpen && !isHalted(slot))

  function isHalted(slot: Slot): boolean {
    const view = slot.market.sessionView()
    return 'status' in view && view.status === 'halted'
  }

  function send(socket: Socket, payload: string): void {
    if (socket.readyState !== socket.OPEN) return
    if (socket.bufferedAmount > 1_000_000) {
      socket.close()
      return
    }
    socket.send(payload)
  }

  function publish(slot: Slot): void {
    const session = slot.market.sessionView()
    const halted = 'status' in session && session.status === 'halted'
    const type = halted && !slot.sawHalt ? 'halt' : 'snapshot'
    if (halted && slot.haltedAt === null) slot.haltedAt = Date.now()
    slot.sawHalt = halted
    if (!('status' in session) || session.status === 'idle') slot.sawHalt = false
    const watchPayload = JSON.stringify({ type, session })
    for (const socket of slot.watchers) send(socket, watchPayload)
    if (slot.market.liveOpen) {
      const streamPayload = JSON.stringify({ type: 'snapshot', observation: slot.market.observation() })
      for (const socket of slot.streams) send(socket, streamPayload)
    }
  }

  function sendEvents(slot: Slot, events: unknown[], observation: unknown): void {
    if (events.length === 0) return
    const payload = JSON.stringify({ type: 'events', events, observation })
    for (const socket of slot.streams) send(socket, payload)
  }

  /** `sessionId` picks a session. Without one, the latest session, else the training market. */
  function resolve(sessionId: string | undefined): Slot | Failure {
    if (sessionId === undefined || sessionId === '') return current ?? training
    const slot = slots.get(sessionId)
    if (!slot) return { ok: false, status: 404, code: 'unknown_session', message: `There is no session ${sessionId}.` }
    return slot
  }

  function sessionIdOf(request: { query: unknown; body?: unknown }): string | undefined {
    const query = isRecord(request.query) ? request.query : {}
    const fromQuery = stringQuery(query.sessionId)
    if (fromQuery !== undefined) return fromQuery
    const body = isRecord(request.body) ? request.body : {}
    return typeof body.sessionId === 'string' ? body.sessionId : undefined
  }

  async function pullPrices(): Promise<void> {
    const needing = [...slots.values()].filter((slot) => slot.market.needsFeed)
    if (needing.length === 0) return
    const price = await fetchSolMid()
    for (const slot of needing) {
      const events = price ? await slot.market.setReferenceMid(price.cents, price.source) : []
      if (!price) await slot.market.markStale()
      sendEvents(slot, events, slot.market.observation())
      publish(slot)
    }
  }

  function startLoops(): void {
    if (options?.clock === false || clock) return
    clock = setInterval(() => {
      const now = Date.now()
      for (const slot of slots.values()) {
        if (slot.haltedAt !== null && now - slot.haltedAt > HALTED_TTL_MS) {
          drop(slot)
          continue
        }
        if (!slot.market.liveOpen) continue
        void slot.market.tick(200).then((events) => {
          sendEvents(slot, events, slot.market.observation())
          publish(slot)
        })
      }
    }, 200)
    feed = setInterval(() => {
      void pullPrices()
    }, 1_000)
  }

  function drop(slot: Slot): void {
    slots.delete(slot.id)
    if (current === slot) current = null
    for (const socket of [...slot.watchers, ...slot.streams]) socket.close()
    slot.release?.()
    void slot.market.close()
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
  app.addHook('onRequest', async (request, reply) => {
  reply.header('Access-Control-Allow-Origin', '*')
  reply.header('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS')
  reply.header('Access-Control-Allow-Headers', 'Content-Type')
  if (request.method === 'OPTIONS') return reply.code(204).send()})

  await app.register(websocket)

  app.get('/', async (_request, reply) => {
    const html = await readFile(join(root, 'frontend', 'index.html'))
    return reply.type('text/html').send(html)
  })

  app.get('/v1/health', async () => ({
    ok: true,
    mode: 'localhost',
    execution: options?.chain ? 'solana' : 'fake',
    sessions: { running: running().length, max: MAX_SESSIONS },
    ...(options?.chain ? { chain: options.chain.snapshot() } : {}),
  }))
  app.get('/v1/instruments', async () => instrumentsBody(training.market.feeView(), Boolean(options?.btcFeed)))
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

  // Every live session, so a bot can find the ones it should trade.
  app.get('/v1/sessions', async () => ({
    max: MAX_SESSIONS,
    sessions: [...slots.values()].map((slot) => {
      const view = slot.market.sessionView()
      return 'sessionId' in view
        ? { sessionId: view.sessionId, status: view.status, strategy: view.strategy, instrument: view.instrument, startedAt: view.startedAt }
        : { sessionId: slot.id, status: 'idle' }
    }),
  }))

  app.get('/v1/bars', async (request, reply) => {
    const slot = resolve(sessionIdOf(request))
    if ('ok' in slot) return sendFailure(reply, slot)
    const query = isRecord(request.query) ? request.query : {}
    const raw = stringQuery(query.limit)
    const limit = raw === undefined || raw === '' ? 601 : Number(raw)
    if (!Number.isInteger(limit) || limit < 1 || limit > 601) {
      return reply.code(400).send({ error: { code: 'bad_limit', message: 'limit must be an integer from 1 to 601.' } })
    }
    return slot.market.barsView(limit)
  })
  app.get('/v1/book', async (request, reply) => {
    const slot = resolve(sessionIdOf(request))
    if ('ok' in slot) return sendFailure(reply, slot)
    const view = slot.market.observation()
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
  app.get('/v1/account', async (request, reply) => {
    const slot = resolve(sessionIdOf(request))
    if ('ok' in slot) return sendFailure(reply, slot)
    return slot.market.observation()
  })
  app.get('/v1/session', async (request, reply) => {
    const id = sessionIdOf(request)
    if (id === undefined || id === '') return current ? current.market.sessionView() : { status: 'idle' }
    const slot = resolve(id)
    if ('ok' in slot) return sendFailure(reply, slot)
    return slot.market.sessionView()
  })

  app.post('/v1/session', async (request, reply) => {
    if (running().length >= MAX_SESSIONS) {
      return sendFailure(reply, {
        ok: false,
        status: 409,
        code: 'too_many_sessions',
        message: `${MAX_SESSIONS} sessions are already trading. Try again when one ends.`,
      })
    }
    const id = `sess_${++sessionSeq}`
    const slot = newSlot(id, true)
    const result = await slot.market.openSession(bodyOf(request.body), id)
    if (!result.ok) {
      slot.release?.()
      void slot.market.close()
      return sendFailure(reply, result)
    }
    slots.set(id, slot)
    current = slot
    startLoops()
    publish(slot)
    return reply.code(201).send(result.session)
  })

  app.post('/v1/stop', async (request, reply) => {
    const id = sessionIdOf(request)
    const slot = id === undefined || id === '' ? current : slots.get(id)
    if (!slot) {
      return sendFailure(reply, { ok: false, status: 404, code: 'no_session', message: 'There is no session to stop.' })
    }
    const result = await slot.market.stop()
    if (!result.ok) return sendFailure(reply, result)
    publish(slot)
    return result.session
  })

  app.post('/v1/reset', async (request, reply) => {
    if (running().length > 0) {
      return sendFailure(reply, { ok: false, status: 409, code: 'session_open', message: 'Reset is refused while a live session is running.' })
    }
    const result = await training.market.reset(bodyOf(request.body))
    if (!result.ok) return sendFailure(reply, result)
    current = null
    return { observation: result.observation, done: result.done }
  })

  app.post('/v1/step', async (request, reply) => {
    const slot = resolve(sessionIdOf(request))
    if ('ok' in slot) return sendFailure(reply, slot)
    const body = bodyOf(request.body)
    delete body.sessionId
    const result = await slot.market.step(body)
    if (!result.ok) return sendFailure(reply, result)
    publish(slot)
    sendEvents(slot, result.events, result.observation)
    return { observation: result.observation, reward: result.reward, done: result.done, events: result.events }
  })

  app.post('/v1/orders', async (request, reply) => {
    const slot = resolve(sessionIdOf({ query: request.query }))
    if ('ok' in slot) return sendFailure(reply, slot)
    const result = await slot.market.place(bodyOf(request.body))
    if (!result.ok) return sendFailure(reply, result)
    publish(slot)
    return { events: result.events, observation: result.observation }
  })

  app.delete('/v1/orders/:clientOrderId', async (request, reply) => {
    const slot = resolve(sessionIdOf({ query: request.query }))
    if ('ok' in slot) return sendFailure(reply, slot)
    const { clientOrderId } = request.params as { clientOrderId: string }
    const result = await slot.market.cancel(clientOrderId)
    if (!result.ok) return sendFailure(reply, result)
    publish(slot)
    return { events: result.events, observation: result.observation }
  })

  app.post('/v1/orders/cancel-all', async (request, reply) => {
    const slot = resolve(sessionIdOf(request))
    if ('ok' in slot) return sendFailure(reply, slot)
    const result = await slot.market.cancelAll()
    if (!result.ok) return sendFailure(reply, result)
    publish(slot)
    return { events: result.events, canceled: result.canceled, observation: result.observation }
  })

  app.post('/v1/flatten', async (request, reply) => {
    const slot = resolve(sessionIdOf(request))
    if ('ok' in slot) return sendFailure(reply, slot)
    const result = await slot.market.flatten()
    if (!result.ok) return sendFailure(reply, result)
    publish(slot)
    sendEvents(slot, result.events, result.observation)
    return { events: result.events, observation: result.observation }
  })

  app.get('/v1/watch', { websocket: true }, (socket: Socket, request) => {
    const slot = resolve(sessionIdOf(request))
    if ('ok' in slot) {
      send(socket, JSON.stringify({ type: 'error', error: { code: slot.code, message: slot.message } }))
      socket.close()
      return
    }
    slot.watchers.add(socket)
    send(socket, JSON.stringify({ type: 'snapshot', session: slot.market.sessionView() }))
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
    socket.on('close', () => slot.watchers.delete(socket))
  })

  app.get('/v1/stream', { websocket: true }, (socket: Socket, request) => {
    const slot = resolve(sessionIdOf(request))
    if ('ok' in slot) {
      send(socket, JSON.stringify({ type: 'error', error: { code: slot.code, message: slot.message } }))
      socket.close()
      return
    }
    slot.streams.add(socket)
    send(socket, JSON.stringify({ type: 'snapshot', observation: slot.market.observation() }))
    socket.on('message', (raw: Buffer) => {
      void handleStream(slot, socket, raw)
    })
    socket.on('close', () => slot.streams.delete(socket))
  })

  async function replyCommand(
    slot: Slot,
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
          observation: slot.market.observation(),
        }),
      )
    } else {
      send(socket, JSON.stringify({ type: 'error', error: { code: result.code, message: result.message } }))
    }
    publish(slot)
  }

  async function handleStream(slot: Slot, socket: Socket, raw: Buffer): Promise<void> {
    const market = slot.market
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
      await replyCommand(slot, socket, await market.place(message.order))
      return
    }
    if (message.op === 'cancel' && typeof message.clientOrderId === 'string') {
      await replyCommand(slot, socket, await market.cancel(message.clientOrderId))
      return
    }
    if (message.op === 'cancel_all') {
      await replyCommand(slot, socket, await market.cancelAll())
      return
    }
    if (message.op === 'flatten') {
      await replyCommand(slot, socket, await market.flatten())
      return
    }
    send(socket, JSON.stringify({ type: 'error', error: { code: 'invalid_message', message: 'Unrecognized stream command.' } }))
  }

  app.addHook('onClose', async () => {
    if (clock) clearInterval(clock)
    if (feed) clearInterval(feed)
    for (const slot of [...slots.values()]) drop(slot)
    await training.market.close()
    await options?.chain?.close()
  })

  return {
    app,
    get market() {
      return (current ?? training).market
    },
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
