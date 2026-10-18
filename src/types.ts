export const HOUSE_CENTS = 100_000
export const DEPOSIT_MIN = 100
export const DEPOSIT_MAX = 10_000
// Trading fees, in basis points of each fill's notional. OFF (0) by default. Binance's regular-user futures rates are
// MAKER_BPS=2 TAKER_BPS=5; set those environment variables to bring them back. The bot is started with the same values.
function feeBps(name: string): number {
  const value = Number(process.env[name] ?? 0)
  return Number.isFinite(value) && value >= 0 ? value : 0
}
export const MAKER_BPS = feeBps('MAKER_BPS')
export const TAKER_BPS = feeBps('TAKER_BPS')
export const LATENCY_MS = 50
export const RATE_LIMIT = 20
export const RATE_WINDOW_MS = 1_000
export const MAX_DT_MS = 60_000
export const BOOK_DEPTH = 10
export const MID_HISTORY = 32
export const FILL_HISTORY = 50
export const STAKE_HISTORY = 180

export type Side = 'buy' | 'sell'
export type OrderType = 'limit' | 'market' | 'post_only' | 'ioc'
export type OrderStatus = 'pending' | 'open' | 'filled' | 'canceled' | 'rejected'
export type HaltReason = 'floor' | 'ceiling' | 'manual'
export type PriceSource = 'tape' | 'synthetic' | 'binance' | 'jupiter' | 'coinbase' | 'stale'
export type InstrumentSymbol = 'BTC-USDT' | 'SOL-USD'

export interface Instrument {
  symbol: InstrumentSymbol
  base: string
  quote: 'USD'
  priceTickCents: number
  lotSize: '0.001'
  maxOrderLots: number
  maxPositionLots: number
  data: 'replay' | 'live'
  tape?: string
  sources?: string[]
}

export const INSTRUMENTS: readonly Instrument[] = [
  {
    symbol: 'BTC-USDT',
    base: 'BTC',
    quote: 'USD',
    priceTickCents: 10,
    lotSize: '0.001',
    maxOrderLots: 10,
    maxPositionLots: 10,
    data: 'replay',
    tape: 'BTCUSDT_20261001',
  },
  {
    symbol: 'SOL-USD',
    base: 'SOL',
    quote: 'USD',
    priceTickCents: 1,
    lotSize: '0.001',
    maxOrderLots: 2_000,
    maxPositionLots: 10_000,
    data: 'live',
    sources: ['binance', 'jupiter', 'coinbase'],
  },
]

export function instrumentOf(symbol: string): Instrument | undefined {
  return INSTRUMENTS.find((item) => item.symbol === symbol)
}

export function notionalCents(priceCents: number, lots: number): number {
  return Math.round((priceCents * lots) / 1_000)
}

export function feeCents(notional: number, bps: number): number {
  return Math.floor((notional * bps) / 10_000)
}

export interface BookLevel {
  price: number
  size: number
}

export interface OpenOrder {
  clientOrderId: string
  orderId: string
  side: Side
  price: number | null
  size: number
  remaining: number
  status: 'pending' | 'open'
  type: OrderType
}

export interface UserFill {
  clientOrderId: string
  orderId: string
  side: Side
  price: number
  size: number
  fee: number
  liquidity: 'maker' | 'taker'
  ts: number
  signature: string | null
}

export interface TradePrint {
  price: number
  size: number
  side: Side
  ts: number
}

export interface Observation {
  market: string
  clock: 'step' | 'wall'
  ts: number
  mid: number
  priceSource: PriceSource
  bids: BookLevel[]
  asks: BookLevel[]
  lastTrade: TradePrint | null
  mids: number[]
  positionLots: number
  freeCashCents: number
  equityCents: number
  realizedPnlCents: number
  unrealizedPnlCents: number
  openOrders: OpenOrder[]
  halted: boolean
  haltReason: HaltReason | null
  stakeCents: number | null
  floorCents: number | null
  ceilingCents: number | null
  step: number
  done: boolean
}

export type MarketEvent =
  | {
      type: 'ack'
      clientOrderId: string
      orderId: string
      status: OrderStatus
      duplicate: boolean
    }
  | {
      type: 'reject'
      clientOrderId: string
      reason: string
      duplicate: boolean
    }
  | ({ type: 'fill' } & UserFill)
  | ({ type: 'trade' } & TradePrint)

export interface OrderInput {
  clientOrderId?: unknown
  side?: unknown
  size?: unknown
  type?: unknown
  price?: unknown
  timeInForce?: unknown
}

export interface HaltView {
  reason: HaltReason
  stakeCents: number
  poolPnlCents: number
  at: string
}

export interface SessionView {
  sessionId: string
  status: 'running' | 'halted'
  halt: HaltView | null
  instrument: InstrumentSymbol
  depositCents: number
  floorCents: number
  ceilingCents: number
  stakeCents: number
  poolPnlCents: number
  houseFundCents: number
  poolEquityCents: number
  positionLots: number
  freeCashCents: number
  realizedPnlCents: number
  unrealizedPnlCents: number
  midCents: number
  priceSource: PriceSource
  feed: 'ok' | 'stale'
  bids: BookLevel[]
  asks: BookLevel[]
  openOrders: OpenOrder[]
  fills: Array<{
    clientOrderId: string
    orderId: string
    side: Side
    price: number
    size: number
    fee: number
    liquidity: 'maker' | 'taker'
    ts: string
    signature: string | null
  }>
  stakeHistory: Array<{ ts: string; stakeCents: number }>
  startedAt: string
}

export function rejectReasonMessage(reason: string): string {
  switch (reason) {
    case 'session_halted':
      return 'The live session is halted.'
    case 'session_required':
      return 'No training episode or live session is active.'
    case 'bad_client_order_id':
      return 'clientOrderId must be 1-64 characters from [A-Za-z0-9._:-].'
    case 'bad_side':
      return 'side must be buy or sell.'
    case 'bad_type':
      return 'type must be limit, market, post_only, or ioc.'
    case 'bad_size':
      return 'size must be a positive integer at most the instrument maximum.'
    case 'price_required':
      return 'This order type requires a positive integer price.'
    case 'price_forbidden':
      return 'A market order cannot include a price.'
    case 'bad_tick':
      return 'Price is not a multiple of the instrument tick.'
    case 'position_limit':
      return 'The order would exceed the position limit.'
    case 'rate_limit':
      return 'Too many new orders in the last 1000ms of market time.'
    case 'post_only_would_take':
      return 'Post-only order would take liquidity.'
    case 'unknown_order':
      return 'No pending or open order has that id.'
    case 'chain_unavailable':
      return 'The local Solana validator did not accept the settlement.'
    default:
      return reason
  }
}
