"""Quote through /v1/stream. Scoring runs on CPU from the saved weights."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import torch
import websockets

from model.client import ModelClient
from model.features import features_from_observation
from model.net import SignalNet
from model.policy import Decision, IdSource, InstrumentLimits, decide, within_rate_limit

ARTIFACTS = Path(__file__).resolve().parent / "artifacts" / "signal.pt"


def load_scorer(path: Path = ARTIFACTS):
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run python -m model.train first.")
    blob = torch.load(path, map_location="cpu", weights_only=False)
    network = SignalNet()
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


async def _send(socket, decision: Decision, sent_at: list[float]) -> None:
    loop = asyncio.get_running_loop()
    now = loop.time()
    messages = []
    if decision.flatten:
        messages.append({"op": "flatten"})
    elif decision.cancel_all:
        messages.append({"op": "cancel_all"})
    else:
        for client_order_id in decision.cancels:
            messages.append({"op": "cancel", "clientOrderId": client_order_id})
        if decision.orders and within_rate_limit(sent_at, now, len(decision.orders)):
            for order in decision.orders:
                messages.append({"op": "place", "order": order})
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
    score_of, threshold = load_scorer()
    ids = IdSource()
    sent_at: list[float] = []
    print(f"connected policy threshold {threshold:.2f}")
    async with websockets.connect(client.ws_url(), max_queue=1) as socket:
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=10)
            message = json.loads(raw)
            if message.get("type") == "pong":
                continue
            if message.get("type") != "snapshot":
                continue
            observation = message.get("observation")
            if observation is None:
                continue
            symbol = observation.get("market")
            limits = limits_for(symbol, catalog)
            if limits is None:
                continue
            features = features_from_observation(observation)
            score = score_of(features) if features is not None else 0.5
            decision = decide(observation, limits, score, threshold, ids, features)
            if decision.reason in ("no_session", "halted", "empty_book") and not decision.orders:
                if decision.reason == "halted":
                    print("session halted, not sending orders")
                continue
            if decision.orders or decision.cancels or decision.cancel_all or decision.flatten:
                print(decision.reason, len(decision.orders), "orders")
                await _send(socket, decision, sent_at)
            await socket.send(json.dumps({"op": "ping"}))


async def run(base_url: str = "http://localhost:3000") -> None:
    while True:
        try:
            await session(base_url)
        except SystemExit:
            raise
        except Exception as error:
            print(f"stream closed ({error}). Reconnecting.")
            await asyncio.sleep(1)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
