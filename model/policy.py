"""Turn one observation into model-API order commands.

Orders follow the predicted return. There is no standing quote on both
sides of the touch: that rule filled just before the price moved against
it, and the maker fee made the loss larger.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TAKER_BPS = 5
MAKER_BPS = 2
SHOCK_BPS = 50
TARGET_CAP = 10
WIDE_SPREAD_BPS = 5.0


@dataclass
class InstrumentLimits:
    symbol: str
    price_tick_cents: int
    max_order_lots: int
    max_position_lots: int


@dataclass
class Decision:
    orders: list[dict] = field(default_factory=list)
    cancels: list[str] = field(default_factory=list)
    cancel_all: bool = False
    flatten: bool = False
    reason: str = ""


def round_tick(price: int, tick: int, side: str) -> int:
    if tick <= 0:
        raise ValueError("tick must be positive")
    if side == "buy":
        return price - (price % tick)
    remainder = price % tick
    if remainder == 0:
        return price
    return price + (tick - remainder)


def notional_cents(price: int, size: int) -> int:
    return int(round(price * size / 1000))


def shock_budget_cents(price: int, size: int) -> int:
    """Room a new position needs: a 50 bps adverse move plus the taker fee to flatten.

    One BTC lot is about $84 of notional. Requiring that whole notional as
    cushion made a $5 stake unable to send any order.
    """
    notion = notional_cents(price, max(1, size))
    return notion * SHOCK_BPS // 10_000 + notion * TAKER_BPS // 10_000 + 1


def lots_that_fit(price: int, room_cents: int, limit: int) -> int:
    cap = max(0, min(limit, TARGET_CAP))
    best = 0
    for size in range(1, cap + 1):
        if shock_budget_cents(price, size) < room_cents:
            best = size
        else:
            break
    return best


def rail_capacity(observation: dict, price: int, limits: InstrumentLimits) -> int:
    hard = min(limits.max_position_lots, TARGET_CAP)
    stake = observation.get("stakeCents")
    if stake is None or price <= 0:
        return hard
    room = 10**12
    floor = observation.get("floorCents")
    ceiling = observation.get("ceilingCents")
    if floor is not None:
        room = min(room, int(stake) - int(floor))
    if ceiling is not None:
        room = min(room, int(ceiling) - int(stake))
    return lots_that_fit(price, room, hard)


def working_lots(observation: dict, side: str) -> int:
    total = 0
    for order in observation.get("openOrders") or []:
        if order.get("side") == side and order.get("status") in ("pending", "open"):
            total += int(order.get("remaining") or order.get("size") or 0)
    return total


def position_allows(observation: dict, side: str, size: int, limits: InstrumentLimits) -> bool:
    position = int(observation.get("positionLots") or 0)
    if side == "buy":
        return position + working_lots(observation, "buy") + size <= limits.max_position_lots
    return position - working_lots(observation, "sell") - size >= -limits.max_position_lots


def within_rate_limit(sent_at: list[float], now: float, additional: int, limit: int = 18) -> bool:
    recent = [stamp for stamp in sent_at if now - stamp < 1.0]
    sent_at[:] = recent
    return len(recent) + additional <= limit


class IdSource:
    """The server caches every clientOrderId it has seen, so a reconnect needs a new prefix."""

    def __init__(self, prefix: str = "bot") -> None:
        self.prefix = prefix
        self.used: set[str] = set()
        self._n = 0

    def next_id(self) -> str:
        while True:
            self._n += 1
            client_order_id = f"{self.prefix}-{self._n}"
            if client_order_id not in self.used:
                self.used.add(client_order_id)
                return client_order_id


def aggressive_price(side: str, bid: int, ask: int, tick: int) -> int:
    if side == "buy":
        price = round_tick(ask, tick, "sell")
        if price < ask:
            price += tick
        return price
    price = round_tick(bid, tick, "buy")
    if price > bid:
        price -= tick
    return max(price, tick)


def entry_lots(expected_bps: float, enter_bps: float, capacity: int) -> int:
    if capacity < 1 or enter_bps <= 0:
        return 0
    multiple = abs(expected_bps) / enter_bps
    if multiple < 1.5:
        size = 1
    elif multiple < 2.5:
        size = min(4, capacity)
    else:
        size = capacity
    return max(1, min(size, capacity))


def desired_lots(
    expected_bps: float,
    position: int,
    held_bars: int,
    enter_bps: float,
    exit_bps: float,
    horizon_bars: int,
    capacity: int,
) -> int:
    """Target inventory. An open trade is held for the forecast horizon so fees are not paid twice."""
    cap = max(0, min(capacity, TARGET_CAP))
    if cap < 1:
        return 0
    if position != 0 and 0 <= held_bars < horizon_bars:
        return int(max(-cap, min(cap, position)))
    if expected_bps >= enter_bps:
        raw = entry_lots(expected_bps, enter_bps, cap)
    elif expected_bps <= -enter_bps:
        raw = -entry_lots(expected_bps, enter_bps, cap)
    elif position > 0 and expected_bps > -exit_bps:
        raw = min(position, cap)
    elif position < 0 and expected_bps < exit_bps:
        raw = max(position, -cap)
    else:
        raw = 0
    if position > 0 and raw > 0:
        raw = min(position, cap)
    elif position < 0 and raw < 0:
        raw = max(position, -cap)
    return int(max(-cap, min(cap, raw)))


def _best(observation: dict) -> tuple[int, int] | None:
    bids = observation.get("bids") or []
    asks = observation.get("asks") or []
    if not bids or not asks:
        return None
    bid = int(bids[0]["price"])
    ask = int(asks[0]["price"])
    if bid <= 0 or ask <= bid:
        return None
    return bid, ask


def _open_orders(observation: dict) -> list[dict]:
    return [
        order
        for order in (observation.get("openOrders") or [])
        if order.get("status") in ("pending", "open")
    ]


def decide(
    observation: dict,
    limits: InstrumentLimits,
    expected_bps: float,
    enter_bps: float,
    exit_bps: float,
    horizon_bars: int,
    held_bars: int,
    ids: IdSource,
) -> Decision:
    if observation.get("halted"):
        return Decision(reason="halted")
    if observation.get("stakeCents") is None:
        return Decision(reason="no_session")
    if observation.get("priceSource") == "stale":
        return Decision(cancel_all=True, reason="stale")
    book = _best(observation)
    if book is None:
        return Decision(reason="empty_book")
    bid, ask = book
    mid = int(observation.get("mid") or (bid + ask) // 2)
    position = int(observation.get("positionLots") or 0)
    capacity = rail_capacity(observation, mid, limits)
    if capacity < 1 and position == 0 and not _open_orders(observation):
        return Decision(reason="near_rail")
    if capacity < 1:
        return Decision(flatten=True, reason="near_rail")

    spread_bps = (ask - bid) / mid * 1e4 if mid else 0.0
    if spread_bps > WIDE_SPREAD_BPS:
        if position == 0 and not _open_orders(observation):
            return Decision(reason="wide_spread")
        desired = position
    else:
        desired = desired_lots(
            expected_bps, position, held_bars, enter_bps, exit_bps, horizon_bars, capacity
        )
    orders = _open_orders(observation)
    delta = desired - position
    if orders:
        if delta == 0:
            return Decision(cancels=[order["clientOrderId"] for order in orders], reason="cancel")
        side = "buy" if delta > 0 else "sell"
        same = [order for order in orders if order.get("side") == side]
        extras = [order for order in orders if order not in same[:1]]
        if extras or not same:
            return Decision(cancels=[order["clientOrderId"] for order in (extras or orders)], reason="cancel")
        return Decision(reason="working")
    if delta == 0:
        # #region agent log
        try:
            import json, time
            with open(r"C:\Akash\Coding\dubhacks26\debug-a0c237.log", "a", encoding="utf-8") as _f:
                _f.write(json.dumps({"sessionId": "a0c237", "hypothesisId": "A", "location": "policy.py:decide", "message": "no order", "data": {"reason": "hold", "expectedBps": expected_bps, "enterBps": enter_bps, "position": position, "capacity": capacity, "orders": 0}, "timestamp": int(time.time() * 1000)}) + "\n")
        except Exception:
            pass
        # #endregion
        return Decision(reason="hold")

    side = "buy" if delta > 0 else "sell"
    size = min(abs(delta), limits.max_order_lots)
    while size > 0 and not position_allows(observation, side, size, limits):
        size -= 1
    if size < 1:
        return Decision(reason="position_limit")
    price = aggressive_price(side, bid, ask, limits.price_tick_cents)
    reason = "enter" if position == 0 else ("reverse" if desired * position < 0 else "exit")
    # #region agent log
    try:
        import json, time
        with open(r"C:\Akash\Coding\dubhacks26\debug-a0c237.log", "a", encoding="utf-8") as _f:
            _f.write(json.dumps({"sessionId": "a0c237", "hypothesisId": "C", "location": "policy.py:decide", "message": "order", "data": {"reason": reason, "expectedBps": expected_bps, "enterBps": enter_bps, "side": side, "size": size, "orders": 1}, "timestamp": int(time.time() * 1000)}) + "\n")
    except Exception:
        pass
    # #endregion
    return Decision(
        orders=[
            {
                "clientOrderId": ids.next_id(),
                "side": side,
                "size": size,
                "type": "ioc",
                "price": price,
            }
        ],
        reason=reason,
    )
