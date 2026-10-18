import type { PriceSource } from './types.js'

const SOL_MINT = 'So11111111111111111111111111111111111111112'

export async function fetchSolMid(): Promise<{ cents: number; source: PriceSource } | null> {
  const attempts: Array<() => Promise<{ cents: number; source: PriceSource }>> = [
    async () => {
      const response = await fetch('https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT', {
        signal: AbortSignal.timeout(2_000),
      })
      if (!response.ok) throw new Error(`binance ${response.status}`)
      const body = (await response.json()) as { price?: string }
      return { cents: dollarsToCents(body.price), source: 'binance' }
    },
    async () => {
      const response = await fetch(`https://lite-api.jup.ag/price/v3?ids=${SOL_MINT}`, {
        signal: AbortSignal.timeout(2_000),
      })
      if (!response.ok) throw new Error(`jupiter ${response.status}`)
      const body = (await response.json()) as Record<string, { usdPrice?: number }>
      return { cents: dollarsToCents(body[SOL_MINT]?.usdPrice), source: 'jupiter' }
    },
    async () => {
      const response = await fetch('https://api.coinbase.com/v2/prices/SOL-USD/spot', {
        signal: AbortSignal.timeout(2_000),
      })
      if (!response.ok) throw new Error(`coinbase ${response.status}`)
      const body = (await response.json()) as { data?: { amount?: string } }
      return { cents: dollarsToCents(body.data?.amount), source: 'coinbase' }
    },
  ]
  for (const attempt of attempts) {
    try {
      const price = await attempt()
      if (price.cents > 0) return price
    } catch {
      continue
    }
  }
  return null
}

/**
 * The SOL-USD price over the last `spanMs`, as one mid in cents per `barMs`, oldest first.
 * Each bar holds the last Coinbase trade at or before its end. Null when Coinbase is
 * unreachable or its recent trades do not reach back far enough.
 */
export async function fetchSolBars(spanMs: number, barMs: number): Promise<number[] | null> {
  try {
    const response = await fetch('https://api.exchange.coinbase.com/products/SOL-USD/trades?limit=1000', {
      headers: { 'User-Agent': 'dubhacks26' },
      signal: AbortSignal.timeout(3_000),
    })
    if (!response.ok) return null
    const body = (await response.json()) as Array<{ time?: string; price?: string }>
    const trades = body
      .map((trade) => ({ t: Date.parse(trade.time ?? ''), cents: dollarsToCents(trade.price) }))
      .filter((trade) => Number.isFinite(trade.t) && trade.cents > 0)
      .sort((a, b) => a.t - b.t)
    const start = Date.now() - spanMs
    let next = 0
    let last: number | null = null
    while (next < trades.length && trades[next].t <= start) last = trades[next++].cents
    if (last === null) return null
    const bars: number[] = []
    for (let edge = start + barMs; bars.length < Math.floor(spanMs / barMs); edge += barMs) {
      while (next < trades.length && trades[next].t <= edge) last = trades[next++].cents
      bars.push(last)
    }
    return bars
  } catch {
    return null
  }
}

function dollarsToCents(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed) || parsed <= 0) return 0
  return Math.round(parsed * 100)
}
