import { cents, lots, type LiveBtcSource, type TopOfBook } from './tape.js'

/** Binance USD-M BTCUSDT best bid/ask and trades. The same feed the BTC tape was recorded from. */
export const BINANCE_BTC_STREAM = 'wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/btcusdt@trade'

type Book = Omit<TopOfBook, 'trade'>
type Trade = NonNullable<TopOfBook['trade']>

/**
 * Live BTC-USDT book for live sessions. It connects once when the server starts, reconnects on
 * its own, and samples the book every `sampleMs` so a new session can be handed the last two
 * minutes as history. Prices are cents per coin and sizes are 0.001 BTC lots, like the tape.
 */
export class BinanceBtcFeed implements LiveBtcSource {
  private socket: WebSocket | null = null
  private book: Book | null = null
  private sinceAdvance: Trade | null = null
  private sinceSample: Trade | null = null
  private samples: TopOfBook[] = []
  private lastMessageMs = 0
  private sampler: ReturnType<typeof setInterval> | null = null
  private retry: ReturnType<typeof setTimeout> | null = null
  private stopped = false

  constructor(
    private readonly url = BINANCE_BTC_STREAM,
    private readonly sampleMs = 200,
    private readonly keep = 601,
    private readonly staleMs = 5_000,
  ) {}

  start(): void {
    this.stopped = false
    this.connect()
    this.sampler = setInterval(() => this.sample(), this.sampleMs)
  }

  shutdown(): void {
    this.stopped = true
    if (this.sampler) clearInterval(this.sampler)
    if (this.retry) clearTimeout(this.retry)
    this.socket?.close()
    this.socket = null
  }

  get available(): boolean {
    return this.book !== null && !this.stale
  }

  get stale(): boolean {
    return Date.now() - this.lastMessageMs > this.staleMs
  }

  async open(): Promise<TopOfBook> {
    if (!this.book) throw new Error('Live BTC prices have not arrived yet')
    this.sinceAdvance = null
    return { ...this.book, trade: null }
  }

  async advance(_marketMs: number): Promise<TopOfBook> {
    if (!this.book) throw new Error('Live BTC prices have not arrived yet')
    const trade = this.sinceAdvance
    this.sinceAdvance = null
    return { ...this.book, trade }
  }

  /** Live time cannot be moved; there is nothing to rebase. */
  rebase(_marketMs: number): void {}

  /** A session ending does not stop the feed. `shutdown` does. */
  async close(): Promise<void> {}

  history(count: number): TopOfBook[] {
    return this.samples.slice(-count)
  }

  /** Exposed for tests: apply one raw combined-stream message. */
  receive(raw: string): void {
    let message: { data?: Record<string, unknown> }
    try {
      message = JSON.parse(raw) as { data?: Record<string, unknown> }
    } catch {
      return
    }
    const data = message.data
    if (!data) return
    if (data.e === 'bookTicker') {
      const bid = cents(String(data.b))
      const ask = cents(String(data.a))
      if (bid <= 0 || ask <= bid) return
      this.book = { bid, bidSize: lots(String(data.B)), ask, askSize: lots(String(data.A)) }
      this.lastMessageMs = Date.now()
    } else if (data.e === 'trade' && (data.X === undefined || data.X === 'MARKET')) {
      const price = cents(String(data.p))
      const size = lots(String(data.q))
      if (price <= 0 || size <= 0) return
      // `m` is true when the buyer was the maker, so the aggressor sold.
      const trade: Trade = { price, size, side: data.m ? 'sell' : 'buy' }
      this.sinceAdvance = trade
      this.sinceSample = trade
      this.lastMessageMs = Date.now()
    }
  }

  private sample(): void {
    if (!this.book || this.stale) return
    this.samples.push({ ...this.book, trade: this.sinceSample })
    this.sinceSample = null
    if (this.samples.length > this.keep) this.samples.splice(0, this.samples.length - this.keep)
  }

  private connect(): void {
    if (this.stopped) return
    let socket: WebSocket
    try {
      socket = new WebSocket(this.url)
    } catch {
      this.retry = setTimeout(() => this.connect(), 2_000)
      return
    }
    this.socket = socket
    socket.onmessage = (event) => this.receive(String(event.data))
    socket.onerror = () => {}
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null
      if (!this.stopped) this.retry = setTimeout(() => this.connect(), 2_000)
    }
  }
}
