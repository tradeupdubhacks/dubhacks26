import type { BookLevel, Side } from './types.js'

export interface RestingOrder {
  orderId: string
  clientOrderId: string
  account: 'pool' | 'external'
  side: Side
  price: number
  remaining: number
}

export interface MatchFill {
  price: number
  size: number
  maker: RestingOrder
  takerAccount: 'pool' | 'external'
  takerSide: Side
  takerClientOrderId: string
  takerOrderId: string
}

export interface MatchResult {
  fills: MatchFill[]
  canceled: RestingOrder[]
  remaining: number
}

interface Incoming {
  orderId: string
  clientOrderId: string
  account: 'pool' | 'external'
  side: Side
  limitPrice: number | null
  size: number
}

export class OrderBook {
  private bids = new Map<number, RestingOrder[]>()
  private asks = new Map<number, RestingOrder[]>()
  private bidPrices: number[] = []
  private askPrices: number[] = []
  private byId = new Map<string, RestingOrder>()

  clear(): void {
    this.bids.clear()
    this.asks.clear()
    this.bidPrices = []
    this.askPrices = []
    this.byId.clear()
  }

  rest(order: RestingOrder): void {
    this.insertLevel(order.side, order.price)
    this.queue(order.side, order.price).push(order)
    this.byId.set(order.orderId, order)
  }

  /** Add `order.remaining` back, or rest the order again if the fill removed it. */
  restore(order: RestingOrder): void {
    const live = this.byId.get(order.orderId)
    if (!live) {
      this.rest({ ...order })
      return
    }
    live.remaining += order.remaining
  }

  undo(canceled: RestingOrder[], filled: RestingOrder[]): void {
    for (const order of canceled) this.restore(order)
    for (const order of filled) this.restore(order)
  }

  cancelId(orderId: string): RestingOrder | undefined {
    const order = this.byId.get(orderId)
    if (!order) return undefined
    this.remove(order)
    return order
  }

  cancelAccount(account: 'pool' | 'external'): RestingOrder[] {
    const removed: RestingOrder[] = []
    for (const order of this.byId.values()) {
      if (order.account === account) removed.push(order)
    }
    for (const order of removed) this.remove(order)
    return removed
  }

  ordersFor(account: 'pool' | 'external'): RestingOrder[] {
    return [...this.byId.values()].filter((order) => order.account === account)
  }

  depth(side: Side, limit: number): BookLevel[] {
    const prices = side === 'buy' ? this.bidPrices : this.askPrices
    const out: BookLevel[] = []
    for (const price of prices) {
      if (out.length === limit) break
      let size = 0
      for (const order of this.queue(side, price)) size += order.remaining
      if (size > 0) out.push({ price, size })
    }
    return out
  }

  /** Best price of liquidity that is not the given account. */
  bestOther(side: Side, account: 'pool' | 'external'): number | null {
    const prices = side === 'buy' ? this.bidPrices : this.askPrices
    for (const price of prices) {
      if (this.queue(side, price).some((order) => order.account !== account && order.remaining > 0)) {
        return price
      }
    }
    return null
  }

  match(incoming: Incoming): MatchResult {
    const fills: MatchFill[] = []
    const canceled: RestingOrder[] = []
    let remaining = incoming.size
    const bookSide: Side = incoming.side === 'buy' ? 'sell' : 'buy'

    while (remaining > 0) {
      const prices = bookSide === 'buy' ? this.bidPrices : this.askPrices
      if (prices.length === 0) break
      const price = prices[0]!
      if (incoming.limitPrice !== null) {
        if (incoming.side === 'buy' && price > incoming.limitPrice) break
        if (incoming.side === 'sell' && price < incoming.limitPrice) break
      }
      const queue = this.queue(bookSide, price)
      const resting = queue[0]
      if (!resting) {
        this.dropEmpty(bookSide, price)
        continue
      }
      if (resting.account === incoming.account) {
        canceled.push({ ...resting })
        this.remove(resting)
        continue
      }
      const size = Math.min(remaining, resting.remaining)
      const maker = { ...resting, remaining: size }
      resting.remaining -= size
      remaining -= size
      fills.push({
        price,
        size,
        maker,
        takerAccount: incoming.account,
        takerSide: incoming.side,
        takerClientOrderId: incoming.clientOrderId,
        takerOrderId: incoming.orderId,
      })
      if (resting.remaining === 0) this.remove(resting)
    }

    return { fills, canceled, remaining }
  }

  private queue(side: Side, price: number): RestingOrder[] {
    return (side === 'buy' ? this.bids : this.asks).get(price) ?? []
  }

  private insertLevel(side: Side, price: number): void {
    const book = side === 'buy' ? this.bids : this.asks
    if (book.has(price)) return
    const prices = side === 'buy' ? this.bidPrices : this.askPrices
    let index = 0
    if (side === 'buy') {
      while (index < prices.length && prices[index]! > price) index += 1
    } else {
      while (index < prices.length && prices[index]! < price) index += 1
    }
    prices.splice(index, 0, price)
    book.set(price, [])
  }

  private remove(order: RestingOrder): void {
    this.byId.delete(order.orderId)
    const book = order.side === 'buy' ? this.bids : this.asks
    const queue = book.get(order.price)
    if (!queue) return
    const index = queue.findIndex((item) => item.orderId === order.orderId)
    if (index >= 0) queue.splice(index, 1)
    if (queue.length === 0) this.dropEmpty(order.side, order.price)
  }

  private dropEmpty(side: Side, price: number): void {
    const book = side === 'buy' ? this.bids : this.asks
    const prices = side === 'buy' ? this.bidPrices : this.askPrices
    book.delete(price)
    const index = prices.indexOf(price)
    if (index >= 0) prices.splice(index, 1)
  }
}
