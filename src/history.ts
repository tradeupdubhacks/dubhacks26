const INTERVALS = ['1m', '5m', '15m', '1h', '4h', '1d'] as const
type Interval = (typeof INTERVALS)[number]
type Source = 'binance' | 'coinbase' | 'kraken'

const COINBASE_GRANULARITY: Record<Interval, number | null> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  '4h': null,
  '1d': 86400,
}

const KRAKEN_INTERVAL: Record<Interval, number> = {
  '1m': 1,
  '5m': 5,
  '15m': 15,
  '1h': 60,
  '4h': 240,
  '1d': 1440,
}

export interface HistoryQuery {
  symbol?: string
  interval?: string
  limit?: string
}

export interface HistoryResult {
  status: number
  body: unknown
}

interface Candle {
  openTime: string
  openCents: number
  highCents: number
  lowCents: number
  closeCents: number
  open: string
  high: string
  low: string
  close: string
  volume: string
}

type Attempt = { ok: true; candles: Candle[] } | { ok: false; kind: 'unknown' | 'unavailable' }

/**
 * Public candles for a coin the page may chart. The session can still only
 * trade a symbol from GET /v1/instruments.
 * Binance is tried first. A regional block falls through to Coinbase, then Kraken.
 */
export async function loadHistory(query: HistoryQuery, fetchImpl: typeof fetch = fetch): Promise<HistoryResult> {
  const base = coinBase(query.symbol)
  if (!base) {
    return error(400, 'bad_symbol', 'Symbol must be a coin ticker, such as ETH or SOL-USD.')
  }
  const interval = query.interval === undefined || query.interval === '' ? '1h' : query.interval
  if (!isInterval(interval)) {
    return error(400, 'bad_interval', 'Interval must be one of 1m, 5m, 15m, 1h, 4h, 1d.')
  }
  const limit = query.limit === undefined || query.limit === '' ? 100 : Number(query.limit)
  if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
    return error(400, 'bad_limit', 'Limit must be an integer from 1 to 500.')
  }

  const attempts: Array<{ source: Source; run: () => Promise<Attempt> }> = [
    { source: 'binance', run: () => fromBinance(base, interval, limit, fetchImpl) },
    { source: 'coinbase', run: () => fromCoinbase(base, interval, limit, fetchImpl) },
    { source: 'kraken', run: () => fromKraken(base, interval, limit, fetchImpl) },
  ]
  let unknown = 0
  let unavailable = 0
  for (const attempt of attempts) {
    const result = await attempt.run()
    if (result.ok) {
      return {
        status: 200,
        body: {
          symbol: base,
          market: `${base}-USD`,
          interval,
          source: attempt.source,
          candles: result.candles,
        },
      }
    }
    if (result.kind === 'unknown') unknown += 1
    else unavailable += 1
  }
  if (unknown > 0 && unavailable === 0) {
    return error(404, 'unknown_symbol', 'No USD market was found for that coin.')
  }
  return error(502, 'history_unavailable', 'Historical prices are unavailable right now.')
}

async function fromBinance(base: string, interval: Interval, limit: number, fetchImpl: typeof fetch): Promise<Attempt> {
  const url = new URL('https://api.binance.com/api/v3/klines')
  url.searchParams.set('symbol', `${base}USDT`)
  url.searchParams.set('interval', interval)
  url.searchParams.set('limit', String(limit))
  const response = await readJson(url, fetchImpl)
  if (!response.ok) return response
  if (response.status === 400) return { ok: false, kind: 'unknown' }
  if (!Array.isArray(response.body)) return { ok: false, kind: 'unavailable' }
  return { ok: true, candles: take(response.body.flatMap(binanceCandle), limit) }
}

async function fromCoinbase(base: string, interval: Interval, limit: number, fetchImpl: typeof fetch): Promise<Attempt> {
  const granularity = COINBASE_GRANULARITY[interval]
  if (granularity === null) return { ok: false, kind: 'unavailable' }
  const url = new URL(`https://api.exchange.coinbase.com/products/${base}-USD/candles`)
  url.searchParams.set('granularity', String(granularity))
  const response = await readJson(url, fetchImpl)
  if (!response.ok) return response
  if (response.status === 404) return { ok: false, kind: 'unknown' }
  if (!Array.isArray(response.body)) return { ok: false, kind: 'unavailable' }
  return { ok: true, candles: take(response.body.flatMap(coinbaseCandle), limit) }
}

async function fromKraken(base: string, interval: Interval, limit: number, fetchImpl: typeof fetch): Promise<Attempt> {
  const pairs = base === 'BTC' ? ['XBTUSD', 'BTCUSD'] : [`${base}USD`]
  let unavailable = false
  for (const pair of pairs) {
    const url = new URL('https://api.kraken.com/0/public/OHLC')
    url.searchParams.set('pair', pair)
    url.searchParams.set('interval', String(KRAKEN_INTERVAL[interval]))
    const response = await readJson(url, fetchImpl)
    if (!response.ok) {
      unavailable = true
      continue
    }
    if (!response.body || typeof response.body !== 'object' || Array.isArray(response.body)) {
      unavailable = true
      continue
    }
    const body = response.body as { error?: unknown; result?: unknown }
    const errors = Array.isArray(body.error) ? body.error.map(String) : []
    if (errors.some((item) => item.includes('Unknown asset pair'))) continue
    if (errors.length > 0 || !body.result || typeof body.result !== 'object') {
      unavailable = true
      continue
    }
    const rows = Object.entries(body.result as Record<string, unknown>).find(([key, value]) => key !== 'last' && Array.isArray(value))
    if (!rows) return { ok: false, kind: 'unknown' }
    return { ok: true, candles: take((rows[1] as unknown[]).flatMap(krakenCandle), limit) }
  }
  return unavailable ? { ok: false, kind: 'unavailable' } : { ok: false, kind: 'unknown' }
}

async function readJson(
  url: URL,
  fetchImpl: typeof fetch,
): Promise<{ ok: true; status: number; body: unknown } | { ok: false; kind: 'unavailable' }> {
  try {
    const response = await fetchImpl(url, { signal: AbortSignal.timeout(5_000) })
    const body: unknown = await response.json()
    return { ok: true, status: response.status, body }
  } catch {
    return { ok: false, kind: 'unavailable' }
  }
}

function binanceCandle(row: unknown): Candle[] {
  if (!Array.isArray(row) || row.length < 6) return []
  return candle(row[0], row[1], row[2], row[3], row[4], row[5])
}

function coinbaseCandle(row: unknown): Candle[] {
  if (!Array.isArray(row) || row.length < 6) return []
  return candle(row[0], row[3], row[2], row[1], row[4], row[5])
}

function krakenCandle(row: unknown): Candle[] {
  if (!Array.isArray(row) || row.length < 7) return []
  return candle(row[0], row[1], row[2], row[3], row[4], row[6])
}

function candle(time: unknown, open: unknown, high: unknown, low: unknown, close: unknown, volume: unknown): Candle[] {
  const openText = priceString(open)
  const highText = priceString(high)
  const lowText = priceString(low)
  const closeText = priceString(close)
  const volumeText = priceString(volume)
  const millis = openMillis(time)
  if (!openText || !highText || !lowText || !closeText || !volumeText || millis === null) return []
  return [{
    openTime: new Date(millis).toISOString(),
    openCents: toCents(openText),
    highCents: toCents(highText),
    lowCents: toCents(lowText),
    closeCents: toCents(closeText),
    open: openText,
    high: highText,
    low: lowText,
    close: closeText,
    volume: volumeText,
  }]
}

function take(candles: Candle[], limit: number): Candle[] {
  return candles
    .sort((left, right) => left.openTime.localeCompare(right.openTime))
    .slice(-limit)
}

function coinBase(symbol: string | undefined): string | null {
  if (symbol === undefined) return null
  const raw = symbol.trim().toUpperCase()
  if (!/^[A-Z0-9]{1,20}(?:-USD|-USDT|USDT|USD)?$/.test(raw)) return null
  let base = raw
  if (base.endsWith('-USDT')) base = base.slice(0, -5)
  else if (base.endsWith('-USD')) base = base.slice(0, -4)
  else if (base.endsWith('USDT') && base.length > 4) base = base.slice(0, -4)
  else if (base.endsWith('USD') && base.length > 3) base = base.slice(0, -3)
  if (!/^[A-Z0-9]{1,20}$/.test(base)) return null
  return base
}

function isInterval(value: string): value is Interval {
  return (INTERVALS as readonly string[]).includes(value)
}

function openMillis(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed) || parsed <= 0) return null
  return parsed < 1e12 ? parsed * 1000 : parsed
}

function priceString(value: unknown): string | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null
  const text = String(value)
  if (!/^\d+(?:\.\d+)?$/.test(text) || text.length > 40) return null
  return text
}

function toCents(value: string): number {
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed < 0) return 0
  return Math.round(parsed * 100)
}

function error(status: number, code: string, message: string): HistoryResult {
  return { status, body: { error: { code, message } } }
}
