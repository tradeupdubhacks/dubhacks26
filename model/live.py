"""Quote through /v1/stream. Scoring runs on CPU from the saved weights."""

from __future__ import annotations

import asyncio
import json
import time

import websockets

from model.client import ModelClient
from model.policy import Decision, IdSource, InstrumentLimits, decide, within_rate_limit
from model.score import Scorer


# Documented in docs/model-api.md. The client instrument payload does not repeat them.
DOCUMENTED_LIMITS = {
    "BTC-USDT": (10, 10),
    "SOL-USD": (2000, 10000),
}


def limits_for(symbol: str, instruments: dict) -> InstrumentLimits | None:
    raw = instruments.get(symbol)
    if raw is None:
        return None
    order_cap, position_cap = DOCUMENTED_LIMITS.get(symbol, (1, 1))
    return InstrumentLimits(
        symbol=raw["symbol"],
        price_tick_cents=int(raw["priceTickCents"]),
        max_order_lots=int(raw.get("maxOrderLots", order_cap)),
        max_position_lots=int(raw.get("maxPositionLots", position_cap)),
    )


# The server answers every command with a fresh snapshot. Deciding on each one
# re-sends cancels for orders whose cancel is still inside the 50ms latency
# window, which floods the socket. Decide at most this often instead.
DECISION_INTERVAL = 0.1
# With no live session the server sends one snapshot and then nothing.
IDLE_PING = 5.0
# Forget an in-flight command the server never confirmed.
INFLIGHT_TTL = 2.0
TERMINAL = ("canceled", "filled")


class InFlight:
    """Orders and cancels sent on the stream that the latest snapshot may not show yet."""

    def __init__(self) -> None:
        self.places: dict[str, tuple[float, dict]] = {}
        self.cancels: dict[str, float] = {}

    def sent_place(self, order: dict, now: float) -> None:
        self.places[order["clientOrderId"]] = (now, order)

    def sent_cancel(self, client_order_id: str, now: float) -> None:
        self.cancels[client_order_id] = now

    def on_events(self, events: list[dict]) -> None:
        for event in events:
            client_order_id = event.get("clientOrderId")
            if not client_order_id:
                continue
            done = event.get("type") == "reject" or event.get("status") in TERMINAL
            if done:
                self.places.pop(client_order_id, None)
                self.cancels.pop(client_order_id, None)

    def view(self, observation: dict, now: float) -> dict:
        """The observation as it will look once our in-flight commands land."""
        self.places = {k: v for k, v in self.places.items() if now - v[0] < INFLIGHT_TTL}
        self.cancels = {k: v for k, v in self.cancels.items() if now - v < INFLIGHT_TTL}
        listed = observation.get("openOrders") or []
        listed_ids = {order.get("clientOrderId") for order in listed}
        for client_order_id in listed_ids:
            self.places.pop(client_order_id, None)
        for client_order_id in list(self.cancels):
            if client_order_id not in listed_ids:
                self.cancels.pop(client_order_id)
        orders = [order for order in listed if order.get("clientOrderId") not in self.cancels]
        for _, order in self.places.values():
            orders.append({**order, "remaining": order["size"], "status": "pending"})
        return {**observation, "openOrders": orders}

    def working_ids(self, observation: dict) -> list[str]:
        return [order["clientOrderId"] for order in observation.get("openOrders") or []]


async def _send(socket, decision: Decision, sent_at: list[float], inflight: InFlight, view: dict) -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()
    messages = []
    if decision.flatten or decision.cancel_all:
        messages.append({"op": "flatten" if decision.flatten else "cancel_all"})
        for client_order_id in inflight.working_ids(view):
            inflight.sent_cancel(client_order_id, now)
    else:
        for client_order_id in decision.cancels:
            if client_order_id in inflight.cancels:
                continue
            messages.append({"op": "cancel", "clientOrderId": client_order_id})
            inflight.sent_cancel(client_order_id, now)
        if decision.orders and within_rate_limit(sent_at, now, len(decision.orders)):
            for order in decision.orders:
                messages.append({"op": "place", "order": order})
                inflight.sent_place(order, now)
                sent_at.append(now)
        elif decision.orders:
            print("skipped new orders because the 20-per-second limit is close")
    for message in messages:
        await socket.send(json.dumps(message))


async def session(base_url: str) -> None:
    client = ModelClient(base_url)
    client.health()
    catalog = {
        item["symbol"]: item
        for item in client.instruments()["instruments"]
    }
    scorer = Scorer()
    # The server remembers every id, including ones from an earlier connection.
    ids = IdSource(prefix=f"bot{int(time.time() * 1000):x}")
    inflight = InFlight()
    sent_at: list[float] = []
    loop = asyncio.get_running_loop()
    last_decision = 0.0
    waiting = False
    halted = False
    announced = False
    ready_announced = False
    entry_ts: int | None = None
    last_sign = 0
    print(
        f"connected to {client.ws_url()}, enter {scorer.enter_bps:.1f} bps "
        f"over {scorer.horizon_bars * scorer.bar_ms / 1000:.0f}s"
    )
    async with websockets.connect(client.ws_url(), max_queue=None) as socket:
        while True:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=IDLE_PING)
            except asyncio.TimeoutError:
                # Quiet stream: no live session is open. Keep the socket alive.
                await socket.send(json.dumps({"op": "ping"}))
                continue
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "events":
                inflight.on_events(message.get("events") or [])
                continue
            if kind == "error":
                print("server error", message.get("error"))
                continue
            if kind != "snapshot":
                continue
            observation = message.get("observation")
            if observation is None:
                continue
            if observation.get("stakeCents") is None and not observation.get("halted"):
                scorer.reset()
                entry_ts = None
                last_sign = 0
                announced = False
                ready_announced = False
                if not waiting:
                    print("waiting for a live session. Open one from the page.")
                    waiting = True
                continue
            if waiting:
                print("live session found")
                waiting = False
                scorer.reset()
                announced = False
                ready_announced = False
            now = loop.time()
            if now - last_decision < DECISION_INTERVAL:
                continue
            last_decision = now
            symbol = observation.get("market")
            limits = limits_for(symbol, catalog)
            if limits is None:
                continue
            view = inflight.view(observation, now)
            expected = scorer.update(observation)
            if not scorer.ready and not announced:
                print("waiting 2 minutes for a price window before sending orders")
                announced = True
            elif scorer.ready and not ready_announced:
                if scorer.enter_bps > 100:
                    print("price window ready. no forecast cleared the taker fee, so no orders are sent")
                else:
                    print(f"price window ready. entry level {scorer.enter_bps:.0f} bps")
                ready_announced = True
            position = int(observation.get("positionLots") or 0)
            sign = (position > 0) - (position < 0)
            ts = int(observation.get("ts") or 0)
            if sign == 0:
                entry_ts = None
            elif entry_ts is None or sign != last_sign:
                entry_ts = ts
            last_sign = sign
            held = 0 if entry_ts is None else max(0, (ts - entry_ts) // scorer.bar_ms)
            decision = decide(
                view,
                limits,
                expected,
                scorer.enter_bps,
                scorer.exit_bps,
                scorer.horizon_bars,
                held,
                ids,
            )
            if decision.reason == "halted":
                if not halted:
                    print("session halted, not sending orders")
                    halted = True
                continue
            halted = False
            if decision.reason in ("no_session", "empty_book") and not decision.orders:
                continue
            idle = not view.get("positionLots") and not view.get("openOrders")
            if (decision.flatten or decision.cancel_all) and idle:
                # Nothing to flatten. Near a rail on a flat book, or a stale feed with no orders.
                continue
            if decision.orders or decision.cancels or decision.cancel_all or decision.flatten:
                print(f"{decision.reason} expected {expected:.1f} bps, {len(decision.orders)} orders")
                await _send(socket, decision, sent_at, inflight, view)


async def run(base_url: str = "http://localhost:3000") -> None:
    while True:
        try:
            await session(base_url)
        except SystemExit:
            raise
        except Exception as error:
            print(f"stream closed ({error!r}). Reconnecting.")
            await asyncio.sleep(1)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
