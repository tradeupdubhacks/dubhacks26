"""Quote through /v1/stream. Scoring runs on CPU from the saved weights."""

from __future__ import annotations

import asyncio
import builtins
import functools
import json
import os
import time

import websockets

from model.client import ApiError, ModelClient
from model.policy import (
    HARD_EXIT_BPS,
    TRADE_EVERY_MS,
    Decision,
    IdSource,
    HybridConfig,
    InstrumentLimits,
    decide,
    decide_hybrid,
    within_rate_limit,
)
from model.score import Scorer


# Documented in docs/model-api.md. The client instrument payload does not repeat them.
DOCUMENTED_LIMITS = {
    "BTC-USDT": (100_000, 100_000),
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
# Sessions this bot trades. The server tags each live session with the model it chose:
# "network" is this one ("safe" is the older name). None is an older server with no tags.
OWN_STRATEGIES = (None, "network", "safe")
# The session models in model/live_session.py, by the command-line word that starts them.
SESSION_MODELS = {"session": ("small",), "transformer": ("transformer",)}
# A price window that is not full is reloaded from GET /v1/bars at most this often.
SEED_RETRY_S = 2.0
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


async def session(base_url: str, session_id: str | None = None) -> None:
    """Trade one session. With `session_id`, only that session, returning once it halts.
    Without one, the server's latest session, waiting for the next when it ends."""
    # Tag every line with the session, since one bot now trades several at once.
    print = functools.partial(builtins.print, f"[{session_id}]", flush=True) if session_id else builtins.print  # noqa: A001
    client = ModelClient(base_url, session_id=session_id)
    client.health()
    catalog = {
        item["symbol"]: item
        for item in client.instruments()["instruments"]
    }
    scorer = Scorer()
    policy_name = model_policy()
    hybrid = hybrid_config()
    if policy_name == "hybrid":
        print(
            f"policy: hybrid. long {hybrid.lots} lots when the forecast is >= {scorer.enter_bps:g} bps and the book "
            f"leans {hybrid.enter_imbalance:g}; out when it is < {hybrid.exit_bps:g} bps or leans -{hybrid.exit_imbalance:g}"
        )
    else:
        print("policy: pace (scale in and hold)")
    # The server remembers every id, including ones from an earlier connection.
    ids = IdSource(prefix=f"bot{int(time.time() * 1000):x}")
    inflight = InFlight()
    sent_at: list[float] = []
    loop = asyncio.get_running_loop()
    last_decision = 0.0
    last_seed = float("-inf")
    reload_reported = False
    waiting = False
    halted = False
    announced = False
    ready_announced = False
    entry_ts: int | None = None
    last_trade_ts: int | None = None
    last_trade_wall: float | None = None
    seen_position: int | None = None
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
            owner = observation.get("strategy")
            foreign = owner is not None and owner not in OWN_STRATEGIES
            if foreign or (observation.get("stakeCents") is None and not observation.get("halted")):
                scorer.reset()
                entry_ts = None
                last_trade_ts = None
                last_trade_wall = None
                seen_position = None
                last_sign = 0
                announced = False
                ready_announced = False
                if not waiting:
                    if foreign:
                        print(f"this session uses the {owner} strategy, which the hft bot trades. waiting for a safe session.")
                    else:
                        print("waiting for a live session. Open one from the page.")
                    waiting = True
                continue
            if waiting:
                print("live session found")
                waiting = False
                scorer.reset()
                entry_ts = None
                last_trade_ts = None
                last_trade_wall = None
                seen_position = None
                last_sign = 0
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
            if scorer.bucket is not None and int(observation.get("ts") or 0) // scorer.bar_ms < scorer.bucket:
                # The clock went back: a new session replaced the old one. Load its history too.
                scorer.reset()
                entry_ts = None
                last_trade_ts = None
                last_trade_wall = None
                seen_position = None
                last_sign = 0
                announced = False
                ready_announced = False
            if scorer.bucket is None or (not scorer.ready and now - last_seed >= SEED_RETRY_S):
                # Start from the server's bar history instead of waiting two minutes to build one.
                # A session that replaces another can leave stale bars ahead of the new clock,
                # and the window then empties itself. Reload whenever it is not full.
                last_seed = now
                try:
                    history = client.bars()
                    if history.get("market") == symbol:
                        scorer.reset()
                        loaded = scorer.seed(history.get("bars") or [])
                        # Reloads repeat every few seconds while the window fills; report only the first.
                        if not reload_reported:
                            print(f"loaded {loaded} bars of price history from the server")
                            reload_reported = True
                except ApiError as error:
                    print(f"no bar history ({error.code}), building the window live")
            expected = scorer.update(observation)
            if not scorer.ready and not announced:
                print("price history is still filling. buys still go out on the 5s pace")
                announced = True
            elif scorer.ready and not ready_announced:
                if policy_name == "hybrid":
                    print(f"price window ready. buying when the forecast is >= {scorer.enter_bps:g} bps and the book leans up")
                else:
                    print(
                        f"price window ready. adding while the forecast stays above "
                        f"{-HARD_EXIT_BPS:.0f} bps, then holding"
                    )
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
            # Each fill starts the 5s clock. The next decision adds another clip
            # until the rail cap, and then the long is held. A canceled IOC does
            # not move the position, so it does not reset the clock.
            if seen_position is None:
                seen_position = position
            elif position != seen_position:
                last_trade_ts = ts
                last_trade_wall = now
                seen_position = position
            market_idle = TRADE_EVERY_MS if last_trade_ts is None else max(0, ts - last_trade_ts)
            wall_idle = TRADE_EVERY_MS if last_trade_wall is None else max(0.0, (now - last_trade_wall) * 1000)
            idle_ms = int(max(market_idle, wall_idle))
            horizon = max(1, TRADE_EVERY_MS // max(1, scorer.bar_ms))
            if policy_name == "hybrid":
                decision = decide_hybrid(view, limits, expected, scorer.enter_bps, ids, hybrid)
            else:
                decision = decide(
                    view,
                    limits,
                    expected,
                    scorer.enter_bps,
                    scorer.exit_bps,
                    horizon,
                    held,
                    ids,
                    idle_ms,
                )
            if decision.reason == "halted":
                if not halted:
                    print("session halted, not sending orders")
                    halted = True
                if session_id:
                    return
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


STRATEGIES = ("model", "hft")
POLICIES = ("hybrid", "pace")


def model_policy() -> str:
    """Feature flag for BOT_STRATEGY=model. MODEL_POLICY=hybrid (default) is forecast plus book
    imbalance (decide_hybrid). MODEL_POLICY=pace is the scale-in-and-hold policy (decide)."""
    name = os.environ.get("MODEL_POLICY", "hybrid").strip().lower() or "hybrid"
    if name not in POLICIES:
        raise SystemExit(f"Unknown MODEL_POLICY {name!r}. Use one of: {', '.join(POLICIES)}.")
    return name


def hybrid_config() -> HybridConfig:
    base = HybridConfig()
    return HybridConfig(
        lots=int(os.environ.get("HYBRID_LOTS", base.lots)),
        enter_imbalance=float(os.environ.get("HYBRID_ENTER_IMBALANCE", base.enter_imbalance)),
        exit_imbalance=float(os.environ.get("HYBRID_EXIT_IMBALANCE", base.exit_imbalance)),
        exit_bps=float(os.environ.get("HYBRID_EXIT_BPS", base.exit_bps)),
        max_leverage=float(os.environ.get("HYBRID_MAX_LEVERAGE", base.max_leverage)),
    )


def strategy() -> str:
    """Feature flag. BOT_STRATEGY=model (default) runs the network, BOT_STRATEGY=hft runs model/hft.py."""
    name = os.environ.get("BOT_STRATEGY", "model").strip().lower() or "model"
    if name not in STRATEGIES:
        raise SystemExit(f"Unknown BOT_STRATEGY {name!r}. Use one of: {', '.join(STRATEGIES)}.")
    return name


# How often the supervisor reads GET /v1/sessions for new sessions to trade.
SESSION_POLL_S = 1.0


async def _trade(run_session, base_url: str, session_id: str) -> None:
    """Trade one session until it halts. Reconnect if the stream drops while it still runs."""
    while True:
        try:
            await run_session(base_url, session_id)
            return
        except SystemExit:
            raise
        except Exception as error:
            try:
                status = (await asyncio.to_thread(ModelClient(base_url, session_id=session_id).session)).get("status")
            except Exception:
                status = None
            if status != "running":
                return
            print(f"[{session_id}] stream closed ({error!r}). Reconnecting.")
            await asyncio.sleep(1)


async def _single(run_session, base_url: str) -> None:
    """Older servers run one session at a time and have no session list."""
    while True:
        try:
            await run_session(base_url)
        except SystemExit:
            raise
        except Exception as error:
            print(f"stream closed ({error!r}). Reconnecting.")
            await asyncio.sleep(1)


def _orphaned() -> bool:
    """True when the server that started this bot (MODEL_BOT_CHILD=1, src/bot.ts) has died."""
    return os.environ.get("MODEL_BOT_CHILD") == "1" and os.getppid() == 1


async def run(base_url: str = "http://127.0.0.1:3000", kind: str | None = None) -> None:
    """Supervisor: trade every running session of this bot's model, one task per session.

    `kind` is "session" or "transformer" for the session models; otherwise BOT_STRATEGY picks
    the team network ("model") or the hft rule bot."""
    if kind in SESSION_MODELS:
        import functools as _functools

        from model.live_session import session as session_model

        name, own = kind, SESSION_MODELS[kind]
        run_session = _functools.partial(_session_model_task, session_model, kind)
    else:
        name = strategy()
        if name == "hft":
            from model.hft import OWN_STRATEGIES as own
            from model.hft import session as run_session
        else:
            own, run_session = OWN_STRATEGIES, session
    print(f"strategy: {name}")
    client = ModelClient(base_url)
    try:
        await asyncio.to_thread(client.sessions)
    except ApiError as error:
        if error.status == 404:
            print("this server has no GET /v1/sessions. trading its one session")
            await _single(run_session, base_url)
            return
    except Exception:
        pass  # server not up yet; the loop below keeps trying
    print(f"watching {client.base_url}/v1/sessions for {' and '.join(str(x) for x in own if x)} sessions")
    tasks: dict[str, asyncio.Task] = {}
    down = False
    while True:
        if _orphaned():
            raise SystemExit("the server that started this bot is gone")
        try:
            listed = await asyncio.to_thread(client.sessions)
            if down:
                print("server is back")
            down = False
        except Exception as error:
            if not down:
                print(f"cannot list sessions ({error!r}). retrying")
            down = True
            await asyncio.sleep(2)
            continue
        for item in listed:
            session_id = item.get("sessionId")
            if not session_id or item.get("status") != "running" or item.get("strategy") not in own:
                continue
            task = tasks.get(session_id)
            if task is None or task.done():
                print(f"[{session_id}] new {item.get('strategy')} session, trading it")
                tasks[session_id] = asyncio.create_task(_trade(run_session, base_url, session_id))
        listed_ids = {item.get("sessionId") for item in listed}
        for session_id in [sid for sid, task in tasks.items() if task.done() and sid not in listed_ids]:
            del tasks[session_id]
        await asyncio.sleep(SESSION_POLL_S)


async def _session_model_task(session_model, kind: str, base_url: str, session_id: str | None = None) -> None:
    """model/live_session.py's session loop, with the policy word it expects."""
    await session_model(base_url, "transformer" if kind == "transformer" else "session", session_id)


def main() -> None:
    import sys

    # `python -m model.live session` and `transformer` run the session models in model/live_session.py.
    # Like the other bots they supervise: one task per running session of their model.
    kind = next((arg for arg in sys.argv[1:] if arg in SESSION_MODELS), None)
    asyncio.run(run(os.environ.get("MODEL_API_URL", "http://127.0.0.1:3000"), kind))


if __name__ == "__main__":
    main()
