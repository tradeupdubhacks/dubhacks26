"""Quote through /v1/stream. Scoring runs on CPU from the saved weights."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import torch
import websockets

from model.client import ModelClient
from model.features import features_from_observation
from model.features_session import session_features
from model.net import SignalNet
from model.policy import Decision, IdSource, InstrumentLimits, decide, within_rate_limit
from model.session_policy import SessionParams, SessionState, decide_session
from model.sizing import MAKER_BPS, TAKER_BPS
from model.transformer_signal import TickStream, TransformerScorer

ARTIFACTS = Path(__file__).resolve().parent / "artifacts" / "signal.pt"
SESSION_ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "session_signal.pt"


def load_scorer(path: Path = ARTIFACTS):
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run python -m model.train first.")
    blob = torch.load(path, map_location="cpu", weights_only=False)
    network = SignalNet(int(blob.get("n_features", 4)))
    network.load_state_dict(blob["state_dict"])
    network.eval()
    mean = blob["mean"]
    std = blob["std"]
    threshold = float(blob["threshold"])

    def score(features: list[float]) -> float:
        scaled = [(value - center) / scale for value, center, scale in zip(features, mean, std)]
        tensor = torch.tensor(scaled, dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            return float(torch.sigmoid(network(tensor)).item())

    return score, threshold


# "idle" uses the parameters saved in the artifact, which were calibrated to trade only when confidence is
# extreme; on the held-out day that is almost never (confidence peaks near 0.73, the threshold is 0.74), so the
# stake stays flat. "active" trades whenever confidence is above 0.30. On the held-out hours it makes about 18
# trades per session and loses money on average (fees and a tiny edge); see model/SESSION_MODEL.md.
PROFILES = {
    "idle": None,
    "active": dict(take_profit_bps=25, stop_bps=25, max_hold_steps=3000, cooldown_steps=25, threshold=0.30, vol_gate=0.0),
}


def load_session_model(path: Path = SESSION_ARTIFACT, profile: str | None = None):
    """Scorer and policy parameters for the goal-seeking session policy (python -m model.train_session).

    The profile comes from MODEL_PROFILE (default "active"): "idle" keeps the artifact's own parameters."""
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run python -m model.train_session first.")
    profile = profile or os.environ.get("MODEL_PROFILE", "active")
    if profile not in PROFILES:
        raise SystemExit(f"Unknown MODEL_PROFILE {profile!r}. Use one of: {', '.join(PROFILES)}.")
    score, _threshold = load_scorer(path)
    blob = torch.load(path, map_location="cpu", weights_only=False)
    params = SessionParams(**(PROFILES[profile] or blob["session_params"]))
    print(f"session model profile: {profile} (entry threshold {params.threshold:.2f}, take-profit/stop {params.take_profit_bps:g}/{params.stop_bps:g} bps)")
    return score, params


def load_transformer_model():
    """The tick transformer and its short-hold policy: enter on a confident 5 s forecast, flatten after about 5 s.

    Entry confidence is |prediction| / scale, so the threshold is the one that selected the top 10 % of ticks on the validation day.
    There is no take-profit; the trade ends at the hold limit (25 ticks = 5 s) or a 25 bps stop. Sizing aims for the largest position the
    rails allow, so `sizing_bps` is just above the fees."""
    scorer = TransformerScorer()
    params = SessionParams(
        take_profit_bps=0, stop_bps=25, max_hold_steps=25, cooldown_steps=5, threshold=scorer.entry_threshold,
        vol_gate=0.0, sizing_bps=TAKER_BPS + MAKER_BPS + 1.0,
    )
    print(f"transformer loaded (entry confidence {params.threshold:.2f}, hold {params.max_hold_steps * 0.2:.0f}s, stop {params.stop_bps:g} bps)")
    return scorer, params


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


async def session(base_url: str, policy: str = "quote") -> None:
    client = ModelClient(base_url)
    client.health()
    catalog = {
        item["symbol"]: item
        for item in client.instruments()["instruments"]
    }
    if policy == "session":
        score_of, params = load_session_model()
        threshold = params.threshold
        states: dict[str, SessionState] = {}
    elif policy == "transformer":
        scorer, params = load_transformer_model()
        stream = TickStream()
        threshold = params.threshold
        states = {}
    else:
        score_of, threshold = load_scorer()
    # The server remembers every id, including ones from an earlier connection.
    ids = IdSource(prefix=f"bot{int(time.time() * 1000):x}")
    inflight = InFlight()
    sent_at: list[float] = []
    loop = asyncio.get_running_loop()
    last_decision = 0.0
    waiting = False
    halted = False
    print(f"connected to {client.ws_url()}, policy threshold {threshold:.2f}")
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
                if not waiting:
                    print("waiting for a live session. Open one from the page.")
                    waiting = True
                continue
            if waiting:
                print("live session found, quoting")
                waiting = False
            now = loop.time()
            if now - last_decision < DECISION_INTERVAL:
                continue
            last_decision = now
            symbol = observation.get("market")
            limits = limits_for(symbol, catalog)
            if limits is None:
                continue
            view = inflight.view(observation, now)
            if policy in ("session", "transformer"):
                if symbol != "BTC-USDT":
                    continue                      # both are trained on BTC top of book only
                features = session_features(view)
                if policy == "transformer":
                    stream.update(observation)    # the raw snapshot, not the in-flight view: only the book matters
                    score = scorer.score(stream)
                else:
                    score = score_of(features) if features is not None else 0.5
                decision = decide_session(
                    view, limits, score, features, params, states.setdefault(symbol, SessionState()), ids
                )
            else:
                features = features_from_observation(view)
                score = score_of(features) if features is not None else 0.5
                decision = decide(view, limits, score, threshold, ids, features)
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
                print(decision.reason, len(decision.orders), "orders")
                await _send(socket, decision, sent_at, inflight, view)


async def run(base_url: str = "http://127.0.0.1:3000", policy: str = "quote") -> None:
    while True:
        try:
            await session(base_url, policy)
        except SystemExit:
            raise
        except Exception as error:
            print(f"stream closed ({error!r}). Reconnecting.")
            await asyncio.sleep(1)


def main() -> None:
    """python -m model.live            join-the-touch quoting (signal.pt)
    python -m model.live session    goal-seeking session policy (session_signal.pt), BTC-USDT only
    python -m model.live transformer short-hold transformer policy (transformer_signal.pt), BTC-USDT only"""
    url = os.environ.get("MODEL_API_URL", "http://127.0.0.1:3000")
    kind = "transformer" if "transformer" in sys.argv[1:] else "session" if "session" in sys.argv[1:] else "quote"
    asyncio.run(run(url, policy=kind))


if __name__ == "__main__":
    main()
