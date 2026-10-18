import assert from 'node:assert/strict'
import { after, before, describe, it } from 'node:test'
import type { FastifyInstance } from 'fastify'
import { buildApp } from '../src/app.js'
import type { Chain, SettlementLeg } from '../src/chain.js'
import type { Market } from '../src/market.js'

describe('gateway', () => {
  let app: FastifyInstance
  let market: Market
  let close: () => Promise<void>

  before(async () => {
    const built = await buildApp({ clock: false })
    app = built.app
    market = built.market
    close = built.close
  })

  after(async () => {
    await close()
  })

  it('reports health, instruments, and an idle session', async () => {
    const health = await app.inject({ method: 'GET', url: '/v1/health' })
    assert.equal(health.statusCode, 200)
    assert.equal(health.json().execution, 'fake')
    const instruments = await app.inject({ method: 'GET', url: '/v1/instruments' })
    assert.equal(instruments.json().houseFundCents, 10_000_000)
    assert.ok(instruments.json().instruments.some((item: { symbol: string }) => item.symbol === 'SOL-USD'))
    const session = await app.inject({ method: 'GET', url: '/v1/session' })
    assert.deepEqual(session.json(), { status: 'idle' })
  })

  it('rejects a deposit outside $1 to $100 and a floor that cannot trade', async () => {
    const low = await app.inject({
      method: 'POST',
      url: '/v1/session',
      payload: { depositCents: 50, instrument: 'SOL-USD', ceilingCents: 80 },
    })
    assert.equal(low.statusCode, 400)
    assert.equal(low.json().error.code, 'deposit_out_of_range')
    const floor = await app.inject({
      method: 'POST',
      url: '/v1/session',
      payload: { depositCents: 500, instrument: 'SOL-USD', floorCents: 500, ceilingCents: 800 },
    })
    assert.equal(floor.statusCode, 400)
    assert.equal(floor.json().error.code, 'floor_out_of_range')
  })

  it('trains on synthetic SOL without touching a live stake', async () => {
    const reset = await app.inject({
      method: 'POST',
      url: '/v1/reset',
      payload: { instrument: 'SOL-USD', seed: 3, episodeSteps: 5 },
    })
    assert.equal(reset.statusCode, 200)
    assert.equal(reset.json().observation.priceSource, 'synthetic')
    assert.equal(reset.json().observation.stakeCents, null)
    assert.ok(reset.json().observation.asks.length > 0)

    const waiting = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: {
        dtMs: 10,
        orders: [{ clientOrderId: 'm1', side: 'buy', size: 1, type: 'market' }],
      },
    })
    assert.equal(waiting.statusCode, 200)
    assert.equal(waiting.json().observation.positionLots, 0)
    assert.equal(waiting.json().events[0].status, 'pending')

    const released = await app.inject({ method: 'POST', url: '/v1/step', payload: { dtMs: 100 } })
    assert.equal(released.json().observation.positionLots, 1)
    assert.equal(released.json().done, false)

    const duplicate = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: {
        dtMs: 100,
        orders: [
          { clientOrderId: 'm2', side: 'buy', size: 1, type: 'market' },
          { clientOrderId: 'm2', side: 'buy', size: 1, type: 'market' },
        ],
      },
    })
    assert.equal(duplicate.json().observation.positionLots, 2)
    assert.equal(duplicate.json().events.some((event: { duplicate?: boolean }) => event.duplicate), true)
  })

  it('rejects a post-only cross and enforces the order rate', async () => {
    await app.inject({ method: 'POST', url: '/v1/reset', payload: { instrument: 'SOL-USD', seed: 1 } })
    const cross = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: {
        dtMs: 100,
        orders: [{ clientOrderId: 'po', side: 'buy', size: 1, type: 'post_only', price: 10_000_000 }],
      },
    })
    assert.equal(
      cross.json().events.some((event: { reason?: string }) => event.reason === 'post_only_would_take'),
      true,
    )
    assert.equal(cross.json().observation.positionLots, 0)

    await app.inject({ method: 'POST', url: '/v1/reset', payload: { instrument: 'SOL-USD', seed: 1 } })
    const orders = Array.from({ length: 21 }, (_, index) => ({
      clientOrderId: `r${index}`,
      side: 'buy',
      size: 1,
      type: 'limit',
      price: 1,
    }))
    const burst = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100, orders },
    })
    const rejects = burst.json().events.filter((event: { reason?: string }) => event.reason === 'rate_limit')
    assert.equal(rejects.length, 1)
    assert.equal(burst.json().observation.openOrders.length, 20)
  })

  it('opens a stake, fills a fake SOL order, and halts at the ceiling', async () => {
    const session = await app.inject({
      method: 'POST',
      url: '/v1/session',
      payload: { depositCents: 500, instrument: 'SOL-USD', ceilingCents: 800 },
    })
    assert.equal(session.statusCode, 201)
    assert.equal(session.json().stakeCents, 500)
    assert.equal(session.json().floorCents, 0)

    const blocked = await app.inject({
      method: 'POST',
      url: '/v1/reset',
      payload: { instrument: 'SOL-USD' },
    })
    assert.equal(blocked.statusCode, 409)
    assert.equal(blocked.json().error.code, 'session_open')

    const stepped = await app.inject({ method: 'POST', url: '/v1/step', payload: { dtMs: 100 } })
    assert.equal(stepped.statusCode, 409)
    assert.equal(stepped.json().error.code, 'step_not_for_live_mid')

    await market.setReferenceMid(10_400, 'coinbase')
    const order = await app.inject({
      method: 'POST',
      url: '/v1/orders',
      payload: { clientOrderId: 'live-1', side: 'buy', size: 2_000, type: 'market' },
    })
    assert.equal(order.statusCode, 200)
    assert.equal(order.json().events[0].status, 'pending')
    await market.tick(100)
    assert.equal(market.observation().positionLots, 2_000)

    await market.setReferenceMid(20_000, 'coinbase')
    const view = market.sessionView()
    if (!('sessionId' in view)) throw new Error('expected a live session')
    assert.equal(view.status, 'halted')
    assert.equal(view.halt?.reason, 'ceiling')

    const again = await app.inject({
      method: 'POST',
      url: '/v1/orders',
      payload: { clientOrderId: 'live-2', side: 'buy', size: 1, type: 'market' },
    })
    assert.equal(again.statusCode, 409)
    assert.equal(again.json().error.code, 'session_halted')
  })

  it('stops a running session without rewriting an earlier halt', async () => {
    const first = await app.inject({ method: 'POST', url: '/v1/stop' })
    assert.equal(first.statusCode, 200)
    assert.equal(first.json().halt.reason, 'ceiling')

    const next = await app.inject({
      method: 'POST',
      url: '/v1/session',
      payload: { depositCents: 200, instrument: 'SOL-USD', floorCents: 100, ceilingCents: 400 },
    })
    assert.equal(next.statusCode, 201)
    const stopped = await app.inject({ method: 'POST', url: '/v1/stop' })
    assert.equal(stopped.json().halt.reason, 'manual')
    assert.equal(stopped.json().status, 'halted')
  })

})

describe('order lifecycle', () => {
  let app: FastifyInstance
  let market: Market
  let close: () => Promise<void>

  before(async () => {
    const built = await buildApp({ clock: false })
    app = built.app
    market = built.market
    close = built.close
  })

  after(async () => {
    await close()
  })

  it('replays a filled ack when the same id is sent again', async () => {
    await app.inject({ method: 'POST', url: '/v1/reset', payload: { instrument: 'SOL-USD', seed: 2 } })
    const first = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100, orders: [{ clientOrderId: 'once', side: 'buy', size: 1, type: 'market' }] },
    })
    assert.equal(first.json().observation.positionLots, 1)
    const again = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100, orders: [{ clientOrderId: 'once', side: 'buy', size: 1, type: 'market' }] },
    })
    assert.equal(again.json().observation.positionLots, 1)
    const duplicate = again.json().events.find((event: { clientOrderId?: string }) => event.clientOrderId === 'once')
    assert.equal(duplicate.duplicate, true)
    assert.equal(duplicate.status, 'filled')
  })

  it('cancels only after the latency delay and reuses that canceled ack', async () => {
    await app.inject({ method: 'POST', url: '/v1/reset', payload: { instrument: 'SOL-USD', seed: 4 } })
    const rested = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100, orders: [{ clientOrderId: 'bid', side: 'buy', size: 1, type: 'limit', price: 1 }] },
    })
    assert.equal(rested.json().observation.openOrders.length, 1)
    const early = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 10, cancels: ['bid'] },
    })
    assert.equal(early.json().observation.openOrders.length, 1)
    const gone = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100 },
    })
    assert.equal(gone.json().observation.openOrders.length, 0)
    const reuse = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100, orders: [{ clientOrderId: 'bid', side: 'buy', size: 1, type: 'limit', price: 1 }] },
    })
    assert.equal(reuse.json().observation.openOrders.length, 0)
    assert.equal(reuse.json().events[0].status, 'canceled')
    assert.equal(reuse.json().events[0].duplicate, true)
  })

  it('remembers a post-only reject and does not self-trade', async () => {
    await app.inject({ method: 'POST', url: '/v1/reset', payload: { instrument: 'SOL-USD', seed: 5 } })
    const cross = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: {
        dtMs: 100,
        orders: [{ clientOrderId: 'po2', side: 'buy', size: 1, type: 'post_only', price: 10_000_000 }],
      },
    })
    assert.equal(cross.json().events.at(-1).reason, 'post_only_would_take')
    const repeated = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: {
        dtMs: 100,
        orders: [{ clientOrderId: 'po2', side: 'buy', size: 1, type: 'post_only', price: 10_000_000 }],
      },
    })
    assert.equal(repeated.json().events[0].reason, 'post_only_would_take')
    assert.equal(repeated.json().events[0].duplicate, true)
    assert.equal(repeated.json().observation.positionLots, 0)

    const book = market.observation()
    const price = book.mid
    assert.ok(price > book.bids[0]!.price && price < book.asks[0]!.price)
    const traded = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: {
        dtMs: 100,
        orders: [
          { clientOrderId: 'mine', side: 'sell', size: 1, type: 'limit', price },
          { clientOrderId: 'hit', side: 'buy', size: 1, type: 'market' },
        ],
      },
    })
    assert.equal(traded.json().observation.positionLots, 1)
    assert.equal(
      traded.json().events.some((event: { clientOrderId?: string; type?: string }) => event.type === 'fill' && event.clientOrderId === 'mine'),
      false,
    )
    assert.equal(
      traded.json().events.some((event: { clientOrderId?: string; status?: string }) => event.clientOrderId === 'mine' && event.status === 'canceled'),
      true,
    )
    assert.equal(
      traded.json().events.some((event: { clientOrderId?: string; type?: string }) => event.type === 'fill' && event.clientOrderId === 'hit'),
      true,
    )
  })

  it('cancels the unfilled remainder of an IOC at the touch', async () => {
    const opened = await app.inject({ method: 'POST', url: '/v1/reset', payload: { instrument: 'SOL-USD', seed: 6 } })
    const ask = opened.json().observation.asks[0]
    const ioc = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: {
        dtMs: 100,
        orders: [{ clientOrderId: 'ioc1', side: 'buy', size: ask.size + 1, type: 'ioc', price: ask.price + 4 }],
      },
    })
    assert.equal(ioc.json().observation.positionLots, ask.size)
    assert.equal(ioc.json().observation.openOrders.length, 0)
    assert.equal(ioc.json().events.some((event: { type?: string }) => event.type === 'fill'), true)
    assert.equal(
      ioc.json().events.some((event: { clientOrderId?: string; status?: string }) => event.clientOrderId === 'ioc1' && event.status === 'canceled'),
      true,
    )
  })

  it('walks the BTC tape without paying a flat account for the mid move', async () => {
    const reset = await app.inject({ method: 'POST', url: '/v1/reset', payload: { episodeSteps: 3 } })
    assert.equal(reset.statusCode, 200)
    assert.equal(reset.json().observation.priceSource, 'tape')
    assert.ok(reset.json().observation.bids.length > 0)
    const flat = await app.inject({ method: 'POST', url: '/v1/step', payload: { dtMs: 1_000 } })
    assert.equal(flat.json().reward, 0)
    assert.equal(flat.json().observation.positionLots, 0)
    const clock = flat.json().observation.ts

    const tick = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100, orders: [{ clientOrderId: 'tick', side: 'buy', size: 1, type: 'limit', price: 6_000_001 }] },
    })
    assert.equal(tick.statusCode, 400)
    assert.equal(tick.json().error.reason, 'bad_tick')
    const priced = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100, orders: [{ clientOrderId: 'px', side: 'buy', size: 1, type: 'market', price: 10 }] },
    })
    assert.equal(priced.statusCode, 400)
    assert.equal(priced.json().error.reason, 'price_forbidden')
    const account = await app.inject({ method: 'GET', url: '/v1/account' })
    assert.equal(account.json().ts, clock)

    const limited = await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: {
        dtMs: 100,
        orders: [
          { clientOrderId: 'big', side: 'buy', size: 100_000, type: 'limit', price: 10 },
          { clientOrderId: 'over', side: 'buy', size: 1, type: 'limit', price: 10 },
        ],
      },
    })
    assert.equal(limited.json().events.some((event: { reason?: string }) => event.reason === 'position_limit'), true)
    const finished = await app.inject({ method: 'POST', url: '/v1/step', payload: { dtMs: 100 } })
    assert.equal(finished.json().done, true)
  })

  it('flattens a training position without halting', async () => {
    await app.inject({ method: 'POST', url: '/v1/reset', payload: { instrument: 'SOL-USD', seed: 7 } })
    await app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100, orders: [{ clientOrderId: 'open', side: 'buy', size: 1, type: 'market' }] },
    })
    const flat = await app.inject({ method: 'POST', url: '/v1/flatten' })
    assert.equal(flat.statusCode, 200)
    assert.equal(flat.json().observation.positionLots, 0)
    assert.equal(flat.json().observation.halted, false)
    const next = await app.inject({
      method: 'POST',
      url: '/v1/orders',
      payload: { clientOrderId: 'again', side: 'buy', size: 1, type: 'limit', price: 1 },
    })
    assert.equal(next.statusCode, 200)
    assert.equal(next.json().events[0].status, 'pending')
  })

  it('halts a live session at the floor when the mark collapses', async () => {
    const session = await app.inject({
      method: 'POST',
      url: '/v1/session',
      payload: { depositCents: 500, instrument: 'SOL-USD', ceilingCents: 100_000 },
    })
    assert.equal(session.statusCode, 201)
    await app.inject({
      method: 'POST',
      url: '/v1/orders',
      payload: { clientOrderId: 'long', side: 'buy', size: 2_000, type: 'market' },
    })
    await market.tick(100)
    assert.equal(market.observation().positionLots, 2_000)
    await market.setReferenceMid(100, 'coinbase')
    const view = market.sessionView()
    if (!('sessionId' in view)) throw new Error('expected a live session')
    assert.equal(view.status, 'halted')
    assert.equal(view.halt?.reason, 'floor')
  })
})

describe('history', () => {
  function kline(price: string) {
    return [1_710_000_000_000, price, price, price, price, '12.5']
  }

  it('returns candles for a coin that is not a session instrument', async () => {
    const seen: string[] = []
    const built = await buildApp({
      clock: false,
      historyFetch: async (input) => {
        seen.push(String(input))
        return new Response(JSON.stringify([kline('3500.125'), kline('0.00001234')]))
      },
    })
    const response = await built.app.inject({ method: 'GET', url: '/v1/history?symbol=ETH&interval=1h&limit=2' })
    assert.equal(response.statusCode, 200)
    const body = response.json()
    assert.equal(body.symbol, 'ETH')
    assert.equal(body.market, 'ETH-USD')
    assert.equal(body.source, 'binance')
    assert.equal(body.candles.length, 2)
    assert.equal(body.candles[0].openCents, 350013)
    assert.equal(body.candles[0].open, '3500.125')
    assert.equal(body.candles[1].closeCents, 0)
    assert.equal(body.candles[1].close, '0.00001234')
    assert.match(seen[0]!, /symbol=ETHUSDT/)
    assert.match(seen[0]!, /interval=1h/)
    assert.match(seen[0]!, /limit=2/)
    await built.close()
  })

  it('accepts an instrument symbol and rejects a bad ticker', async () => {
    const built = await buildApp({
      clock: false,
      historyFetch: async () => new Response(JSON.stringify([kline('100')])),
    })
    const sol = await built.app.inject({ method: 'GET', url: '/v1/history?symbol=SOL-USD' })
    assert.equal(sol.statusCode, 200)
    assert.equal(sol.json().symbol, 'SOL')
    const btc = await built.app.inject({ method: 'GET', url: '/v1/history?symbol=BTC-USDT' })
    assert.equal(btc.json().symbol, 'BTC')
    const bad = await built.app.inject({ method: 'GET', url: '/v1/history?symbol=../SOL' })
    assert.equal(bad.statusCode, 400)
    assert.equal(bad.json().error.code, 'bad_symbol')
    const interval = await built.app.inject({ method: 'GET', url: '/v1/history?symbol=ETH&interval=2h' })
    assert.equal(interval.json().error.code, 'bad_interval')
    const limit = await built.app.inject({ method: 'GET', url: '/v1/history?symbol=ETH&limit=0' })
    assert.equal(limit.json().error.code, 'bad_limit')
    await built.close()
  })

  it('maps a missing market to unknown_symbol', async () => {
    const built = await buildApp({
      clock: false,
      historyFetch: async (input) => {
        const url = String(input)
        if (url.includes('binance.com')) {
          return new Response(JSON.stringify({ code: -1121, msg: 'Invalid symbol.' }), { status: 400 })
        }
        if (url.includes('coinbase.com')) return new Response(JSON.stringify({ message: 'NotFound' }), { status: 404 })
        return new Response(JSON.stringify({ error: ['EQuery:Unknown asset pair'], result: {} }))
      },
    })
    const response = await built.app.inject({ method: 'GET', url: '/v1/history?symbol=NOTACOIN' })
    assert.equal(response.statusCode, 404)
    assert.equal(response.json().error.code, 'unknown_symbol')
    await built.close()
  })

  it('uses Coinbase when Binance refuses the region', async () => {
    const built = await buildApp({
      clock: false,
      historyFetch: async (input) => {
        const url = String(input)
        if (url.includes('binance.com')) {
          return new Response(JSON.stringify({ code: 0, msg: 'restricted' }), { status: 451 })
        }
        assert.match(url, /products\/DOGE-USD/)
        return new Response(JSON.stringify([[1_710_000_000, 0.1, 0.2, 0.12, 0.15, 1000]]))
      },
    })
    const response = await built.app.inject({ method: 'GET', url: '/v1/history?symbol=DOGE&interval=1h&limit=1' })
    assert.equal(response.statusCode, 200)
    assert.equal(response.json().source, 'coinbase')
    assert.equal(response.json().candles[0].open, '0.12')
    assert.equal(response.json().candles[0].high, '0.2')
    assert.equal(response.json().candles[0].low, '0.1')
    assert.equal(response.json().candles[0].closeCents, 15)
    await built.close()
  })
})

describe('solana settlement', () => {
  it('sends a live fill to the chain and leaves training in memory', async () => {
    const settled: SettlementLeg[][] = []
    const chain: Chain = {
      async start() {},
      async settle(legs) {
        settled.push(legs)
        return 'sig-test'
      },
      snapshot() {
        return { rpc: 'http://127.0.0.1:8899', ready: true, pool: 'pool' }
      },
      async close() {},
    }
    const built = await buildApp({ clock: false, chain })
    const health = await built.app.inject({ method: 'GET', url: '/v1/health' })
    assert.equal(health.json().execution, 'solana')

    await built.app.inject({ method: 'POST', url: '/v1/reset', payload: { instrument: 'SOL-USD', seed: 1 } })
    await built.app.inject({
      method: 'POST',
      url: '/v1/step',
      payload: { dtMs: 100, orders: [{ clientOrderId: 'train', side: 'buy', size: 1, type: 'market' }] },
    })
    assert.equal(settled.length, 0)

    const session = await built.app.inject({
      method: 'POST',
      url: '/v1/session',
      payload: { depositCents: 500, instrument: 'SOL-USD', ceilingCents: 100_000 },
    })
    assert.equal(session.statusCode, 201)
    await built.app.inject({
      method: 'POST',
      url: '/v1/orders',
      payload: { clientOrderId: 'chain-1', side: 'buy', size: 2, type: 'market' },
    })
    const events = await built.market.tick(100)
    const fill = events.find((event) => event.type === 'fill')
    assert.ok(fill && fill.type === 'fill')
    assert.equal(fill.signature, 'sig-test')
    assert.equal(settled.length, 1)
    assert.equal(settled[0]![0]!.side, 'buy')
    assert.equal(settled[0]![0]!.lots, 2)
    assert.ok(settled[0]![0]!.quoteCents > 0)
    await built.close()
  })
})
