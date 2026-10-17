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

function dollarsToCents(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed) || parsed <= 0) return 0
  return Math.round(parsed * 100)
}
