"""Turn one observation into model-API order commands.

Orders follow the predicted return. There is no standing quote on both
sides of the touch: that rule filled just before the price moved against
it, and the maker fee made the loss larger.

Inventory is built one clip at a time and then held. Selling a long just
because it had reached one or two lots paid the taker fee on every flip,
and that flip was the loss. A strong adverse forecast can take one clip
off. A quiet or bullish forecast adds, up to the rail budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TAKER_BPS = 5
MAKER_BPS = 2
SHOCK_BPS = 120
WIDE_SPREAD_BPS = 5.0
# A quiet book still adds one clip per this window, then the position is held.
TRADE_EVERY_MS = 5_000
# The server sits on an order for 50ms. On the BTC tape the touch often moves
# several dollars in that gap, and a limit at the old ask is canceled.
LATENCY_CROSS_BPS = 80
# One buy is about a BTC lot of notional. Cheaper coins use more lots per buy.
ADD_NOTIONAL_CENTS = 8_000
# Stake/price used to cap BTC at one or two lots, so the next decision had to sell.
MAX_ADDS = 16
# Mild forecasts are noise next to the taker fee. Only a crash forecast scales out.
HARD_EXIT_BPS = 25.0


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
    """Room a new position needs: a 120 bp adverse move plus the taker fee to flatten.

    One BTC lot is about $84. The cushion is large enough that a $5 stake
    still fits a few lots, and small enough that those lots survive a normal swing.
    """
    notion = notional_cents(price, max(1, size))
    return notion * SHOCK_BPS // 10_000 + notion * TAKER_BPS // 10_000 + 1


def lots_that_fit(price: int, room_cents: int, limit: int) -> int:
    cap = max(0, limit)
    best = 0
    for size in range(1, cap + 1):
        if shock_budget_cents(price, size) < room_cents:
            best = size
        else:
            break
    return best


def rail_capacity(observation: dict, price: int, limits: InstrumentLimits) -> int:
    hard = limits.max_position_lots
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


def crossing_price(side: str, bid: int, ask: int, tick: int, mid: int) -> int:
    """IOC limit that still reaches the touch after the matching delay."""
    price = aggressive_price(side, bid, ask, tick)
    cushion = max(tick, mid * LATENCY_CROSS_BPS // 10_000)
    if tick > 1:
        cushion -= cushion % tick
        if cushion < tick:
            cushion = tick
    if side == "buy":
        return price + cushion
    return max(tick, price - cushion)


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


def order_clip(price: int, limits: InstrumentLimits) -> int:
    """Lots in a single buy. BTC stays at one lot. A cheaper coin buys more lots."""
    if price <= 0:
        return 1
    lots = max(1, (ADD_NOTIONAL_CENTS * 1000) // price)
    return max(1, min(lots, limits.max_order_lots, limits.max_position_lots))


def entry_lots(expected_bps: float, enter_bps: float, capacity: int, step: int = 1) -> int:
    """One clip. A single order for the whole cap was one buy, and the next decision sold."""
    if capacity < 1 or enter_bps <= 0 or abs(expected_bps) < enter_bps:
        return 0
    return max(1, min(capacity, step))


def deploy_lots(observation: dict, price: int, limits: InstrumentLimits) -> int:
    """How many lots the book may hold. The rail sets the risk. The stake price does not."""
    allowed = rail_capacity(observation, price, limits)
    if allowed < 1 or price <= 0:
        return 0
    step = order_clip(price, limits)
    adds = min(MAX_ADDS, allowed // step)
    if adds < 1:
        return min(allowed, step)
    return min(adds * step, allowed, limits.max_position_lots)


def pace_lots(position: int, capacity: int, expected_bps: float, step: int = 1) -> int:
    """Add one clip. At the cap, hold. Selling the cap to buy it back was the loss."""
    cap = max(0, capacity)
    step = max(1, step)
    # A tighter rail stops new buys. Selling the position down to the new cap
    # and buying it back when the stake ticks up is a fee loop.
    if expected_bps <= -HARD_EXIT_BPS or position >= cap:
        return position
    return min(cap, position + step)


def desired_lots(
    expected_bps: float,
    position: int,
    held_bars: int,
    enter_bps: float,
    exit_bps: float,
    horizon_bars: int,
    capacity: int,
    step: int = 1,
) -> int:
    """Target inventory. A long is held. Only a crash forecast, after the horizon, takes a clip off."""
    del exit_bps  # mild exit levels paid the fee twice; the crash level is HARD_EXIT_BPS
    cap = max(0, capacity)
    step = max(1, step)
    if position != 0 and 0 <= held_bars < horizon_bars:
        return position
    if position > 0 and expected_bps <= -HARD_EXIT_BPS:
        return max(0, position - step)
    if position < 0 and expected_bps >= HARD_EXIT_BPS:
        return min(0, position + step)
    if position == 0 and expected_bps >= enter_bps:
        return entry_lots(expected_bps, enter_bps, cap, step)
    return position


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
    idle_ms: int = 0,
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
    step = order_clip(mid, limits)
    capacity = deploy_lots(observation, mid, limits)
    if capacity < 1 and position == 0 and not _open_orders(observation):
        return Decision(reason="near_rail")
    if capacity < 1:
        return Decision(flatten=True, reason="near_rail")

    spread_bps = (ask - bid) / mid * 1e4 if mid else 0.0
    due = idle_ms >= TRADE_EVERY_MS
    # A wide book used to return here for the whole session. BTC is tight, but a
    # fill was then held for an hour, so both markets went quiet. The 5s pace
    # still crosses either book.
    if spread_bps > WIDE_SPREAD_BPS and not due:
        if position == 0 and not _open_orders(observation):
            return Decision(reason="wide_spread")
        desired = position
    else:
        desired = desired_lots(
            expected_bps,
            position,
            held_bars,
            enter_bps,
            exit_bps,
            horizon_bars,
            capacity,
            step,
        )
    # A crash scale-out waits for the same 5s clock as a new buy, so a one-bar
    # forecast cannot dump the position on every snapshot.
    if desired < position and not due:
        desired = position
    orders = _open_orders(observation)
    paced = False
    if due and not orders and desired == position and capacity >= 1:
        nudged = pace_lots(position, capacity, expected_bps, step)
        if nudged != position:
            desired = nudged
            paced = True
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
        return Decision(reason="hold")

    side = "buy" if delta > 0 else "sell"
    size = min(abs(delta), limits.max_order_lots)
    while size > 0 and not position_allows(observation, side, size, limits):
        size -= 1
    if size < 1:
        return Decision(reason="position_limit")
    price = crossing_price(side, bid, ask, limits.price_tick_cents, mid)
    if paced:
        reason = "pace"
    elif position == 0:
        reason = "enter"
    elif desired * position < 0:
        reason = "reverse"
    else:
        reason = "exit"
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


# Hybrid policy (MODEL_POLICY=hybrid, the default in model/live.py).
#
# The network's 5-minute forecast decides whether to be long at all. The top-of-book
# imbalance, (bid size - ask size) / (bid size + ask size), times the entry and exit.
# Tuned with 20-minute simulations through the server on 26 windows of recorded
# BTCUSDT (FEE_MODEL=solana, $100 deposit, $0 floor, $105 ceiling): +$5 in all 26,
# median 2.3 minutes, deepest dip -$2.03. The same rule without the forecast also
# reached +$5 in all 26, faster, with dips to -$5.89. The forecast mostly keeps the
# bot out of the market when the book's lean is not backed by a longer move.
HYBRID_LOTS = 200
HYBRID_ENTER_IMBALANCE = 0.6
HYBRID_EXIT_IMBALANCE = 0.6
HYBRID_EXIT_BPS = 0.0
HYBRID_MAX_LEVERAGE = 200.0


@dataclass
class HybridConfig:
    lots: int = HYBRID_LOTS
    enter_imbalance: float = HYBRID_ENTER_IMBALANCE
    exit_imbalance: float = HYBRID_EXIT_IMBALANCE
    exit_bps: float = HYBRID_EXIT_BPS
    max_leverage: float = HYBRID_MAX_LEVERAGE


def book_imbalance(observation: dict) -> float:
    bids = observation.get("bids") or []
    asks = observation.get("asks") or []
    if not bids or not asks:
        return 0.0
    bid_size, ask_size = int(bids[0]["size"]), int(asks[0]["size"])
    total = bid_size + ask_size
    return (bid_size - ask_size) / total if total > 0 else 0.0


def decide_hybrid(
    observation: dict,
    limits: InstrumentLimits,
    expected_bps: float,
    enter_bps: float,
    ids: IdSource,
    config: HybridConfig | None = None,
) -> Decision:
    """Long `lots` when the forecast clears `enter_bps` and the book leans up; flat on a weak forecast or a hard lean down."""
    config = config or HybridConfig()
    if observation.get("halted"):
        return Decision(reason="halted")
    stake = observation.get("stakeCents")
    if stake is None:
        return Decision(reason="no_session")
    if observation.get("priceSource") == "stale":
        return Decision(cancel_all=True, reason="stale")
    book = _best(observation)
    if book is None:
        return Decision(reason="empty_book")
    if _open_orders(observation):
        return Decision(reason="working")
    bid, ask = book
    position = int(observation.get("positionLots") or 0)
    imbalance = book_imbalance(observation)
    tick = limits.price_tick_cents

    if position == 0:
        if expected_bps < enter_bps or imbalance < config.enter_imbalance:
            return Decision(reason="wait")
        lot_cents = ask / 1000
        cap = int(int(stake) * config.max_leverage // lot_cents) if lot_cents > 0 else config.lots
        size = max(1, min(config.lots, cap, limits.max_order_lots))
        while size > 0 and not position_allows(observation, "buy", size, limits):
            size -= 1
        if size < 1:
            return Decision(reason="position_limit")
        price = aggressive_price("buy", bid, ask, tick)
        return Decision(
            orders=[{"clientOrderId": ids.next_id(), "side": "buy", "size": size, "type": "ioc", "price": price}],
            reason="enter",
        )

    if position > 0 and expected_bps >= config.exit_bps and imbalance > -config.exit_imbalance:
        return Decision(reason="hold")
    # Exit a long on a weak forecast or a hard lean down. A short (left by another
    # policy) is simply closed, since this policy only holds longs.
    side = "sell" if position > 0 else "buy"
    size = min(abs(position), limits.max_order_lots)
    price = aggressive_price(side, bid, ask, tick)
    return Decision(
        orders=[{"clientOrderId": ids.next_id(), "side": side, "size": size, "type": "ioc", "price": price}],
        reason="exit",
    )
