import { OrderBook, type MatchFill, type RestingOrder } from './book.js'
import { ChainError, type Chain, type SettlementLeg } from './chain.js'
import { mulberry32, type Rng } from './rng.js'
import { fetchSolBars, fetchSolMid } from './price.js'
import { BtcTape, type BtcSource, type LiveBtcSource, type TopOfBook } from './tape.js'
import {
  BOOK_DEPTH,
  DEPOSIT_MAX,
  DEPOSIT_MIN,
  FILL_HISTORY,
  HOUSE_CENTS,
  INSTRUMENTS,
  LAMPORTS_PER_SOL,
  LATENCY_MS,
  MAKER_BPS,
  MAX_DT_MS,
  MID_HISTORY,
  RATE_LIMIT,
  RATE_WINDOW_MS,
  DEFAULT_INSTRUMENT,
  DEFAULT_STRATEGY,
  SETTLEMENT_SIGNATURES,
  STRATEGIES,
  STRATEGY_ALIASES,
  SOLANA_LAMPORTS_PER_SIGNATURE,
  STAKE_HISTORY,
  TAKER_BPS,
  feeCents,
  type FeeModel,
  instrumentOf,
  notionalCents,
  rejectReasonMessage,
  type HaltReason,
  type Instrument,
  type InstrumentSymbol,
  type MarketEvent,
  type Observation,
  type OpenOrder,
  type OrderInput,
  type OrderType,
  type PriceSource,
  type SessionView,
  type Side,
  type Strategy,
  type UserFill,
} from './types.js'

/** Bar width the model's features use, and how many bars its lookback needs (two minutes plus one). */
const BAR_MS = 200
const BAR_HISTORY = 601

export interface Bar {
  ts: number
  bid: number
  ask: number
  bidSize: number
  askSize: number
  mid: number
  tradeSide: Side | null
  tradeSize: number
}

const ID_RE = /^[A-Za-z0-9._:-]{1,64}$/
const ORDER_FIELDS = new Set(['clientOrderId', 'side', 'size', 'type', 'price', 'timeInForce'])
interface WorkingOrder {
  orderId: string
  clientOrderId: string
  side: Side
  price: number | null
  size: number
  type: OrderType
  tif: 'gtc' | 'ioc'
  releaseAt: number
  status: 'pending' | 'open'
}

interface Job {
  seq: number
  releaseAt: number
  kind: 'place' | 'cancel'
  clientOrderId: string
}

interface StoredFill extends UserFill {
  wall: string
}

type Cached = Extract<MarketEvent, { type: 'ack' | 'reject' }>

export interface Failure {
  ok: false
  status: number
  code: string
  message: string
  clientOrderId?: string
  reason?: string
  duplicate?: boolean
}

export interface PlaceBody {
  events: MarketEvent[]
  observation: Observation
}

export interface StepBody extends PlaceBody {
  reward: number
  done: boolean
}

function failure(
  status: number,
  code: string,
  message: string,
  extra?: Partial<Failure>,
): Failure {
  return { ok: false, status, code, message, ...extra }
}

export class Market {
  private readonly book = new OrderBook()
  private readonly tape: BtcTape
  private lock: Promise<void> = Promise.resolve()

  private mode: 'idle' | 'training' | 'live' = 'idle'
  private instrument: Instrument = INSTRUMENTS[0]!
  private data: 'tape' | 'synthetic' | 'reference' = 'tape'
  private rng: Rng = mulberry32(1)
  private marketMs = 0
  private stepCount = 0
  private episodeSteps = 1_000
  private mid = 0
  private source: PriceSource = 'tape'
  private referenceMid: number | null = null
  private mids: number[] = []
  private lastTrade: Observation['lastTrade'] = null
  private bars: Bar[] = []

  private cash = HOUSE_CENTS
  private position = 0
  private avg = 0
  private realized = 0
  private startEquity = HOUSE_CENTS

  private session: {
    id: string
    strategy: Strategy
    deposit: number
    floor: number
    ceiling: number
    startedAt: string
    startedMs: number
  } | null = null
  private halted = false
  private haltReason: HaltReason | null = null
  private haltAt: string | null = null
  private frozenStake: number | null = null
  private frozenPnl: number | null = null

  private working = new Map<string, WorkingOrder>()
  private jobs: Job[] = []
  private cache = new Map<string, Cached>()
  private placedAt: number[] = []
  private fills: StoredFill[] = []
  private stakeHistory: Array<{ ts: string; stakeCents: number }> = []
  private seq = 0
  private orderSeq = 0
  private sessionSeq = 0
  private extSeq = 0

  /** Fractions of a cent of Solana fee not yet charged. Cash is whole cents, so they carry over. */
  private feeCarry = 0
  /** SOL price used to turn lamports into cents. Refreshed at session open and from the SOL feed. */
  private solCents = 10_000

  /** True when the current BTC-USDT session reads the live feed instead of the tape. */
  private btcLive = false

  constructor(
    tapeDirectory: string,
    private readonly chain: Chain | null = null,
    readonly feeModel: FeeModel = 'exchange',
    /** Live BTC-USDT book for live sessions. Training always replays the tape. */
    private readonly btcFeed: LiveBtcSource | null = null,
  ) {
    this.tape = new BtcTape(tapeDirectory)
  }

  /** Live sessions read the live feed when one is attached. Everything else replays the tape. */
  private get btc(): BtcSource {
    return this.btcLive && this.btcFeed ? this.btcFeed : this.tape
  }

  get btcFeedAttached(): boolean {
    return this.btcFeed !== null
  }

  /** Network fee of one settlement transaction, in fractional cents. */
  solanaFeeCents(): number {
    return ((SOLANA_LAMPORTS_PER_SIGNATURE * SETTLEMENT_SIGNATURES) / LAMPORTS_PER_SOL) * this.solCents
  }

  feeView(): { model: FeeModel; makerBps: number; takerBps: number; solanaFeeCents: number } {
    const exchange = this.feeModel === 'exchange'
    return {
      model: this.feeModel,
      makerBps: exchange ? MAKER_BPS : 0,
      takerBps: exchange ? TAKER_BPS : 0,
      solanaFeeCents: exchange ? 0 : this.solanaFeeCents(),
    }
  }

  /** Whole cents to charge for one fill. Solana fees carry their fraction to the next fill. */
  private feeFor(notional: number, liquidity: 'maker' | 'taker'): number {
    if (this.feeModel === 'exchange') return feeCents(notional, liquidity === 'maker' ? MAKER_BPS : TAKER_BPS)
    this.feeCarry += this.solanaFeeCents()
    // Ten 0.1-cent fees sum to 0.9999999999999999 in floating point; that is still one cent.
    const whole = Math.floor(this.feeCarry + 1e-9)
    this.feeCarry -= whole
    return whole
  }

  get liveOpen(): boolean {
    return this.mode === 'live'
  }

  get needsFeed(): boolean {
    return this.mode === 'live' && this.instrument.symbol === 'SOL-USD'
  }

  /** Serialized so the wall clock and HTTP handlers cannot interleave. */
  private run<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.lock.then(fn, fn)
    this.lock = next.then(
      () => undefined,
      () => undefined,
    )
    return next
  }

  markStale(): Promise<void> {
    return this.run(async () => {
      if (this.data !== 'reference') return
      this.source = 'stale'
    })
  }

  close(): Promise<void> {
    return this.run(async () => {
      await this.tape.close()
    })
  }

  setReferenceMid(cents: number, source: PriceSource): Promise<MarketEvent[]> {
    return this.run(async () => {
      if (this.data !== 'reference' || this.halted) return []
      this.referenceMid = this.align(cents)
      this.solCents = cents
      this.source = source
      this.quoteReference()
      const events = [...(await this.crossResting()), ...(await this.maybeHalt())]
      this.rememberStake()
      return events
    })
  }

  tick(dtMs: number): Promise<MarketEvent[]> {
    return this.run(async () => {
      if (this.mode !== 'live') return []
      const events = await this.advance(dtMs)
      this.rememberStake()
      return events
    })
  }

  reset(input: Record<string, unknown>): Promise<{ ok: true; observation: Observation; done: false } | Failure> {
    return this.run(async () => {
      if (this.mode === 'live') {
        return failure(409, 'session_open', 'Reset is refused while a live session exists.')
      }
      const extra = unknown(input, new Set(['instrument', 'seed', 'episodeSteps']))
      if (extra) return extra
      const symbol = input.instrument === undefined ? 'BTC-USDT' : input.instrument
      if (typeof symbol !== 'string' || !instrumentOf(symbol)) {
        return failure(400, 'invalid_type', 'instrument must be a known symbol.')
      }
      const inst = instrumentOf(symbol)!
      let seed = 1
      if (input.seed !== undefined) {
        if (!isInt(input.seed) || input.seed < 0 || input.seed > 2_147_483_647) {
          return failure(400, 'bad_seed', 'seed must be an integer from 0 to 2147483647.')
        }
        seed = input.seed
      }
      let episodeSteps = 1_000
      if (input.episodeSteps !== undefined) {
        if (!isInt(input.episodeSteps) || input.episodeSteps < 1 || input.episodeSteps > 1_000_000) {
          return failure(400, 'bad_episode_steps', 'episodeSteps must be an integer from 1 to 1000000.')
        }
        episodeSteps = input.episodeSteps
      }
      await this.begin({
        mode: 'training',
        instrument: inst,
        seed,
        episodeSteps,
        deposit: null,
      })
      return { ok: true, observation: this.observation(), done: false }
    })
  }

  step(input: Record<string, unknown>): Promise<({ ok: true } & StepBody) | Failure> {
    return this.run(async () => {
      if (this.mode === 'live' && this.halted) {
        return failure(409, 'session_halted', 'The live session is halted.')
      }
      if (this.mode === 'live' && this.instrument.symbol === 'SOL-USD') {
        return failure(409, 'step_not_for_live_mid', 'A live SOL-USD session advances on the wall clock.')
      }
      if (this.mode === 'live' && this.btcLive) {
        return failure(409, 'step_not_for_live_feed', 'A live BTC-USDT session follows the live market on the wall clock.')
      }
      if (this.mode === 'idle') {
        return failure(409, 'session_required', 'Call reset before step, or open a BTC-USDT session.')
      }
      const extra = unknown(input, new Set(['orders', 'cancels', 'dtMs']))
      if (extra) return extra
      const dtMs = input.dtMs === undefined ? 100 : input.dtMs
      if (!isInt(dtMs) || dtMs < 1 || dtMs > MAX_DT_MS) {
        return failure(400, 'bad_dt', 'dtMs must be an integer from 1 to 60000.')
      }
      if (input.cancels !== undefined && !isStringArray(input.cancels)) {
        return failure(400, 'bad_cancels', 'cancels must be an array of client order id strings.')
      }
      const orders = input.orders === undefined ? [] : input.orders
      if (!Array.isArray(orders)) return failure(400, 'invalid_type', 'orders must be an array.')
      for (const order of orders) {
        if (!isRecord(order)) return failure(400, 'invalid_type', 'Each order must be an object.')
        const extraOrder = unknown(order, ORDER_FIELDS)
        if (extraOrder) return extraOrder
        const shape = shapeError(order, this.instrument)
        if (shape) {
          return failure(400, 'bad_order', rejectReasonMessage(shape.reason), {
            clientOrderId: shape.clientOrderId,
            reason: shape.reason,
          })
        }
      }
      const events: MarketEvent[] = []
      const queued: string[] = []
      for (const raw of orders) {
        const order = raw as OrderInput
        const id = String(order.clientOrderId)
        const cached = this.cache.get(id)
        if (cached) {
          events.push({ ...cached, duplicate: true })
          continue
        }
        const risk = this.riskError(normalized(order, this.instrument))
        if (risk) {
          const reject: Cached = { type: 'reject', clientOrderId: id, reason: risk, duplicate: false }
          this.cache.set(id, reject)
          events.push(reject)
          continue
        }
        const working = this.enqueuePlace(normalized(order, this.instrument))
        const ack = this.pendingAck(working)
        this.cache.set(id, ack)
        queued.push(working.clientOrderId)
        events.push(ack)
      }
      for (const id of (input.cancels as string[] | undefined) ?? []) {
        const queuedCancel = this.enqueueCancel(id)
        if (!queuedCancel) {
          events.push({ type: 'reject', clientOrderId: id, reason: 'unknown_order', duplicate: false })
        }
      }
      const before = this.equity()
      const released = await this.advance(dtMs)
      this.stepCount += 1
      const finished = new Set(queued.filter((id) => this.working.get(id)?.status !== 'pending'))
      const visible = events.filter(
        (event) =>
          !(
            event.type === 'ack' &&
            event.status === 'pending' &&
            !event.duplicate &&
            finished.has(event.clientOrderId)
          ),
      )
      this.rememberStake()
      return {
        ok: true,
        observation: this.observation(),
        reward: this.equity() - before,
        done: this.mode === 'training' && this.stepCount >= this.episodeSteps,
        events: [...visible, ...released],
      }
    })
  }

  place(input: Record<string, unknown>): Promise<({ ok: true } & PlaceBody) | Failure> {
    return this.run(async () => this.placeNow(input, true))
  }

  cancel(clientOrderId: string): Promise<({ ok: true } & PlaceBody) | Failure> {
    return this.run(async () => {
      const blocked = this.tradingBlocked('')
      if (blocked) return blocked
      if (!ID_RE.test(clientOrderId)) {
        return failure(400, 'bad_order', rejectReasonMessage('bad_client_order_id'), {
          clientOrderId,
          reason: 'bad_client_order_id',
        })
      }
      const queued = this.enqueueCancel(clientOrderId)
      if (!queued) {
        return failure(404, 'unknown_order', rejectReasonMessage('unknown_order'), {
          clientOrderId,
          reason: 'unknown_order',
        })
      }
      return { ok: true, events: [queued], observation: this.observation() }
    })
  }

  cancelAll(): Promise<({ ok: true; canceled: string[] } & PlaceBody) | Failure> {
    return this.run(async () => {
      const blocked = this.tradingBlocked('')
      if (blocked) return blocked
      const ids = [...this.working.keys()]
      const events: MarketEvent[] = []
      for (const id of ids) {
        const ack = this.enqueueCancel(id)
        if (ack) events.push(ack)
      }
      return { ok: true, canceled: ids, events, observation: this.observation() }
    })
  }

  flatten(): Promise<({ ok: true } & PlaceBody) | Failure> {
    return this.run(async () => {
      const blocked = this.tradingBlocked('')
      if (blocked) return blocked
      const events = await this.flattenNow(false)
      this.rememberStake()
      return { ok: true, events, observation: this.observation() }
    })
  }

  /** `id` comes from the gateway, which numbers sessions across every market it runs. */
  openSession(input: Record<string, unknown>, id?: string): Promise<{ ok: true; session: SessionView } | Failure> {
    return this.run(async () => {
      if (this.mode === 'live' && !this.halted) {
        return failure(409, 'session_running', 'A session is already running. Stop it before starting another.')
      }
      const extra = unknown(input, new Set(['depositCents', 'instrument', 'floorCents', 'ceilingCents', 'strategy']))
      if (extra) return extra
      const deposit = input.depositCents
      if (!isInt(deposit) || deposit < DEPOSIT_MIN || deposit > DEPOSIT_MAX) {
        return failure(400, 'deposit_out_of_range', 'Deposit must be an integer from 100 to 10000 cents.')
      }
      const symbol = input.instrument === undefined ? DEFAULT_INSTRUMENT : input.instrument
      if (typeof symbol !== 'string' || !instrumentOf(symbol)) {
        return failure(400, 'unknown_instrument', 'Instrument is not available.')
      }
      const inst = instrumentOf(symbol)!
      const named = input.strategy === undefined ? DEFAULT_STRATEGY : input.strategy
      const strategy = typeof named === 'string' ? (STRATEGY_ALIASES[named] ?? named) : named
      if (typeof strategy !== 'string' || !STRATEGIES.includes(strategy as Strategy)) {
        return failure(400, 'bad_strategy', `Strategy must be one of: ${STRATEGIES.join(', ')}.`)
      }
      const floor = input.floorCents === undefined ? 0 : input.floorCents
      if (!isInt(floor) || floor < 0 || floor >= deposit) {
        return failure(400, 'floor_out_of_range', 'Floor must be an integer from 0 up to, but not including, the deposit.')
      }
      if (input.ceilingCents === undefined) return failure(400, 'ceiling_required', 'Ceiling is required.')
      if (!isInt(input.ceilingCents) || input.ceilingCents <= deposit) {
        return failure(400, 'ceiling_not_above_deposit', 'Ceiling must be an integer strictly above the deposit.')
      }
      if (inst.symbol === 'BTC-USDT' && this.btcFeed && !this.btcFeed.available) {
        return failure(503, 'feed_unavailable', 'Live Bitcoin prices are not available right now. Try again in a few seconds.')
      }
      if (this.chain) {
        try {
          await this.chain.start()
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Local Solana validator is unavailable.'
          return failure(503, 'chain_unavailable', message)
        }
      }
      await this.begin({
        mode: 'live',
        instrument: inst,
        seed: 1,
        episodeSteps: 1_000,
        deposit,
      })
      await this.preroll()
      if (this.feeModel === 'solana') {
        const sol = await fetchSolMid()
        if (sol) this.solCents = sol.cents
      }
      this.session = {
        id: id ?? `sess_${++this.sessionSeq}`,
        strategy: strategy as Strategy,
        deposit,
        floor,
        ceiling: input.ceilingCents,
        startedAt: new Date().toISOString(),
        startedMs: Date.now(),
      }
      this.rememberStake()
      return { ok: true, session: this.liveView() }
    })
  }

  stop(): Promise<{ ok: true; session: SessionView } | Failure> {
    return this.run(async () => {
      if (this.mode !== 'live' || !this.session) {
        return failure(404, 'no_session', 'There is no session to stop.')
      }
      if (!this.halted) await this.halt('manual')
      return { ok: true, session: this.liveView() }
    })
  }

  private liveView(): SessionView {
    const view = this.sessionView()
    if (!('sessionId' in view)) throw new Error('Live session is missing')
    return view
  }

  sessionView(): SessionView | { status: 'idle' } {
    if (!this.session || this.mode !== 'live') return { status: 'idle' }
    const stake = this.stakeCents()
    const pnl = stake - this.session.deposit
    return {
      sessionId: this.session.id,
      status: this.halted ? 'halted' : 'running',
      halt: this.halted
        ? {
            reason: this.haltReason!,
            stakeCents: this.frozenStake ?? stake,
            poolPnlCents: this.frozenPnl ?? pnl,
            at: this.haltAt!,
          }
        : null,
      instrument: this.instrument.symbol,
      strategy: this.session.strategy,
      depositCents: this.session.deposit,
      floorCents: this.session.floor,
      ceilingCents: this.session.ceiling,
      stakeCents: this.frozenStake ?? stake,
      poolPnlCents: this.frozenPnl ?? pnl,
      houseFundCents: HOUSE_CENTS,
      poolEquityCents: this.equity(),
      positionLots: this.position,
      freeCashCents: this.cash,
      realizedPnlCents: this.realized,
      unrealizedPnlCents: this.unrealized(),
      midCents: this.mid,
      priceSource: this.source,
      feed: this.source === 'stale' ? 'stale' : 'ok',
      bids: this.book.depth('buy', BOOK_DEPTH),
      asks: this.book.depth('sell', BOOK_DEPTH),
      openOrders: this.openOrders(),
      fills: this.fills.slice(0, FILL_HISTORY).map((fill) => ({
        clientOrderId: fill.clientOrderId,
        orderId: fill.orderId,
        side: fill.side,
        price: fill.price,
        size: fill.size,
        fee: fill.fee,
        liquidity: fill.liquidity,
        ts: fill.wall,
        signature: fill.signature,
      })),
      stakeHistory: this.stakeHistory,
      startedAt: this.session.startedAt,
    }
  }

  observation(): Observation {
    const live = this.mode === 'live' && this.session
    return {
      market: this.instrument.symbol,
      clock: this.mode === 'live' ? 'wall' : 'step',
      ts: this.marketMs,
      mid: this.mid,
      priceSource: this.source,
      bids: this.book.depth('buy', BOOK_DEPTH),
      asks: this.book.depth('sell', BOOK_DEPTH),
      lastTrade: this.lastTrade,
      mids: [...this.mids],
      positionLots: this.position,
      freeCashCents: this.cash,
      equityCents: this.equity(),
      realizedPnlCents: this.realized,
      unrealizedPnlCents: this.unrealized(),
      openOrders: this.openOrders(),
      halted: live ? this.halted : false,
      haltReason: live ? this.haltReason : null,
      stakeCents: live ? (this.frozenStake ?? this.stakeCents()) : null,
      floorCents: live ? this.session!.floor : null,
      ceilingCents: live ? this.session!.ceiling : null,
      strategy: live ? this.session!.strategy : null,
      step: this.stepCount,
      done: this.mode === 'training' && this.stepCount >= this.episodeSteps,
    }
  }

  private async begin(input: {
    mode: 'training' | 'live'
    instrument: Instrument
    seed: number
    episodeSteps: number
    deposit: number | null
  }): Promise<void> {
    await this.tape.close()
    this.book.clear()
    this.mode = input.mode
    this.instrument = input.instrument
    this.episodeSteps = input.episodeSteps
    this.rng = mulberry32(input.seed)
    this.marketMs = 0
    this.stepCount = 0
    this.position = 0
    this.avg = 0
    this.realized = 0
    this.cash = HOUSE_CENTS + (input.deposit ?? 0)
    this.startEquity = this.cash
    this.halted = false
    this.haltReason = null
    this.haltAt = null
    this.frozenStake = null
    this.frozenPnl = null
    this.working.clear()
    this.jobs = []
    this.cache.clear()
    this.placedAt = []
    this.fills = []
    this.stakeHistory = []
    this.lastTrade = null
    this.mids = []
    this.bars = []
    this.feeCarry = 0
    this.referenceMid = null
    if (this.mode !== 'live') this.session = null
    this.btcLive = input.mode === 'live' && input.instrument.symbol === 'BTC-USDT' && this.btcFeed !== null
    if (input.instrument.symbol === 'BTC-USDT') {
      this.data = 'tape'
      this.source = this.btcLive ? 'binance' : 'tape'
      if (!this.btc.available) throw new Error(this.btcLive ? 'Live BTC prices are unavailable' : 'BTC tape files are missing')
      const top = await this.btc.open()
      this.applyTop(top)
    } else if (input.mode === 'live') {
      this.data = 'reference'
      this.source = 'stale'
      this.mid = this.align(10_400)
      this.quoteReference()
    } else {
      this.data = 'synthetic'
      this.source = 'synthetic'
      this.mid = this.align(10_400)
      this.quoteSynthetic(0)
    }
    this.noteMid()
  }

  private async advance(dtMs: number): Promise<MarketEvent[]> {
    const events: MarketEvent[] = []
    const target = this.marketMs + dtMs
    while (this.marketMs < target && !this.halted) {
      const nextJob = this.jobs[0]
      const slice = Math.min(target, nextJob ? nextJob.releaseAt : target)
      if (slice > this.marketMs) {
        events.push(...(await this.moveBook(slice)))
        this.marketMs = slice
        events.push(...(await this.crossResting()), ...(await this.maybeHalt()))
      }
      if (this.halted) break
      if (this.jobs[0] && this.jobs[0].releaseAt <= this.marketMs) {
        const job = this.jobs.shift()!
        const released = job.kind === 'place' ? await this.releasePlace(job.clientOrderId) : this.releaseCancel(job.clientOrderId)
        events.push(...released)
        events.push(...(await this.maybeHalt()))
      }
      if (!this.jobs[0] && this.marketMs < target) {
        events.push(...(await this.moveBook(target)))
        this.marketMs = target
        events.push(...(await this.crossResting()), ...(await this.maybeHalt()))
      }
    }
    if (this.halted && this.marketMs < target) this.marketMs = target
    this.recordBar()
    return events
  }

  private async moveBook(marketMs: number): Promise<MarketEvent[]> {
    if (this.data === 'tape') {
      const top = await this.btc.advance(marketMs)
      this.applyTop(top, marketMs)
      if (this.btcLive && this.btcFeed) this.source = this.btcFeed.stale ? 'stale' : 'binance'
      return []
    }
    if (this.data === 'synthetic') return this.quoteSynthetic(marketMs - this.marketMs)
    this.quoteReference()
    return []
  }

  /** `ts` is the time the tape was advanced to, so a trade lands in the bar the observation reports. */
  private applyTop(top: TopOfBook, ts = this.marketMs): void {
    this.replaceExternal(top.bid, top.bidSize, top.ask, top.askSize)
    this.mid = Math.round((top.bid + top.ask) / 2)
    if (top.trade) {
      this.lastTrade = { ...top.trade, ts }
    }
    this.noteMid()
  }

  /** One bar per BAR_MS of market time: top of book plus a trade printed in that bar. */
  private recordBar(): void {
    const bid = this.book.depth('buy', 1)[0]
    const ask = this.book.depth('sell', 1)[0]
    if (!bid || !ask) return
    const bucket = Math.floor(this.marketMs / BAR_MS)
    const trade = this.lastTrade && Math.floor(this.lastTrade.ts / BAR_MS) === bucket ? this.lastTrade : null
    const bar: Bar = {
      ts: this.marketMs,
      bid: bid.price,
      ask: ask.price,
      bidSize: bid.size,
      askSize: ask.size,
      mid: this.mid,
      tradeSide: trade ? trade.side : null,
      tradeSize: trade ? trade.size : 0,
    }
    const last = this.bars[this.bars.length - 1]
    if (last && Math.floor(last.ts / BAR_MS) === bucket) {
      this.bars[this.bars.length - 1] = bar
      return
    }
    this.bars.push(bar)
    if (this.bars.length > BAR_HISTORY) this.bars.shift()
  }

  /**
   * Fill the bar history with the two minutes before a live session starts, so a model's
   * lookback window is full at t=0. BTC plays the first two minutes of the tape and the
   * session clock starts after them. SOL rebuilds the mid from recent Coinbase trades,
   * shifted to meet the current live mid, and quotes it the same way the live feed does.
   * History is skipped, not faked, when it is unavailable.
   */
  private async preroll(): Promise<void> {
    if (this.data === 'tape' && this.btcLive && this.btcFeed) {
      // The live feed has been sampling the book since the server started.
      const history = this.btcFeed.history(BAR_HISTORY)
      for (let i = 0; i < history.length; i += 1) {
        this.marketMs = (i + 1) * BAR_MS
        this.applyTop(history[i], this.marketMs)
        this.recordBar()
      }
      this.applyTop(await this.btcFeed.open(), this.marketMs)
    } else if (this.data === 'tape') {
      for (let i = 1; i <= BAR_HISTORY; i += 1) {
        this.marketMs = i * BAR_MS
        this.applyTop(await this.tape.advance(this.marketMs), this.marketMs)
        this.recordBar()
      }
      this.tape.rebase(this.marketMs)
    } else if (this.data === 'reference') {
      const [history, now] = await Promise.all([fetchSolBars(BAR_HISTORY * BAR_MS, BAR_MS), fetchSolMid()])
      if (!history || !now) return
      const shift = now.cents - history[history.length - 1]
      for (let i = 0; i < history.length; i += 1) {
        this.marketMs = (i + 1) * BAR_MS
        this.referenceMid = this.align(history[i] + shift)
        this.quoteReference()
        this.recordBar()
      }
      this.source = now.source
    } else {
      return
    }
    const offset = this.marketMs
    for (const bar of this.bars) bar.ts -= offset
    if (this.lastTrade) this.lastTrade = { ...this.lastTrade, ts: this.lastTrade.ts - offset }
    this.marketMs = 0
  }

  barsView(limit: number): { market: string; barMs: number; bars: Bar[] } {
    return { market: this.instrument.symbol, barMs: BAR_MS, bars: this.bars.slice(-limit) }
  }

  private quoteReference(): void {
    const mid = this.referenceMid ?? this.mid
    this.mid = mid
    const spread = this.instrument.priceTickCents * 8
    this.quoteLadder(mid - spread, mid + spread)
    this.noteMid()
  }

  private quoteSynthetic(dtMs: number): MarketEvent[] {
    if (dtMs > 0) {
      // About one cent per 100ms on a $100 coin, so a 50ms-old touch is still on the spread.
      const shock = this.rng.normal() * 0.0001 * Math.sqrt(dtMs / 100)
      const next = Math.round(this.mid * Math.exp(shock))
      this.mid = this.align(Math.min(100_000_000, Math.max(100, next)))
    }
    const spread = this.instrument.priceTickCents * 8
    this.quoteLadder(this.mid - spread, this.mid + spread)
    const events: MarketEvent[] = []
    if (dtMs > 0 && this.rng.next() < 0.35) {
      const side: Side = this.rng.next() < 0.5 ? 'buy' : 'sell'
      events.push(...this.noise(side, 10 + this.rng.int(40)))
    }
    this.noteMid()
    return events
  }

  /** Ten levels so a max-size market order can fill and the page has a ladder. */
  private quoteLadder(touchBid: number, touchAsk: number): void {
    this.book.cancelAccount('external')
    const tick = this.instrument.priceTickCents
    const gap = tick * 10
    const perLevel = Math.max(500, Math.ceil(this.instrument.maxOrderLots / 4))
    const bid0 = this.align(touchBid)
    const ask0 = Math.max(this.align(touchAsk), bid0 + tick)
    for (let level = 0; level < BOOK_DEPTH; level += 1) {
      const bid = bid0 - level * gap
      const ask = ask0 + level * gap
      if (bid > 0) {
        this.book.rest({
          orderId: `ext-bid-${level}`,
          clientOrderId: `ext-bid-${level}`,
          account: 'external',
          side: 'buy',
          price: bid,
          remaining: perLevel,
        })
      }
      this.book.rest({
        orderId: `ext-ask-${level}`,
        clientOrderId: `ext-ask-${level}`,
        account: 'external',
        side: 'sell',
        price: ask,
        remaining: perLevel,
      })
    }
  }

  private replaceExternal(bid: number, bidSize: number, ask: number, askSize: number): void {
    this.book.cancelId('ext-bid')
    this.book.cancelId('ext-ask')
    if (bid > 0 && bidSize > 0) {
      this.book.rest({
        orderId: 'ext-bid',
        clientOrderId: 'ext-bid',
        account: 'external',
        side: 'buy',
        price: bid,
        remaining: bidSize,
      })
    }
    if (ask > 0 && askSize > 0 && ask > bid) {
      this.book.rest({
        orderId: 'ext-ask',
        clientOrderId: 'ext-ask',
        account: 'external',
        side: 'sell',
        price: ask,
        remaining: askSize,
      })
    }
  }

  /** Tape or a new quote ran through a resting pool order. The pool is the maker. */
  private async crossResting(): Promise<MarketEvent[]> {
    const events: MarketEvent[] = []
    const orders = this.book.ordersFor('pool')
    for (const order of orders) {
      const other = this.book.bestOther(order.side === 'buy' ? 'sell' : 'buy', 'pool')
      if (other === null) continue
      const crossed = order.side === 'buy' ? order.price >= other : order.price <= other
      if (!crossed) continue
      events.push(...(await this.fillRestingAsMaker(order)))
    }
    return events
  }

  private async fillRestingAsMaker(resting: RestingOrder): Promise<MarketEvent[]> {
    const removed = this.book.cancelId(resting.orderId)
    if (!removed) return []
    const result = this.book.match({
      orderId: resting.orderId,
      clientOrderId: resting.clientOrderId,
      account: 'pool',
      side: resting.side,
      limitPrice: resting.price,
      size: resting.remaining,
    })
    try {
      return await this.settleAggressive(resting.orderId, resting.clientOrderId, resting.side, resting.price, 'gtc', result, 'maker')
    } catch (error) {
      if (!(error instanceof ChainError)) throw error
      this.book.undo(result.canceled, result.fills.map((fill) => fill.maker))
      this.book.restore(removed)
      return []
    }
  }

  /** Takes size that is priced at or ahead of the external quote. Pool rests are makers. */
  private noise(side: Side, size: number): MarketEvent[] {
    const events: MarketEvent[] = []
    let left = size
    const opposite: Side = side === 'buy' ? 'sell' : 'buy'
    const external = this.book.bestOther(opposite, 'pool')
    const pool = this.book
      .ordersFor('pool')
      .filter((order) => order.side === opposite)
      .sort((a, b) => (side === 'buy' ? a.price - b.price : b.price - a.price))
    for (const order of pool) {
      if (left <= 0 || external === null) break
      const ahead = side === 'buy' ? order.price <= external : order.price >= external
      if (!ahead) break
      const qty = Math.min(left, order.remaining)
      order.remaining -= qty
      if (order.remaining === 0) this.book.cancelId(order.orderId)
      const fee = this.applyPool(order.side, order.price, qty, 'maker')
      events.push(this.recordFill(order.clientOrderId, order.orderId, order.side, order.price, qty, 'maker', null, fee))
      this.finishIfFlat(order.clientOrderId)
      this.lastTrade = { price: order.price, size: qty, side, ts: this.marketMs }
      events.push({ type: 'trade', price: order.price, size: qty, side, ts: this.marketMs })
      left -= qty
    }
    if (left > 0 && external !== null) {
      this.lastTrade = { price: external, size: left, side, ts: this.marketMs }
      events.push({ type: 'trade', price: external, size: left, side, ts: this.marketMs })
    }
    return events
  }

  private placeNow(input: Record<string, unknown>, cacheShape: boolean): ({ ok: true } & PlaceBody) | Failure {
    const extra = unknown(input, ORDER_FIELDS)
    if (extra) return extra
    const shape = shapeError(input, this.instrument)
    if (shape) {
      if (cacheShape && shape.clientOrderId && ID_RE.test(shape.clientOrderId)) {
        const prior = this.cache.get(shape.clientOrderId)
        if (prior?.type === 'reject') {
          return failure(400, 'bad_order', rejectReasonMessage(prior.reason), {
            clientOrderId: shape.clientOrderId,
            reason: prior.reason,
            duplicate: true,
          })
        }
        const reject: Cached = {
          type: 'reject',
          clientOrderId: shape.clientOrderId,
          reason: shape.reason,
          duplicate: false,
        }
        this.cache.set(shape.clientOrderId, reject)
      }
      return failure(400, 'bad_order', rejectReasonMessage(shape.reason), {
        clientOrderId: shape.clientOrderId,
        reason: shape.reason,
      })
    }
    const order = normalized(input, this.instrument)
    const blocked = this.tradingBlocked(order.clientOrderId)
    if (blocked) return blocked
    const cached = this.cache.get(order.clientOrderId)
    if (cached) {
      if (cached.type === 'reject') {
        return failure(400, 'bad_order', rejectReasonMessage(cached.reason), {
          clientOrderId: order.clientOrderId,
          reason: cached.reason,
          duplicate: true,
        })
      }
      return { ok: true, events: [{ ...cached, duplicate: true }], observation: this.observation() }
    }
    const risk = this.riskError(order)
    if (risk) {
      const reject: Cached = { type: 'reject', clientOrderId: order.clientOrderId, reason: risk, duplicate: false }
      this.cache.set(order.clientOrderId, reject)
      return failure(400, 'bad_order', rejectReasonMessage(risk), {
        clientOrderId: order.clientOrderId,
        reason: risk,
      })
    }
    const working = this.enqueuePlace(order)
    const ack = this.pendingAck(working)
    this.cache.set(order.clientOrderId, ack)
    return { ok: true, events: [ack], observation: this.observation() }
  }

  private tradingBlocked(clientOrderId: string): Failure | null {
    if (this.mode === 'live' && this.halted) {
      return failure(409, 'session_halted', 'The live session is halted.', { clientOrderId, reason: 'session_halted' })
    }
    if (this.mode === 'idle') {
      return failure(409, 'session_required', 'No training episode or live session is active.', {
        clientOrderId,
        reason: 'session_required',
      })
    }
    return null
  }

  private enqueuePlace(order: Normalized): WorkingOrder {
    const working: WorkingOrder = {
      orderId: `o${++this.orderSeq}`,
      clientOrderId: order.clientOrderId,
      side: order.side,
      price: order.price,
      size: order.size,
      type: order.type,
      tif: order.tif,
      releaseAt: this.marketMs + LATENCY_MS,
      status: 'pending',
    }
    this.working.set(order.clientOrderId, working)
    this.jobs.push({ seq: ++this.seq, releaseAt: working.releaseAt, kind: 'place', clientOrderId: order.clientOrderId })
    this.placedAt.push(this.marketMs)
    return working
  }

  /** Latest ack or reject for an id. A later place with that id returns this instead of a new order. */
  private store(event: MarketEvent): void {
    if (event.type === 'reject') {
      if (!event.clientOrderId || event.reason === 'unknown_order') return
      this.cache.set(event.clientOrderId, { ...event, duplicate: false })
      return
    }
    if (event.type === 'ack') this.cache.set(event.clientOrderId, { ...event, duplicate: false })
  }

  private pendingAck(working: WorkingOrder): Cached {
    return {
      type: 'ack',
      clientOrderId: working.clientOrderId,
      orderId: working.orderId,
      status: 'pending',
      duplicate: false,
    }
  }

  private enqueueCancel(clientOrderId: string): Cached | null {
    const working = this.working.get(clientOrderId)
    if (!working) return null
    this.jobs.push({
      seq: ++this.seq,
      releaseAt: this.marketMs + LATENCY_MS,
      kind: 'cancel',
      clientOrderId,
    })
    return {
      type: 'ack',
      clientOrderId,
      orderId: working.orderId,
      status: working.status,
      duplicate: false,
    }
  }

  private async releasePlace(clientOrderId: string): Promise<MarketEvent[]> {
    const working = this.working.get(clientOrderId)
    if (!working || working.status !== 'pending') return []
    if (working.type === 'post_only') {
      const other = this.book.bestOther(working.side === 'buy' ? 'sell' : 'buy', 'pool')
      const crosses =
        other !== null && working.price !== null && (working.side === 'buy' ? working.price >= other : working.price <= other)
      if (crosses) {
        this.working.delete(clientOrderId)
        const reject: MarketEvent = { type: 'reject', clientOrderId, reason: 'post_only_would_take', duplicate: false }
        this.store(reject)
        return [reject]
      }
    }
    const result = this.book.match({
      orderId: working.orderId,
      clientOrderId,
      account: 'pool',
      side: working.side,
      limitPrice: working.type === 'market' ? null : working.price,
      size: working.size,
    })
    const rests = working.type === 'limit' || working.type === 'post_only'
    const tif = working.type === 'ioc' || working.type === 'market' ? 'ioc' : working.tif
    try {
      return await this.settleAggressive(working.orderId, clientOrderId, working.side, working.price, rests ? tif : 'ioc', result, 'taker')
    } catch (error) {
      if (!(error instanceof ChainError)) throw error
      this.book.undo(result.canceled, result.fills.map((fill) => fill.maker))
      this.working.delete(clientOrderId)
      const reject: MarketEvent = { type: 'reject', clientOrderId, reason: 'chain_unavailable', duplicate: false }
      this.store(reject)
      return [reject]
    }
  }

  private async settleAggressive(
    orderId: string,
    clientOrderId: string,
    side: Side,
    price: number | null,
    tif: 'gtc' | 'ioc',
    result: { fills: MatchFill[]; canceled: RestingOrder[]; remaining: number },
    liquidity: 'maker' | 'taker',
  ): Promise<MarketEvent[]> {
    const signature = await this.chainSignature(side, result)
    const events: MarketEvent[] = []
    for (const canceled of result.canceled) {
      if (canceled.account === 'pool') {
        this.working.delete(canceled.clientOrderId)
        this.jobs = this.jobs.filter((job) => job.clientOrderId !== canceled.clientOrderId)
        const canceledAck: MarketEvent = {
          type: 'ack',
          clientOrderId: canceled.clientOrderId,
          orderId: canceled.orderId,
          status: 'canceled',
          duplicate: false,
        }
        this.store(canceledAck)
        events.push(canceledAck)
      }
    }
    let filled = 0
    for (const fill of result.fills) {
      if (fill.maker.account === 'pool') {
        const fee = this.applyPool(fill.maker.side, fill.price, fill.size, 'maker')
        events.push(this.recordFill(fill.maker.clientOrderId, fill.maker.orderId, fill.maker.side, fill.price, fill.size, 'maker', signature, fee))
        this.finishIfFlat(fill.maker.clientOrderId)
      }
      if (fill.takerAccount === 'pool') {
        const fee = this.applyPool(side, fill.price, fill.size, liquidity)
        events.push(this.recordFill(clientOrderId, orderId, side, fill.price, fill.size, liquidity, signature, fee))
        filled += fill.size
      }
      this.lastTrade = { price: fill.price, size: fill.size, side: fill.takerSide, ts: this.marketMs }
      events.push({ type: 'trade', price: fill.price, size: fill.size, side: fill.takerSide, ts: this.marketMs })
    }
    const remaining = result.remaining
    if (remaining > 0 && tif === 'gtc' && price !== null) {
      this.book.rest({
        orderId,
        clientOrderId,
        account: 'pool',
        side,
        price,
        remaining,
      })
      const working = this.working.get(clientOrderId)
      if (working) working.status = 'open'
      const openAck: MarketEvent = { type: 'ack', clientOrderId, orderId, status: 'open', duplicate: false }
      this.store(openAck)
      events.push(openAck)
    } else {
      this.working.delete(clientOrderId)
      const status = filled > 0 && remaining === 0 ? 'filled' : 'canceled'
      const doneAck: MarketEvent = { type: 'ack', clientOrderId, orderId, status, duplicate: false }
      this.store(doneAck)
      events.push(doneAck)
    }
    return events
  }

  private finishIfFlat(clientOrderId: string): void {
    const resting = this.book.ordersFor('pool').find((order) => order.clientOrderId === clientOrderId)
    if (resting) return
    const working = this.working.get(clientOrderId)
    if (!working || working.status === 'pending') return
    this.working.delete(clientOrderId)
  }

  private releaseCancel(clientOrderId: string): MarketEvent[] {
    return this.markCanceled(clientOrderId)
  }

  private markCanceled(clientOrderId: string): MarketEvent[] {
    const working = this.working.get(clientOrderId)
    if (!working) return [{ type: 'reject', clientOrderId, reason: 'unknown_order', duplicate: false }]
    if (working.status === 'open') {
      const resting = this.book.ordersFor('pool').find((order) => order.clientOrderId === clientOrderId)
      if (resting) this.book.cancelId(resting.orderId)
    } else {
      this.jobs = this.jobs.filter((job) => !(job.kind === 'place' && job.clientOrderId === clientOrderId))
    }
    this.working.delete(clientOrderId)
    const ack: MarketEvent = {
      type: 'ack',
      clientOrderId,
      orderId: working.orderId,
      status: 'canceled',
      duplicate: false,
    }
    this.store(ack)
    return [ack]
  }

  private async flattenNow(fromHalt: boolean): Promise<MarketEvent[]> {
    const events: MarketEvent[] = []
    for (const id of [...this.working.keys()]) events.push(...this.markCanceled(id))
    if (this.position === 0) return events
    const side: Side = this.position > 0 ? 'sell' : 'buy'
    const size = Math.abs(this.position)
    const orderId = `o${++this.orderSeq}`
    const result = this.book.match({
      orderId,
      clientOrderId: 'flatten',
      account: 'pool',
      side,
      limitPrice: null,
      size,
    })
    let settled: MarketEvent[]
    try {
      settled = await this.settleAggressive(orderId, 'flatten', side, null, 'ioc', result, 'taker')
    } catch (error) {
      if (!(error instanceof ChainError)) throw error
      this.book.undo(result.canceled, result.fills.map((fill) => fill.maker))
      events.push({ type: 'reject', clientOrderId: 'flatten', reason: 'chain_unavailable', duplicate: false })
      return events
    }
    events.push(
      ...settled.filter((event) => event.type !== 'ack' || event.clientOrderId !== 'flatten' || fromHalt),
    )
    if (!fromHalt) {
      const filled = result.fills.reduce((sum, fill) => sum + (fill.takerAccount === 'pool' ? fill.size : 0), 0)
      events.push({
        type: 'ack',
        clientOrderId: 'flatten',
        orderId,
        status: filled === size ? 'filled' : 'canceled',
        duplicate: false,
      })
    }
    return events
  }

  private async maybeHalt(): Promise<MarketEvent[]> {
    if (this.mode !== 'live' || this.halted || !this.session) return []
    const stake = this.stakeCents()
    if (stake <= this.session.floor) return this.halt('floor')
    if (stake >= this.session.ceiling) return this.halt('ceiling')
    return []
  }

  private async halt(reason: HaltReason): Promise<MarketEvent[]> {
    if (this.halted) return []
    const events = await this.flattenNow(true)
    if (events.some((event) => event.type === 'reject' && event.reason === 'chain_unavailable')) return events
    this.halted = true
    this.haltReason = reason
    this.haltAt = new Date().toISOString()
    this.frozenStake = this.stakeCents()
    this.frozenPnl = this.frozenStake - (this.session?.deposit ?? 0)
    this.rememberStake()
    return events
  }

  /** Books one pool fill and returns the fee charged for it, in whole cents. */
  private applyPool(side: Side, price: number, size: number, liquidity: 'maker' | 'taker'): number {
    const notion = notionalCents(price, size)
    const fee = this.feeFor(notion, liquidity)
    if (side === 'buy') this.cash -= notion + fee
    else this.cash += notion - fee
    const prev = this.position
    const sign = side === 'buy' ? 1 : -1
    if (prev === 0 || Math.sign(prev) === sign) {
      const next = prev + sign * size
      const prevAbs = Math.abs(prev)
      const nextAbs = Math.abs(next)
      this.avg = nextAbs === 0 ? 0 : Math.round((this.avg * prevAbs + price * size) / nextAbs)
      this.position = next
      this.realized -= fee
      return fee
    }
    const close = Math.min(size, Math.abs(prev))
    const pnl =
      side === 'sell'
        ? notionalCents(price, close) - notionalCents(this.avg, close)
        : notionalCents(this.avg, close) - notionalCents(price, close)
    this.realized += pnl - fee
    this.position = prev + sign * size
    if (this.position === 0) this.avg = 0
    else if (Math.sign(this.position) === sign) this.avg = price
    return fee
  }

  private async chainSignature(takerSide: Side, result: { fills: MatchFill[] }): Promise<string | null> {
    if (!this.chain || this.mode !== 'live') return null
    const legs: SettlementLeg[] = []
    for (const fill of result.fills) {
      const quoteCents = notionalCents(fill.price, fill.size)
      if (fill.takerAccount === 'pool') legs.push({ side: takerSide, lots: fill.size, quoteCents })
      else if (fill.maker.account === 'pool') legs.push({ side: fill.maker.side, lots: fill.size, quoteCents })
    }
    if (legs.length === 0) return null
    return this.chain.settle(legs)
  }

  private recordFill(
    clientOrderId: string,
    orderId: string,
    side: Side,
    price: number,
    size: number,
    liquidity: 'maker' | 'taker',
    signature: string | null,
    fee: number,
  ): MarketEvent {
    const fill: StoredFill = {
      clientOrderId,
      orderId,
      side,
      price,
      size,
      fee,
      liquidity,
      signature,
      ts: this.marketMs,
      wall: new Date().toISOString(),
    }
    this.fills.unshift(fill)
    if (this.fills.length > FILL_HISTORY) this.fills.pop()
    return {
      type: 'fill',
      clientOrderId: fill.clientOrderId,
      orderId: fill.orderId,
      side: fill.side,
      price: fill.price,
      size: fill.size,
      fee: fill.fee,
      liquidity: fill.liquidity,
      signature: fill.signature,
      ts: fill.ts,
    }
  }

  private equity(): number {
    if (this.position >= 0) return this.cash + notionalCents(this.mid, this.position)
    return this.cash - notionalCents(this.mid, -this.position)
  }

  private unrealized(): number {
    if (this.position > 0) return notionalCents(this.mid, this.position) - notionalCents(this.avg, this.position)
    if (this.position < 0) return notionalCents(this.avg, -this.position) - notionalCents(this.mid, -this.position)
    return 0
  }

  private stakeCents(): number {
    if (!this.session) return 0
    return this.session.deposit + (this.equity() - this.startEquity)
  }

  private rememberStake(): void {
    if (!this.session || this.mode !== 'live') return
    const point = { ts: new Date().toISOString(), stakeCents: this.frozenStake ?? this.stakeCents() }
    this.stakeHistory.push(point)
    if (this.stakeHistory.length > STAKE_HISTORY) this.stakeHistory.shift()
  }

  private noteMid(): void {
    this.mids.push(this.mid)
    if (this.mids.length > MID_HISTORY) this.mids.shift()
  }

  private riskError(order: Normalized): string | null {
    const windowStart = this.marketMs - RATE_WINDOW_MS
    const recent = this.placedAt.filter((ts) => ts > windowStart).length
    if (recent >= RATE_LIMIT) return 'rate_limit'
    const buyWorking = this.reserved('buy')
    const sellWorking = this.reserved('sell')
    if (order.side === 'buy' && this.position + buyWorking + order.size > this.instrument.maxPositionLots) {
      return 'position_limit'
    }
    if (order.side === 'sell' && this.position - sellWorking - order.size < -this.instrument.maxPositionLots) {
      return 'position_limit'
    }
    return null
  }

  private reserved(side: Side): number {
    let size = 0
    for (const order of this.working.values()) {
      if (order.side !== side) continue
      if (order.status === 'pending') size += order.size
      else {
        const resting = this.book.ordersFor('pool').find((item) => item.clientOrderId === order.clientOrderId)
        size += resting?.remaining ?? order.size
      }
    }
    return size
  }

  private openOrders(): OpenOrder[] {
    const orders: OpenOrder[] = []
    for (const order of this.working.values()) {
      const resting = this.book.ordersFor('pool').find((item) => item.clientOrderId === order.clientOrderId)
      orders.push({
        clientOrderId: order.clientOrderId,
        orderId: order.orderId,
        side: order.side,
        price: order.price,
        size: order.size,
        remaining: resting?.remaining ?? order.size,
        status: order.status,
        type: order.type,
      })
    }
    return orders
  }

  private align(cents: number): number {
    const tick = this.instrument.priceTickCents
    return Math.max(tick, Math.round(cents / tick) * tick)
  }
}

interface Normalized {
  clientOrderId: string
  side: Side
  size: number
  type: OrderType
  price: number | null
  tif: 'gtc' | 'ioc'
}

function normalized(input: OrderInput, instrument: Instrument): Normalized {
  const type = input.type as OrderType
  const tif = type === 'limit' && input.timeInForce === 'ioc' ? 'ioc' : 'gtc'
  return {
    clientOrderId: String(input.clientOrderId),
    side: input.side as Side,
    size: input.size as number,
    type,
    price: type === 'market' ? null : (input.price as number),
    tif,
  }
}

function shapeError(input: OrderInput, instrument: Instrument): { reason: string; clientOrderId: string } | null {
  const clientOrderId = typeof input.clientOrderId === 'string' ? input.clientOrderId : ''
  if (typeof input.clientOrderId !== 'string' || !ID_RE.test(input.clientOrderId)) {
    return { reason: 'bad_client_order_id', clientOrderId }
  }
  if (input.side !== 'buy' && input.side !== 'sell') return { reason: 'bad_side', clientOrderId }
  if (input.type !== 'limit' && input.type !== 'market' && input.type !== 'post_only' && input.type !== 'ioc') {
    return { reason: 'bad_type', clientOrderId }
  }
  if (!isInt(input.size) || input.size < 1 || input.size > instrument.maxOrderLots) {
    return { reason: 'bad_size', clientOrderId }
  }
  if (input.timeInForce !== undefined && input.timeInForce !== 'gtc' && input.timeInForce !== 'ioc') {
    return { reason: 'bad_type', clientOrderId }
  }
  if (input.type === 'market') {
    if (input.price !== undefined && input.price !== null) return { reason: 'price_forbidden', clientOrderId }
    return null
  }
  if (!isInt(input.price) || input.price < 1) return { reason: 'price_required', clientOrderId }
  if (input.price % instrument.priceTickCents !== 0) return { reason: 'bad_tick', clientOrderId }
  return null
}

function unknown(input: Record<string, unknown>, allowed: Set<string>): Failure | null {
  for (const key of Object.keys(input)) {
    if (!allowed.has(key)) return failure(400, 'unknown_field', `Unknown field ${key}.`)
  }
  return null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isInt(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value)
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

export function instrumentsBody(
  fees: ReturnType<Market['feeView']> = { model: 'exchange', makerBps: MAKER_BPS, takerBps: TAKER_BPS, solanaFeeCents: 0 },
  btcLive = false,
) {
  return {
    houseFundCents: HOUSE_CENTS,
    deposit: { minCents: DEPOSIT_MIN, maxCents: DEPOSIT_MAX },
    fees,
    instruments: INSTRUMENTS.map((item) => ({
      symbol: item.symbol,
      base: item.base,
      quote: item.quote,
      priceTickCents: item.priceTickCents,
      lotSize: item.lotSize,
      ...(item.symbol === 'BTC-USDT' && btcLive
        ? { data: 'live', sources: ['binance'] }
        : {
            data: item.data,
            ...(item.tape ? { tape: item.tape } : {}),
            ...(item.sources ? { sources: item.sources } : {}),
          }),
    })),
  }
}
