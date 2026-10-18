"""High-frequency rule bot: buy on any downtick, sell on the first uptick that beats the fees.

Selected with BOT_STRATEGY=hft. Meant for a server started with FEE_MODEL=solana, where a fill
costs only its Solana network fee (about 0.1 cents). It holds one long position at a time:

- Flat: when the mid ticks down from the last mid the bot saw, buy with an IOC at the ask.
- Long: sell the whole position with an IOC at the bid as soon as the sale, after its fee,
  returns more than the buy cost including its fee, plus HFT_MIN_PROFIT_CENTS.

The Solana fee is per transaction, not per dollar, so size decides how small a move pays.
The bot trades enough lots that one price tick covers a round trip's fees HFT_TICK_COVER
times (default 2), capped at HFT_MAX_LEVERAGE times the stake (default 50). HFT_LOTS fixes
the size instead. Under the exchange fee model no size makes one tick pay, so it trades one lot.

There is no stop-loss. The session floor is the only downside limit.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass

import websockets

from model.client import ModelClient
from model.policy import Decision, IdSource, InstrumentLimits, aggressive_price, position_allows


@dataclass
class HftConfig:
    tick_cover: float = 2.0
    max_leverage: float = 50.0
    min_profit_cents: float = 0.0
    lots: int | None = None

    @classmethod
    def from_env(cls) -> "HftConfig":
        lots = os.environ.get("HFT_LOTS")
        return cls(
            tick_cover=float(os.environ.get("HFT_TICK_COVER", cls.tick_cover)),
            max_leverage=float(os.environ.get("HFT_MAX_LEVERAGE", cls.max_leverage)),
            min_profit_cents=float(os.environ.get("HFT_MIN_PROFIT_CENTS", cls.min_profit_cents)),
            lots=int(lots) if lots else None,
        )


@dataclass
class Fees:
    """GET /v1/instruments `fees`: basis points on notional plus a flat Solana fee per fill."""

    model: str = "exchange"
    taker_bps: float = 5.0
    solana_cents: float = 0.0

    @classmethod
    def from_instruments(cls, body: dict) -> "Fees":
        raw = body.get("fees") or {}
        return cls(
            model=str(raw.get("model", "exchange")),
            taker_bps=float(raw.get("takerBps", 5)),
            solana_cents=float(raw.get("solanaFeeCents", 0)),
        )

    def on(self, notional_cents: float) -> float:
        return notional_cents * self.taker_bps / 10_000 + self.solana_cents


class HftTrader:
    def __init__(self, config: HftConfig, fees: Fees) -> None:
        self.config = config
        self.fees = fees
        self.last_mid: int | None = None
        self.lots = 0
        self.cost_cents = 0.0  # what the open lots cost, fees included

    def reset(self) -> None:
        self.last_mid = None
        self.lots = 0
        self.cost_cents = 0.0

    def on_events(self, events: list[dict], ours: str) -> None:
        """Track the cost of our own fills. `ours` is this connection's clientOrderId prefix."""
        for event in events:
            if event.get("type") != "fill" or not str(event.get("clientOrderId", "")).startswith(ours):
                continue
            size = int(event["size"])
            notional = int(event["price"]) * size / 1000
            fee = int(event.get("fee") or 0)
            if event.get("side") == "buy":
                self.lots += size
                self.cost_cents += notional + fee
            elif self.lots > 0:
                sold = min(size, self.lots)
                self.cost_cents -= self.cost_cents * sold / self.lots
                self.lots -= sold

    def size(self, price: int, stake: int, limits: InstrumentLimits) -> int:
        if self.config.lots:
            lots = self.config.lots
        elif self.fees.taker_bps > 0 or self.fees.solana_cents <= 0:
            lots = 1
        else:
            tick_value = limits.price_tick_cents / 1000  # cents one lot gains on a one-tick move
            lots = math.ceil(self.config.tick_cover * 2 * self.fees.solana_cents / tick_value)
        lot_cents = price / 1000
        cap = int(stake * self.config.max_leverage // lot_cents) if lot_cents > 0 else lots
        return max(1, min(lots, cap, limits.max_order_lots))

    def profit_if_sold(self, bid: int, lots: int) -> float:
        """Cents left after selling `lots` at `bid` and paying the sell fee, minus what they cost."""
        proceeds = bid * lots / 1000
        cost = self.cost_cents * lots / max(1, self.lots)
        return proceeds - self.fees.on(proceeds) - cost

    def decide(self, observation: dict, limits: InstrumentLimits, ids: IdSource) -> Decision:
        if observation.get("halted"):
            return Decision(reason="halted")
        stake = observation.get("stakeCents")
        if stake is None:
            return Decision(reason="no_session")
        if observation.get("priceSource") == "stale":
            return Decision(cancel_all=True, reason="stale")
        bids, asks = observation.get("bids") or [], observation.get("asks") or []
        if not bids or not asks:
            return Decision(reason="empty_book")
        bid, ask = int(bids[0]["price"]), int(asks[0]["price"])
        mid = int(observation.get("mid") or (bid + ask) // 2)
        previous, self.last_mid = self.last_mid, mid
        if any(o.get("status") in ("pending", "open") for o in observation.get("openOrders") or []):
            return Decision(reason="working")
        position = int(observation.get("positionLots") or 0)
        tick = limits.price_tick_cents

        if position > 0:
            if self.lots != position:
                # Fills we did not see (a restart): price the position at the current mark.
                notional = mid * position / 1000
                self.lots, self.cost_cents = position, notional + self.fees.on(notional)
            if self.profit_if_sold(bid, position) <= self.config.min_profit_cents:
                return Decision(reason="hold")
            size = min(position, limits.max_order_lots)
            order = {"clientOrderId": ids.next_id(), "side": "sell", "size": size, "type": "ioc",
                     "price": aggressive_price("sell", bid, ask, tick)}
            return Decision(orders=[order], reason="sell_uptick")
        if position < 0:
            return Decision(flatten=True, reason="flat_short")

        self.lots, self.cost_cents = 0, 0.0
        if previous is None or mid >= previous:
            return Decision(reason="no_downtick")
        size = self.size(ask, int(stake), limits)
        while size > 0 and not position_allows(observation, "buy", size, limits):
            size -= 1
        if size < 1:
            return Decision(reason="position_limit")
        order = {"clientOrderId": ids.next_id(), "side": "buy", "size": size, "type": "ioc",
                 "price": aggressive_price("buy", bid, ask, tick)}
        return Decision(orders=[order], reason="buy_downtick")


async def session(base_url: str) -> None:
    # Shared plumbing from the model bot. Imported here so the two modules do not import each other at load.
    from model.live import DECISION_INTERVAL, IDLE_PING, InFlight, _send, limits_for

    client = ModelClient(base_url)
    client.health()
    instruments = client.instruments()
    catalog = {item["symbol"]: item for item in instruments["instruments"]}
    config = HftConfig.from_env()
    trader = HftTrader(config, Fees.from_instruments(instruments))
    prefix = f"hft{int(time.time() * 1000):x}"
    ids = IdSource(prefix=prefix)
    inflight = InFlight()
    sent_at: list[float] = []
    loop = asyncio.get_running_loop()
    last_decision = 0.0
    last_ts: int | None = None
    waiting = halted = False
    round_trips = 0
    realized = 0.0
    print(f"hft bot connected to {client.ws_url()}: buy any downtick, sell the first uptick that beats the fees")

    def announce_fees() -> None:
        fees = trader.fees
        if fees.model == "solana":
            print(f"fees: Solana only, {fees.solana_cents:.3f} cents per fill")
        else:
            print(f"fees: exchange {fees.taker_bps:g} bps per taker fill. Start the server with FEE_MODEL=solana for one-tick trading.")

    announce_fees()
    async with websockets.connect(client.ws_url(), max_queue=None) as socket:
        while True:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=IDLE_PING)
            except asyncio.TimeoutError:
                await socket.send(json.dumps({"op": "ping"}))
                continue
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "events":
                events = message.get("events") or []
                inflight.on_events(events)
                before = trader.cost_cents / max(1, trader.lots) if trader.lots else 0.0
                trader.on_events(events, prefix)
                for event in events:
                    if event.get("type") != "fill" or not str(event.get("clientOrderId", "")).startswith(prefix):
                        continue
                    if event["side"] == "sell":
                        proceeds = event["price"] * event["size"] / 1000 - int(event.get("fee") or 0)
                        gain = proceeds - before * event["size"]
                        realized += gain
                        round_trips += 1
                        print(f"sold {event['size']} lots at {event['price'] / 100:,.2f}: {gain:+.2f}¢ "
                              f"(round trip {round_trips}, total {realized:+.2f}¢)")
                    else:
                        print(f"bought {event['size']} lots at {event['price'] / 100:,.2f} (fee {event.get('fee', 0)}¢)")
                continue
            if kind == "error":
                print("server error", message.get("error"))
                continue
            observation = message.get("observation") if kind == "snapshot" else None
            if observation is None:
                continue
            if observation.get("stakeCents") is None and not observation.get("halted"):
                trader.reset()
                last_ts = None
                if not waiting:
                    print("waiting for a live session. Open one from the page.")
                    waiting = True
                continue
            ts = int(observation.get("ts") or 0)
            if waiting or (last_ts is not None and ts < last_ts):
                # A new session: its SOL reference price, and so its Solana fee, may have changed.
                trader.reset()
                trader.fees = Fees.from_instruments(client.instruments())
                announce_fees()
                round_trips, realized = 0, 0.0
            waiting = False
            last_ts = ts
            now = loop.time()
            if now - last_decision < DECISION_INTERVAL:
                continue
            last_decision = now
            limits = limits_for(observation.get("market"), catalog)
            if limits is None:
                continue
            view = inflight.view(observation, now)
            decision = trader.decide(view, limits, ids)
            if decision.reason == "halted":
                if not halted:
                    print("session halted, not sending orders")
                    halted = True
                continue
            halted = False
            idle = not view.get("positionLots") and not view.get("openOrders")
            if (decision.flatten or decision.cancel_all) and idle:
                continue
            if decision.orders or decision.cancels or decision.cancel_all or decision.flatten:
                await _send(socket, decision, sent_at, inflight, view)
