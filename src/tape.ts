import { createReadStream, existsSync } from 'node:fs'
import { createInterface } from 'node:readline'
import type { Readable } from 'node:stream'
import { createGunzip } from 'node:zlib'
import type { Side } from './types.js'

export interface TopOfBook {
  bid: number
  bidSize: number
  ask: number
  askSize: number
  trade: { price: number; size: number; side: Side } | null
}

interface BookRow {
  tsUs: number
  bid: number
  bidSize: number
  ask: number
  askSize: number
}

interface TradeRow {
  tsUs: number
  price: number
  size: number
  side: Side
}

interface CsvCursor<T> {
  next: T | null
  pull: () => Promise<T | null>
  close: () => void
}

/**
 * Streams the local Tardis book-ticker and trades files. Market time 0 is the
 * first book-ticker timestamp. Callers advance an absolute market clock.
 */
export class BtcTape {
  private book: CsvCursor<BookRow> | null = null
  private trades: CsvCursor<TradeRow> | null = null
  private originUs = 0
  private cursorUs = 0
  private top: TopOfBook | null = null
  private lastTrade: TopOfBook['trade'] = null

  constructor(private readonly directory: string) {}

  get available(): boolean {
    return (
      existsSync(`${this.directory}/BTCUSDT_20261001_book_ticker.csv.gz`) &&
      existsSync(`${this.directory}/BTCUSDT_20261001_trades.csv.gz`)
    )
  }

  async open(): Promise<TopOfBook> {
    await this.close()
    this.book = openCsv(`${this.directory}/BTCUSDT_20261001_book_ticker.csv.gz`, parseBook)
    this.trades = openCsv(`${this.directory}/BTCUSDT_20261001_trades.csv.gz`, parseTrade)
    const first = await this.book.pull()
    if (!first) throw new Error('BTC book ticker is empty')
    this.originUs = first.tsUs
    this.cursorUs = first.tsUs
    this.lastTrade = null
    this.top = rowToTop(first, null)
    this.book.next = first
    this.trades.next = await this.trades.pull()
    return this.top
  }

  async advance(marketMs: number): Promise<TopOfBook> {
    if (!this.book || !this.trades || !this.top) throw new Error('BTC tape is not open')
    const target = this.originUs + marketMs * 1_000
    if (target < this.cursorUs) return { ...this.top, trade: null }
    let trade = this.lastTrade
    let printed = false
    while (this.trades.next && this.trades.next.tsUs <= target) {
      const row = this.trades.next
      trade = { price: row.price, size: row.size, side: row.side }
      printed = true
      this.trades.next = await this.trades.pull()
    }
    let book = this.top
    while (this.book.next && this.book.next.tsUs <= target) {
      book = rowToTop(this.book.next, trade)
      this.book.next = await this.book.pull()
    }
    this.cursorUs = target
    this.lastTrade = trade
    this.top = { ...book, trade }
    // Only a trade that printed during this advance, so a caller does not count one trade twice.
    return { ...this.top, trade: printed ? trade : null }
  }

  /** Move market time 0 to `marketMs` into the tape, so history before it can be played first. */
  rebase(marketMs: number): void {
    this.originUs += marketMs * 1_000
  }

  async close(): Promise<void> {
    this.book?.close()
    this.trades?.close()
    this.book = null
    this.trades = null
    this.top = null
  }
}

function rowToTop(row: BookRow, trade: TopOfBook['trade']): TopOfBook {
  return {
    bid: row.bid,
    bidSize: row.bidSize,
    ask: row.ask,
    askSize: row.askSize,
    trade,
  }
}

function openCsv<T>(path: string, parse: (line: string) => T | null): CsvCursor<T> {
  const input = createReadStream(path).pipe(createGunzip())
  const lines = createInterface({ input, crlfDelay: Infinity })
  const iterator = lines[Symbol.asyncIterator]()
  let header = true
  const cursor: CsvCursor<T> = {
    next: null,
    async pull() {
      while (true) {
        const step = await iterator.next()
        if (step.done) return null
        const line = String(step.value)
        if (header) {
          header = false
          continue
        }
        const parsed = parse(line)
        if (parsed) return parsed
      }
    },
    close() {
      lines.close()
      ;(input as Readable).destroy()
    },
  }
  return cursor
}

function parseBook(line: string): BookRow | null {
  const parts = line.split(',')
  if (parts.length < 8) return null
  const tsUs = Number(parts[2])
  const askSize = lots(parts[4] ?? '')
  const ask = cents(parts[5] ?? '')
  const bid = cents(parts[6] ?? '')
  const bidSize = lots(parts[7] ?? '')
  if (!Number.isFinite(tsUs) || bid <= 0 || ask <= 0) return null
  return { tsUs, bid, bidSize, ask, askSize }
}

function parseTrade(line: string): TradeRow | null {
  const parts = line.split(',')
  if (parts.length < 8) return null
  const tsUs = Number(parts[2])
  const side = parts[5] === 'buy' || parts[5] === 'sell' ? parts[5] : null
  const price = cents(parts[6] ?? '')
  const size = lots(parts[7] ?? '')
  if (!side || !Number.isFinite(tsUs) || price <= 0 || size <= 0) return null
  return { tsUs, price, size, side }
}

function cents(raw: string): number {
  const value = Number(raw)
  if (!Number.isFinite(value)) return 0
  return Math.round(value * 100)
}

function lots(raw: string): number {
  const value = Number(raw)
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.round(value / 0.001))
}
