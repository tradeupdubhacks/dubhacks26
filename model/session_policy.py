"""Goal-seeking policy for a user session: reach the ceiling without crossing the floor.

One bracket trade at a time. The signal picks the side and the moment, sizing.plan_trade picks the lots,
a resting limit is the take-profit, and flatten is the stop. It speaks only the model API:
orders (ioc, limit), cancel_all and flatten. It never sets the rails; it reads them from the observation.
"""

from __future__ import annotations

from dataclasses import dataclass

from model.features_session import VOL_INDEX
from model.policy import Decision, IdSource, InstrumentLimits, round_tick
from model.sizing import plan_trade


@dataclass
class SessionParams:
    take_profit_bps: float = 30.0
    stop_bps: float = 30.0
    max_hold_steps: int = 9000          # 30 minutes at the 200 ms live tick
    cooldown_steps: int = 25            # 5 s after an exit
    threshold: float = 0.0              # confidence = |p - 0.5| * 2
    vol_gate: float = 0.0               # minimum vol_bps feature
    entry_slip_ticks: int = 2


TICK_MS = 200                      # the live tick the *_steps parameters are counted in


@dataclass
class SessionState:
    entry_ts: float | None = None      # market time when the position was first seen
    cooldown_until: float = 0.0
    armed: bool = False                # an entry was sent or a position held; start the cooldown once flat
    tp_id: str | None = None


def _book(observation: dict) -> tuple[int, int] | None:
    bids = observation.get("bids") or []
    asks = observation.get("asks") or []
    if not bids or not asks:
        return None
    return int(bids[0]["price"]), int(asks[0]["price"])


def decide_session(
    observation: dict,
    limits: InstrumentLimits,
    score: float,
    features: list[float] | None,
    params: SessionParams,
    state: SessionState,
    ids: IdSource,
) -> Decision:
    if observation.get("halted"):
        return Decision(reason="halted")
    stake = observation.get("stakeCents")
    if stake is None:
        return Decision(reason="no_session")
    if observation.get("priceSource") == "stale":
        return Decision(cancel_all=True, reason="stale")
    book = _book(observation)
    if book is None:
        return Decision(reason="empty_book")
    bid, ask = book
    mid = int(observation.get("mid") or (bid + ask) // 2)
    position = int(observation.get("positionLots") or 0)
    now = float(observation.get("ts") or 0)         # market ms; unlike a call count it ignores extra snapshots
    tick = limits.price_tick_cents
    open_orders = [
        order for order in (observation.get("openOrders") or [])
        if order.get("status") in ("pending", "open")
    ]

    if position != 0:
        if state.entry_ts is None:
            state.entry_ts = now
        state.armed = True
        sign = 1 if position > 0 else -1
        lots = abs(position)
        unrealized = int(observation.get("unrealizedPnlCents") or 0)
        stop_cents = lots * (mid / 1000.0) * params.stop_bps / 1e4
        if unrealized <= -stop_cents:
            return Decision(flatten=True, reason="stop")
        if now - state.entry_ts >= (params.max_hold_steps - 1) * TICK_MS:
            return Decision(flatten=True, reason="time")
        if any(order.get("side") != ("buy" if sign > 0 else "sell") for order in open_orders):
            return Decision(reason="hold")          # take-profit already working
        average = mid - unrealized * 1000.0 / position
        target = average * (1 + sign * params.take_profit_bps / 1e4)
        side = "sell" if sign > 0 else "buy"
        price = round_tick(int(round(target)), tick, "sell" if sign > 0 else "buy")
        state.tp_id = ids.next_id()
        decision = Decision(reason="take_profit")
        decision.orders.append({
            "clientOrderId": state.tp_id, "side": side, "size": lots,
            "type": "limit", "timeInForce": "gtc", "price": price,
        })
        return decision

    state.entry_ts = None
    if open_orders:                                 # stale take-profit after a flatten, or an entry in flight
        working = [order for order in open_orders if order.get("type") == "ioc"]
        return Decision(cancel_all=not working, reason="clear" if not working else "entry_pending")
    if state.armed:
        state.armed = False
        state.cooldown_until = now + params.cooldown_steps * TICK_MS
    if now < state.cooldown_until:
        return Decision(reason="cooldown")
    if features is None:
        return Decision(reason="no_features")
    confidence = abs(score - 0.5) * 2
    if confidence < params.threshold or features[VOL_INDEX] < params.vol_gate:
        return Decision(reason="no_signal")
    floor = int(observation.get("floorCents") if observation.get("floorCents") is not None else 0)
    ceiling = observation.get("ceilingCents")
    if ceiling is None:
        return Decision(reason="no_ceiling")
    plan = plan_trade(
        int(stake), floor, int(ceiling), mid,
        limits.max_position_lots, params.take_profit_bps, params.stop_bps,
    )
    if plan is None:
        return Decision(reason="no_room")
    side = "buy" if score >= 0.5 else "sell"
    size = min(plan.lots, limits.max_order_lots)
    cap = ask + params.entry_slip_ticks * tick if side == "buy" else bid - params.entry_slip_ticks * tick
    state.armed = True
    decision = Decision(reason="entry")
    decision.orders.append({
        "clientOrderId": ids.next_id(), "side": side, "size": size, "type": "ioc", "price": cap,
    })
    return decision
