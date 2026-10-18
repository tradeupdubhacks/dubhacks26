"""Turn one observation into model-API order commands."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Must match the server's fees (src/types.ts, 0 unless MAKER_BPS / TAKER_BPS are set).
TAKER_BPS = float(os.environ.get("TAKER_BPS", "0"))
MAKER_BPS = float(os.environ.get("MAKER_BPS", "0"))


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


def _best(observation: dict) -> tuple[int, int] | None:
    bids = observation.get("bids") or []
    asks = observation.get("asks") or []
    if not bids or not asks:
        return None
    return int(bids[0]["price"]), int(asks[0]["price"])


def _near_rail(observation: dict, price: int, size: int) -> bool:
    stake = observation.get("stakeCents")
    if stake is None:
        return False
    cushion = notional_cents(price, size)
    floor = observation.get("floorCents")
    ceiling = observation.get("ceilingCents")
    if floor is not None and int(stake) - int(floor) <= cushion:
        return True
    if ceiling is not None and int(ceiling) - int(stake) <= cushion:
        return True
    return False


def _passive_prices(bid: int, ask: int, tick: int) -> tuple[int | None, int | None]:
    buy = round_tick(bid, tick, "buy")
    sell = round_tick(ask, tick, "sell")
    if buy >= ask:
        buy = ask - tick
    if sell <= bid:
        sell = bid + tick
    if buy < tick:
        buy = None
    if sell <= 0 or (buy is not None and sell <= buy):
        sell = None
    return buy, sell


def decide(
    observation: dict,
    limits: InstrumentLimits,
    score: float,
    threshold: float,
    ids: IdSource,
    features: list[float] | None,
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
    if _near_rail(observation, mid, 1):
        return Decision(flatten=True, reason="near_rail")

    imbalance = features[0] if features else 0.0
    spread = features[2] if features else 0.0
    confidence = abs(score - 0.5) * 2
    taker_fee = TAKER_BPS / 10_000
    direction = "buy" if score >= 0.5 else "sell"
    take = (
        confidence >= threshold
        and abs(imbalance) > taker_fee
        and abs(imbalance) > spread
        and ((imbalance > 0) == (direction == "buy"))
    )

    decision = Decision(reason="take" if take else "quote")
    open_orders = [
        order
        for order in (observation.get("openOrders") or [])
        if order.get("status") in ("pending", "open")
    ]
    buy_price, sell_price = _passive_prices(bid, ask, limits.price_tick_cents)

    if take:
        touch = ask if direction == "buy" else bid
        price = round_tick(touch, limits.price_tick_cents, direction)
        if direction == "buy" and price < ask:
            price = round_tick(ask, limits.price_tick_cents, "sell")
        if direction == "sell" and price > bid:
            price = round_tick(bid, limits.price_tick_cents, "buy")
        if position_allows(observation, direction, 1, limits) and not _near_rail(observation, price, 1):
            decision.orders.append(
                {
                    "clientOrderId": ids.next_id(),
                    "side": direction,
                    "size": 1,
                    "type": "ioc",
                    "price": price,
                }
            )
        for order in open_orders:
            if order.get("side") == direction:
                decision.cancels.append(order["clientOrderId"])
        return decision

    desired = {"buy": buy_price, "sell": sell_price}
    for side, price in desired.items():
        if price is None or not position_allows(observation, side, 1, limits):
            for order in open_orders:
                if order.get("side") == side:
                    decision.cancels.append(order["clientOrderId"])
            continue
        if _near_rail(observation, price, 1):
            continue
        resting = [
            order
            for order in open_orders
            if order.get("side") == side and order.get("type") == "post_only" and order.get("price") == price
        ]
        # Keep one quote per side. Extras come from orders placed on a stale snapshot.
        for order in open_orders:
            if order.get("side") == side and order not in resting[:1]:
                decision.cancels.append(order["clientOrderId"])
        if not resting and 1 <= limits.max_order_lots:
            decision.orders.append(
                {
                    "clientOrderId": ids.next_id(),
                    "side": side,
                    "size": 1,
                    "type": "post_only",
                    "price": price,
                }
            )
    return decision
