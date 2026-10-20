"""High-frequency rule bot: trade with the top-of-book imbalance, exit when it turns.

Selected with BOT_STRATEGY=hft. Meant for a server started with FEE_MODEL=solana, where a fill
costs only its Solana network fee (about 0.1 cents).

Imbalance is (bid size - ask size) / (bid size + ask size) at the touch. On the BTC tape a book
leaning one way keeps moving that way for a few seconds, and that move is far larger than the
one-tick spread a taker pays.

- Flat: buy with an IOC at the ask when imbalance >= HFT_ENTER_IMBALANCE (default 0.6). Sell
  short with an IOC at the bid when it is <= minus that.
- Long: sell everything at the bid once imbalance <= -HFT_EXIT_IMBALANCE (default 0, i.e. the
  book stops leaning our way) or after HFT_MAX_HOLD_S seconds (default 5). Short is the mirror.

Size is HFT_LOTS (default 150, about $12,600 of BTC), capped at HFT_MAX_LEVERAGE times the stake
(default 150). There is no stop-loss beyond the hold limit; the session floor is the hard limit.

Size was raised 1.5x from 100 lots / 100x. On 48 ten-minute windows of the 2026-10-01 tape
(model/backtest.py StepEnv, Solana fee, a $100 stake, -20% floor, +$5 goal), both sizes reached the
goal in every window; 1.5x got there in a median 0.7 minutes instead of 1.0, and its worst dip was
-$3.13 instead of -$2.09.

Simulated on 26 ten-minute windows of recorded BTCUSDT (2026-10-01 and 2026-10-17) through the
server with FEE_MODEL=solana, a $100 stake reached +$5 in all 26, with a median of about
1.5 minutes and no dip below -$1.33. That simulation fills at the recorded top of book after
the server's 50ms latency; real queues and exchange fees would cut into it.
"""

from __future__ import annotations

import asyncio
import builtins
import functools
import json
import os
import time
from dataclasses import dataclass

import websockets

from model.client import ModelClient
from model.policy import Decision, IdSource, InstrumentLimits, aggressive_price, position_allows


# Sessions this bot trades. The server tags each live session with the model it chose; only
# "hft" sessions are this bot's ("fast" is the older name), so it can run next to the other models.
OWN_STRATEGIES = ("hft", "fast")


@dataclass
class HftConfig:
    enter_imbalance: float = 0.6
    exit_imbalance: float = 0.0
    max_hold_s: float = 5.0
    lots: int = 150
    max_leverage: float = 150.0

    @classmethod
    def from_env(cls) -> "HftConfig":
        return cls(
            enter_imbalance=float(os.environ.get("HFT_ENTER_IMBALANCE", cls.enter_imbalance)),
            exit_imbalance=float(os.environ.get("HFT_EXIT_IMBALANCE", cls.exit_imbalance)),
            max_hold_s=float(os.environ.get("HFT_MAX_HOLD_S", cls.max_hold_s)),
            lots=int(os.environ.get("HFT_LOTS", cls.lots)),
            max_leverage=float(os.environ.get("HFT_MAX_LEVERAGE", cls.max_leverage)),
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


def imbalance(observation: dict) -> float | None:
    bids, asks = observation.get("bids") or [], observation.get("asks") or []
    if not bids or not asks:
        return None
    bid_size, ask_size = int(bids[0]["size"]), int(asks[0]["size"])
    total = bid_size + ask_size
    return (bid_size - ask_size) / total if total > 0 else 0.0


class HftTrader:
    def __init__(self, config: HftConfig, fees: Fees) -> None:
        self.config = config
        self.fees = fees
        self.entered_ts: int | None = None
        # Our own fills, to report each round trip: signed lots and the cash they moved, fees included.
        self.lots = 0
        self.cash = 0.0
        self.round_trips: list[float] = []

    def reset(self) -> None:
        self.entered_ts = None
        self.lots = 0
        self.cash = 0.0
        self.round_trips = []

    def on_events(self, events: list[dict], ours: str) -> list[float]:
        """Track our fills. Returns the P&L, in cents, of any round trip they closed."""
        closed = []
        for event in events:
            if event.get("type") != "fill" or not str(event.get("clientOrderId", "")).startswith(ours):
                continue
            size = int(event["size"])
            notional = int(event["price"]) * size / 1000
            fee = int(event.get("fee") or 0)
            sign = 1 if event.get("side") == "buy" else -1
            self.cash += -sign * notional - fee
            self.lots += sign * size
            if self.lots == 0:
                closed.append(self.cash)
                self.round_trips.append(self.cash)
                self.cash = 0.0
        return closed

    def size(self, price: int, stake: int, limits: InstrumentLimits) -> int:
        lot_cents = price / 1000
        cap = int(stake * self.config.max_leverage // lot_cents) if lot_cents > 0 else self.config.lots
        return max(1, min(self.config.lots, cap, limits.max_order_lots))

    def decide(self, observation: dict, limits: InstrumentLimits, ids: IdSource) -> Decision:
        if observation.get("halted"):
            return Decision(reason="halted")
        stake = observation.get("stakeCents")
        if stake is None:
            return Decision(reason="no_session")
        if observation.get("priceSource") == "stale":
            return Decision(cancel_all=True, reason="stale")
        imb = imbalance(observation)
        if imb is None:
            return Decision(reason="empty_book")
        if any(o.get("status") in ("pending", "open") for o in observation.get("openOrders") or []):
            return Decision(reason="working")
        bid, ask = int(observation["bids"][0]["price"]), int(observation["asks"][0]["price"])
        position = int(observation.get("positionLots") or 0)
        ts = int(observation.get("ts") or 0)
        tick = limits.price_tick_cents

        def order(side: str, size: int, reason: str) -> Decision:
            price = aggressive_price(side, bid, ask, tick)
            return Decision(orders=[{"clientOrderId": ids.next_id(), "side": side, "size": size, "type": "ioc", "price": price}], reason=reason)

        if position == 0:
            self.entered_ts = None
            side = "buy" if imb >= self.config.enter_imbalance else "sell" if imb <= -self.config.enter_imbalance else None
            if side is None:
                return Decision(reason="no_signal")
            size = self.size(ask if side == "buy" else bid, int(stake), limits)
            while size > 0 and not position_allows(observation, side, size, limits):
                size -= 1
            if size < 1:
                return Decision(reason="position_limit")
            return order(side, size, f"enter_{'long' if side == 'buy' else 'short'}")

        if self.entered_ts is None:
            self.entered_ts = ts
        held_s = (ts - self.entered_ts) / 1000
        turned = imb <= -self.config.exit_imbalance if position > 0 else imb >= self.config.exit_imbalance
        if not turned and held_s < self.config.max_hold_s:
            return Decision(reason="hold")
        side = "sell" if position > 0 else "buy"
        return order(side, min(abs(position), limits.max_order_lots), "exit_turned" if turned else "exit_timeout")


async def session(base_url: str, session_id: str | None = None) -> None:
    """Trade one session. With `session_id`, only that session, returning once it halts.
    Without one, the server's latest session, waiting for the next when it ends."""
    # Tag every line with the session, since one bot now trades several at once.
    print = functools.partial(builtins.print, f"[{session_id}]", flush=True) if session_id else builtins.print  # noqa: A001
    # Shared plumbing from the model bot. Imported here so the two modules do not import each other at load.
    from model.live import DECISION_INTERVAL, IDLE_PING, InFlight, _send, limits_for

    client = ModelClient(base_url, session_id=session_id)
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
    print(
        f"hft bot connected to {client.ws_url()}: enter at imbalance {config.enter_imbalance:g}, exit when it turns "
        f"past {config.exit_imbalance:g} or after {config.max_hold_s:g}s, {config.lots} lots"
    )

    def announce_fees() -> None:
        fees = trader.fees
        if fees.model == "solana":
            print(f"fees: Solana only, {fees.solana_cents:.3f} cents per fill")
        else:
            print(f"fees: exchange {fees.taker_bps:g} bps per taker fill. This bot is tuned for FEE_MODEL=solana and will lose money on exchange fees.")

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
                for pnl in trader.on_events(events, prefix):
                    total = sum(trader.round_trips)
                    print(f"round trip {len(trader.round_trips)}: {pnl:+.2f}¢ (session total {total:+.2f}¢)")
                continue
            if kind == "error":
                print("server error", message.get("error"))
                continue
            observation = message.get("observation") if kind == "snapshot" else None
            if observation is None:
                continue
            owner = observation.get("strategy")
            live = observation.get("stakeCents") is not None or observation.get("halted")
            if not live or owner not in OWN_STRATEGIES:
                trader.reset()
                last_ts = None
                if not waiting:
                    if live:
                        print(f"this session uses the {owner or 'default'} strategy, which the trained model trades. waiting for a fast session.")
                    else:
                        print("waiting for a fast session. Open one from the page.")
                    waiting = True
                continue
            ts = int(observation.get("ts") or 0)
            if waiting or (last_ts is not None and ts < last_ts):
                # A new session: its SOL reference price, and so its Solana fee, may have changed.
                trader.reset()
                trader.fees = Fees.from_instruments(client.instruments())
                announce_fees()
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
                    print(f"session halted ({observation.get('haltReason')}), not sending orders")
                    halted = True
                if session_id:
                    return
                continue
            halted = False
            idle = not view.get("positionLots") and not view.get("openOrders")
            if (decision.flatten or decision.cancel_all) and idle:
                continue
            if decision.orders or decision.cancels or decision.cancel_all or decision.flatten:
                await _send(socket, decision, sent_at, inflight, view)
