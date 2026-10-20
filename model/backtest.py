"""Session backtest on the BTC replay, using the model API's rules (docs/model-api.md, src/market.ts).

Two engines that must agree:
  run_session  - event driven and vectorised; used for grids over many sessions.
  StepEnv      - steps a 200 ms live tick, calls the real decide_session each step; used to verify run_session.

Replay rules mirrored from the server: top of book only (book_ticker); a market-time delay of 50 ms before an order
acts; IOC/limit orders match the external touch at the touch price; a resting limit fills, at the external price,
when the quote crosses it, checked at the end of each time slice; fees 2 bps maker / 5 bps taker rounded down to a
cent; the session halts when stake = deposit + pool PnL reaches the floor or ceiling and flattens at the touch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from model.features_session import session_features
from model.policy import IdSource, InstrumentLimits, round_tick
from model.session_policy import SessionParams, SessionState, decide_session
from model.sizing import plan_trade

GRID = Path(__file__).resolve().parent.parent / "hft" / "data" / "out" / "BTCUSDT_20261001_grid50ms.npz"
SUB = 4                      # 50 ms cells per 200 ms live tick
TICK = 10
LIMITS = InstrumentLimits("BTC-USDT", TICK, 10, 10)
from model.sizing import MAKER_BPS, TAKER_BPS  # noqa: E402

FEES = {"taker": TAKER_BPS, "maker": MAKER_BPS}      # bps, same source as the server (env, 0 by default)


def fee_cents(price: int, lots: int, bps: float) -> int:
    notional = int(round(price * lots / 1000))
    return int(notional * bps // 10_000)


class Tape:
    def __init__(self, path: Path = GRID) -> None:
        z = np.load(path)
        self.bid, self.ask = z["bid"], z["ask"]
        self.bidsz, self.asksz = z["bidsz"], z["asksz"]
        self.tpx, self.tsz, self.tside = z["tpx"], z["tsz"], z["tside"]
        self.n200 = len(self.bid) // SUB
        self.mid50 = (self.bid + self.ask + 1) // 2
        self.mid200 = self.mid50[::SUB]

    def observation(self, j: int, i: int | None = None) -> dict:
        """Observation at 200 ms tick j (or 50 ms cell i). Pool fields are filled by the caller."""
        i = SUB * j if i is None else i
        start = i - SUB * min(31, i // SUB)
        mids = [int(v) for v in self.mid50[start: i + 1: SUB]]
        trade = None
        if self.tsz[i] > 0:
            trade = {"price": int(self.tpx[i]), "size": int(self.tsz[i]), "side": "buy" if self.tside[i] else "sell", "ts": 0}
        return {
            "market": "BTC-USDT", "priceSource": "tape", "ts": i * 50, "mid": int(self.mid50[i]), "mids": mids,
            "bids": [{"price": int(self.bid[i]), "size": int(self.bidsz[i])}],
            "asks": [{"price": int(self.ask[i]), "size": int(self.asksz[i])}],
            "lastTrade": trade,
        }


def training_features(tape: Tape) -> np.ndarray:
    """session_features at every 200 ms tick, built from the same observation payload the live bot reads."""
    rows = []
    for j in range(tape.n200):
        f = session_features(tape.observation(j))
        rows.append(f if f is not None else [0.0] * 11)
    return np.asarray(rows, dtype=np.float32)


# ----------------------------------------------------------------------------- event-driven engine
@dataclass
class Outcome:
    result: str            # ceiling | floor | timeout
    stake: int
    trades: int
    j_end: int


def _tp_price(side: int, mid0: float, unreal0: float, pos: int, tp_bps: float) -> int:
    average = mid0 - unreal0 * 1000.0 / pos
    target = average * (1 + side * tp_bps / 1e4)
    return round_tick(int(round(target)), TICK, "sell" if side > 0 else "buy")


def run_session(tape: Tape, conf: np.ndarray, vol: np.ndarray, direction: np.ndarray, deposit: int, floor: int,
                ceiling: int, j0: int, j_stop: int, P: SessionParams) -> Outcome:
    """Run one session from tick j0; the data window ends at tick j_stop (exclusive)."""
    stake, j, trades = deposit, j0, 0
    ok = (conf >= P.threshold) & (vol >= P.vol_gate)
    cand = np.flatnonzero(ok)
    mid200, bid200, ask200 = tape.mid200, tape.bid[::SUB], tape.ask[::SUB]
    while True:
        k = np.searchsorted(cand, j)
        if k >= len(cand) or cand[k] >= j_stop - 2:
            return Outcome("timeout", stake, trades, j_stop)
        je = int(cand[k]); mid = int(mid200[je])
        plan = plan_trade(stake, floor, ceiling, mid, LIMITS.max_position_lots, P.take_profit_bps, P.stop_bps)
        if plan is None:
            return Outcome("timeout", stake, trades, j_stop)
        side = 1 if direction[je] > 0 else -1
        lots_req = min(plan.lots, LIMITS.max_order_lots)
        cap = int(ask200[je]) + P.entry_slip_ticks * TICK if side > 0 else int(bid200[je]) - P.entry_slip_ticks * TICK
        r = SUB * je + 1
        px = int(tape.ask[r]) if side > 0 else int(tape.bid[r])
        if (side > 0 and px > cap) or (side < 0 and px < cap):
            j = je + 1 + P.cooldown_steps; continue
        sz = min(lots_req, int(tape.asksz[r] if side > 0 else tape.bidsz[r]))
        if sz < 1:
            j = je + 1 + P.cooldown_steps; continue
        fee_e = fee_cents(px, sz, FEES["taker"])
        trades += 1
        bs = np.arange(je + 1, min(je + 1 + P.max_hold_steps, j_stop))
        if len(bs) == 0:
            return Outcome("timeout", stake - fee_e, trades, j_stop)
        mid_b = mid200[bs].astype(np.float64)
        unreal = np.rint(side * sz * (mid_b - px) / 1000.0)
        stake_m = stake - fee_e + unreal
        stop_c = sz * (mid_b / 1000.0) * P.stop_bps / 1e4
        n = len(bs)
        INF = n + 10
        k_stop = int(np.argmax(unreal <= -stop_c)) if (unreal <= -stop_c).any() else INF
        k_time = P.max_hold_steps - 1 if P.max_hold_steps - 1 < n else INF
        k_pol = min(k_stop, k_time)
        k_tp, tp_taker = INF, False
        if k_pol > 0:
            tp = _tp_price(side, mid_b[0], unreal[0], side * sz, P.take_profit_bps)
            rr = SUB * int(bs[0]) + 1
            if (side > 0 and tape.bid[rr] >= tp) or (side < 0 and tape.ask[rr] <= tp):
                k_tp, tp_taker = 0, True
            else:
                ext = tape.bid[SUB * bs] if side > 0 else tape.ask[SUB * bs]
                hit = (ext >= tp) if side > 0 else (ext <= tp)
                hit[0] = False
                if hit.any():
                    k_tp = int(np.argmax(hit))
        k_halt = INF
        hmask = (stake_m <= floor) | (stake_m >= ceiling)
        if hmask.any():
            k_halt = int(np.argmax(hmask))
        # earliest event: at the same tick TP (phase 0) < halt (1) < policy exit (2)
        events = sorted([(k_tp, 0), (k_halt, 1), (k_pol, 2)])
        kind_k, phase = events[0]
        if kind_k >= INF:
            # no exit before the window ends: mark to market
            return Outcome("timeout", int(stake_m[-1]), trades, j_stop)
        if phase == 0:
            if tp_taker:
                fp, fe, jx = int(tape.bid[rr] if side > 0 else tape.ask[rr]), FEES["taker"], int(bs[0])
            else:
                fp, fe, jx = int(ext[kind_k]), FEES["maker"], int(bs[kind_k])
            stake = stake - fee_e + int(round(side * sz * (fp - px) / 1000.0)) - fee_cents(fp, sz, fe)
            nxt = jx + P.cooldown_steps
            if stake >= ceiling:
                return Outcome("ceiling", stake, trades, jx)
            if stake <= floor:
                return Outcome("floor", stake, trades, jx)
            j = nxt
            continue
        jx = int(bs[kind_k])
        fp = int(bid200[jx]) if side > 0 else int(ask200[jx])
        stake = stake - fee_e + int(round(side * sz * (fp - px) / 1000.0)) - fee_cents(fp, sz, FEES["taker"])
        if phase == 1:
            if stake_m[kind_k] >= ceiling:
                return Outcome("ceiling", stake, trades, jx)
            return Outcome("floor", stake, trades, jx)
        if stake <= floor:
            return Outcome("floor", stake, trades, jx)
        j = jx + 1 + P.cooldown_steps


# ----------------------------------------------------------------------------- step-by-step engine
class StepEnv:
    """A small model-API server on the same replay: place, ioc/limit, flatten, cancel_all, fees, 50 ms delay, rails."""

    def __init__(self, tape: Tape, deposit: int, floor: int, ceiling: int, j0: int, network_fee_cents: float = 0.0) -> None:
        self.t, self.deposit, self.floor, self.ceiling = tape, deposit, floor, ceiling
        # Solana model of the server's fees (FEE_MODEL=solana on origin/balls): each fill pays its settlement transaction's network fee,
        # two 5,000-lamport signatures at the SOL price, about 0.1 cent at $100; the fraction of a cent carries to the next fill.
        self.network_fee_cents, self._fee_carry = network_fee_cents, 0.0
        self.i = SUB * j0
        self.pos, self.avg, self.realized, self.fees = 0, 0.0, 0, 0
        self.open: list[dict] = []
        self.halted, self.reason = False, None
        self.mids: list[int] = [int(v) for v in tape.mid50[max(0, self.i - 31 * SUB): self.i + 1: SUB]]

    def stake(self) -> int:
        return self.deposit + self.realized - self.fees + self._unreal()

    def _unreal(self) -> int:
        return int(round(self.pos * (self.t.mid50[self.i] - self.avg) / 1000.0)) if self.pos else 0

    def _apply(self, side: str, price: int, size: int, bps: float) -> None:
        signed = size if side == "buy" else -size
        self.fees += fee_cents(price, size, bps)
        if self.network_fee_cents:
            self._fee_carry += self.network_fee_cents
            whole = int(self._fee_carry + 1e-9)
            self._fee_carry -= whole
            self.fees += whole
        if self.pos == 0 or (self.pos > 0) == (signed > 0):
            total = abs(self.pos) + size
            self.avg = (self.avg * abs(self.pos) + price * size) / total
            self.pos += signed
        else:
            closing = min(size, abs(self.pos))
            self.realized += int(round((1 if self.pos > 0 else -1) * closing * (price - self.avg) / 1000.0))
            self.pos += signed
            if self.pos == 0:
                self.avg = 0.0
            elif abs(signed) > closing:
                self.avg = float(price)

    def observation(self) -> dict:
        o = self.t.observation(0, self.i)
        o["mids"] = list(self.mids[-32:])
        o.update({"positionLots": self.pos, "unrealizedPnlCents": self._unreal(), "stakeCents": self.stake(),
                  "floorCents": self.floor, "ceilingCents": self.ceiling, "halted": self.halted,
                  "openOrders": [dict(x) for x in self.open if x["status"] in ("pending", "open")]})
        return o

    def _release(self, order: dict) -> None:
        t, i = self.t, self.i
        buy = order["side"] == "buy"
        touch, depth = (int(t.ask[i]), int(t.asksz[i])) if buy else (int(t.bid[i]), int(t.bidsz[i]))
        marketable = touch <= order["price"] if buy else touch >= order["price"]
        if marketable:
            size = min(order["size"], depth)
            if size > 0:
                self._apply(order["side"], touch, size, FEES["taker"])
            order["remaining"] = order["size"] - size
        if order["type"] == "ioc" or order.get("remaining", order["size"]) == 0:
            order["status"] = "done"
        else:
            order["status"] = "open"
            order.setdefault("remaining", order["size"])

    def _cross(self) -> None:
        t, i = self.t, self.i
        for o in self.open:
            if o["status"] != "open":
                continue
            buy = o["side"] == "buy"
            other = int(t.ask[i]) if buy else int(t.bid[i])
            if (o["price"] >= other) if buy else (o["price"] <= other):
                size = min(o["remaining"], int(t.asksz[i] if buy else t.bidsz[i]))
                self._apply(o["side"], other, size, FEES["maker"])
                o["remaining"] -= size
                if o["remaining"] == 0:
                    o["status"] = "done"

    def _halt_check(self) -> None:
        if self.halted:
            return
        s = self.stake()
        if s <= self.floor or s >= self.ceiling:
            self.reason = "ceiling" if s >= self.ceiling else "floor"
            self._flatten()
            self.halted = True

    def _flatten(self) -> None:
        for o in self.open:
            o["status"] = "canceled"
        if self.pos:
            side = "sell" if self.pos > 0 else "buy"
            touch = int(self.t.bid[self.i] if self.pos > 0 else self.t.ask[self.i])
            self._apply(side, touch, abs(self.pos), FEES["taker"])

    def step(self, decision) -> None:
        """Apply one policy Decision at the current tick, then advance 200 ms."""
        if decision.flatten:
            self._flatten()
        elif decision.cancel_all:
            for o in self.open:
                o["status"] = "canceled"
        pending = []
        for order in decision.orders:
            pending.append({**order, "status": "pending", "remaining": order["size"], "order_type": order["type"]})
        self.open = [o for o in self.open if o["status"] in ("pending", "open")]
        self.open.extend(pending)
        for k in range(1, SUB + 1):
            self.i += 1
            if k == 1:                                   # 50 ms: orders placed at the tick are released
                for o in pending:
                    self._release(o)
            self._cross()
            self._halt_check()
            if self.halted:
                break
        self.mids.append(int(self.t.mid50[self.i]))
        self.open = [o for o in self.open if o["status"] in ("pending", "open")]


def run_session_stepwise(tape: Tape, scores: np.ndarray, feats: np.ndarray, deposit: int, floor: int, ceiling: int,
                         j0: int, j_stop: int, P: SessionParams, direction: np.ndarray | None = None) -> Outcome:
    env = StepEnv(tape, deposit, floor, ceiling, j0)
    ids, state, trades = IdSource(), SessionState(), 0
    for j in range(j0, j_stop):
        obs = env.observation()
        score = float(scores[j]) if direction is None else (0.75 if direction[j] > 0 else 0.25) * 0 + float(scores[j])
        if direction is not None:
            score = 0.5 + (abs(scores[j] - 0.5)) * (1 if direction[j] > 0 else -1) + (1e-9 if direction[j] > 0 else 0)
        d = decide_session(obs, LIMITS, score, list(feats[j]), P, state, ids)
        if d.reason == "halted":
            break
        was_flat = env.pos == 0
        env.step(d)
        if was_flat and env.pos != 0:
            trades += 1
        if env.halted:
            return Outcome(env.reason, env.stake(), trades, j)
    return Outcome("timeout", env.stake(), trades, j_stop)
