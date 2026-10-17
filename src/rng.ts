export interface Rng {
  next(): number
  int(max: number): number
  normal(): number
}

export function mulberry32(seed: number): Rng {
  let state = seed >>> 0
  const next = (): number => {
    state = (state + 0x6d2b79f5) | 0
    let t = Math.imul(state ^ (state >>> 15), 1 | state)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4_294_967_296
  }
  return {
    next,
    int(max: number) {
      return Math.floor(next() * max)
    },
    normal() {
      let u = 0
      let v = 0
      while (u === 0) u = next()
      while (v === 0) v = next()
      return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v)
    },
  }
}
