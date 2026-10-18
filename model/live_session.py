"""The session models over /v1/stream: `python -m model.live session` (small network) and `python -m model.live transformer`.

Both are goal-seeking policies for a live user session (see session_policy.py). They reuse the stream plumbing of model/live.py
(in-flight tracking, rate limit, limits) and only add the signal and the policy. The server starts them from the page's model dropdown.
"""

from __future__ import annotations

import asyncio
import builtins
import functools
import json
import os
import sys
import time
import urllib.parse
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


# Entry confidence = |predicted 5 s move| / scale, from 0 to 1. 0.40 trades from the first second and earns the most on the validation day
# (2023-12-15, never trained on): about +16 % over the stricter 0.72 that the model's "top 10 % of ticks" rule gives, and +48 % on the replay day.
# Lower is faster and busier but each trade is weaker; above ~0.6 it can sit idle for minutes. See model/TRANSFORMER.md. Override: TRANSFORMER_ENTRY.
DEFAULT_ENTRY = 0.40


def load_transformer_model():
    """The tick transformer and its short-hold policy: enter on a confident 5 s forecast, flatten after about 5 s.

    There is no take-profit; the trade ends at the hold limit (25 ticks = 5 s) or a 25 bps stop. Sizing aims for the largest position the
    rails allow, so `sizing_bps` is just above the exchange fees (0 when the server runs FEE_MODEL=solana)."""
    scorer = TransformerScorer()
    entry = float(os.environ.get("TRANSFORMER_ENTRY", DEFAULT_ENTRY))
    if not 0.0 <= entry <= 1.0:
        raise SystemExit(f"TRANSFORMER_ENTRY must be between 0 and 1, got {entry}.")
    params = SessionParams(
        take_profit_bps=0, stop_bps=25, max_hold_steps=25, cooldown_steps=5, threshold=entry,
        vol_gate=0.0, sizing_bps=TAKER_BPS + MAKER_BPS + 1.0,
    )
    print(f"transformer loaded (entry confidence {params.threshold:.2f}, hold {params.max_hold_steps * 0.2:.0f}s, stop {params.stop_bps:g} bps)")
    return scorer, params


def orphaned() -> bool:
    """True when the server that spawned this bot (MODEL_BOT_CHILD=1) has died and init has adopted the process."""
    return os.environ.get("MODEL_BOT_CHILD") == "1" and os.getppid() == 1


def prime(stream: TickStream, client: ModelClient, session_id: str | None = None) -> int:
    """Fill the transformer's 30 s window from GET /v1/bars (the server's recent 200 ms bars; at a session's open that is the 2-minute
    pre-roll), so it can trade at once instead of after 31 s. Returns the number of bars loaded; 0 when the endpoint is unavailable."""
    try:
        path = "/v1/bars?limit=601" + (f"&sessionId={urllib.parse.quote(session_id)}" if session_id else "")
        bars = client.request("GET", path).get("bars") or []
    except Exception as error:
        print(f"could not load history ({error}); warming up from live snapshots instead")
        return 0
    stream.snaps.clear()
    for bar in bars:
        stream.update({"ts": bar["ts"], "bids": [{"price": bar["bid"], "size": bar["bidSize"]}],
                       "asks": [{"price": bar["ask"], "size": bar["askSize"]}]})
    return len(bars)


async def session(base_url: str, policy: str = "session", session_id: str | None = None) -> None:
    """Trade one session. With `session_id` (from the supervisor in model/live.py), only that session,
    returning once it halts. Without one, the server's latest session, as before."""
    # Tag every line with the session, since one bot now trades several at once.
    print = functools.partial(builtins.print, f"[{session_id}]", flush=True) if session_id else builtins.print  # noqa: A001
    client = ModelClient(base_url, session_id=session_id)
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
    primed = False
    print(f"connected to {client.ws_url()}, policy threshold {params.threshold:.2f}")
    async with websockets.connect(client.ws_url(), max_queue=None) as socket:
        while True:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=IDLE_PING)
            except asyncio.TimeoutError:
                # Quiet stream: no live session is open. Keep the socket alive.
                await socket.send(json.dumps({"op": "ping"}))
                continue
            except websockets.ConnectionClosed:
                if orphaned():
                    raise SystemExit("the server that started this bot is gone")
                raise
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
                primed = False                        # the next session needs its own history
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
            if policy == "transformer" and not primed:
                primed = True
                loaded = prime(stream, client, session_id)
                print(f"loaded {loaded} history bars, ready to trade at once" if loaded else "no history, warming up")
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
                if session_id:
                    return
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
            if orphaned():
                raise SystemExit("the server that started this bot is gone")
            print(f"stream closed ({error!r}). Reconnecting.")
            await asyncio.sleep(1)


def main() -> None:
    """python -m model.live session       small session network (session_signal.pt), BTC-USDT only
    python -m model.live transformer   tick transformer (transformer_signal.pt), BTC-USDT only"""
    url = os.environ.get("MODEL_API_URL", "http://127.0.0.1:3000")
    asyncio.run(run(url, "transformer" if "transformer" in sys.argv[1:] else "session"))


if __name__ == "__main__":
    main()
