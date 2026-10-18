"""The session models over /v1/stream: `python -m model.live session` (small network) and `python -m model.live transformer`.

Both are goal-seeking policies for a live user session (see session_policy.py). They reuse the stream plumbing of model/live.py
(in-flight tracking, rate limit, limits) and only add the signal and the policy. The server starts them from the page's model dropdown.
"""

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
from model.features_session import session_features
from model.live import DECISION_INTERVAL, IDLE_PING, InFlight, _send, limits_for
from model.net import SmallNet
from model.policy import IdSource
from model.session_policy import SessionParams, SessionState, decide_session
from model.sizing import MAKER_BPS, TAKER_BPS
from model.transformer_signal import TickStream, TransformerScorer

SESSION_ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "session_signal.pt"


def load_scorer(path: Path = SESSION_ARTIFACT):
    """(score(features) -> P(up), threshold) for the small session network."""
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run python -m model.train_session first.")
    blob = torch.load(path, map_location="cpu", weights_only=False)
    network = SmallNet(int(blob.get("n_features", 4)))
    network.load_state_dict(blob["state_dict"])
    network.eval()
    mean, std, threshold = blob["mean"], blob["std"], float(blob["threshold"])

    def score(features: list[float]) -> float:
        scaled = [(value - center) / scale for value, center, scale in zip(features, mean, std)]
        with torch.no_grad():
            return float(torch.sigmoid(network(torch.tensor(scaled, dtype=torch.float32).unsqueeze(0))).item())

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
    """Scorer and policy parameters for the small session network (python -m model.train_session).

    The profile comes from MODEL_PROFILE (default "active"): "idle" keeps the artifact's own parameters."""
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
    rails allow, so `sizing_bps` is just above the exchange fees (0 when the server runs FEE_MODEL=solana)."""
    scorer = TransformerScorer()
    params = SessionParams(
        take_profit_bps=0, stop_bps=25, max_hold_steps=25, cooldown_steps=5, threshold=scorer.entry_threshold,
        vol_gate=0.0, sizing_bps=TAKER_BPS + MAKER_BPS + 1.0,
    )
    print(f"transformer loaded (entry confidence {params.threshold:.2f}, hold {params.max_hold_steps * 0.2:.0f}s, stop {params.stop_bps:g} bps)")
    return scorer, params


async def session(base_url: str, policy: str = "session") -> None:
    client = ModelClient(base_url)
    client.health()
    catalog = {item["symbol"]: item for item in client.instruments()["instruments"]}
    if policy == "transformer":
        scorer, params = load_transformer_model()
        stream = TickStream()
    else:
        score_of, params = load_session_model()
    states: dict[str, SessionState] = {}
    # The server remembers every id, including ones from an earlier connection.
    ids = IdSource(prefix=f"bot{int(time.time() * 1000):x}")
    inflight = InFlight()
    sent_at: list[float] = []
    loop = asyncio.get_running_loop()
    last_decision = 0.0
    waiting = False
    halted = False
    print(f"connected to {client.ws_url()}, policy threshold {params.threshold:.2f}")
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
                print("live session found, trading")
                waiting = False
            now = loop.time()
            if now - last_decision < DECISION_INTERVAL:
                continue
            last_decision = now
            symbol = observation.get("market")
            limits = limits_for(symbol, catalog)
            if limits is None or symbol != "BTC-USDT":
                continue                              # both session models are trained on BTC top of book only
            view = inflight.view(observation, now)
            features = session_features(view)
            if policy == "transformer":
                stream.update(observation)            # the raw snapshot, not the in-flight view: only the book matters
                score = scorer.score(stream)
            else:
                score = score_of(features) if features is not None else 0.5
            decision = decide_session(view, limits, score, features, params, states.setdefault(symbol, SessionState()), ids)
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
                continue                              # nothing to flatten
            if decision.orders or decision.cancels or decision.cancel_all or decision.flatten:
                print(decision.reason, len(decision.orders), "orders")
                await _send(socket, decision, sent_at, inflight, view)


async def run(base_url: str = "http://127.0.0.1:3000", policy: str = "session") -> None:
    while True:
        try:
            await session(base_url, policy)
        except SystemExit:
            raise
        except Exception as error:
            print(f"stream closed ({error!r}). Reconnecting.")
            await asyncio.sleep(1)


def main() -> None:
    """python -m model.live session       small session network (session_signal.pt), BTC-USDT only
    python -m model.live transformer   tick transformer (transformer_signal.pt), BTC-USDT only"""
    url = os.environ.get("MODEL_API_URL", "http://127.0.0.1:3000")
    asyncio.run(run(url, "transformer" if "transformer" in sys.argv[1:] else "session"))


if __name__ == "__main__":
    main()
