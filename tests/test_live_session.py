"""Drive model.live.session (policy "session") over a fake /v1/stream and check the commands it sends."""
import asyncio
import json

import pytest

pytest.importorskip("websockets")

import model.live_session as live
from model.session_policy import SessionParams


def snapshot(**overrides):
    obs = {
        "market": "BTC-USDT", "clock": "wall", "ts": 1000, "mid": 8_400_000, "priceSource": "tape",
        "bids": [{"price": 8_399_990, "size": 400}], "asks": [{"price": 8_400_010, "size": 100}],
        "mids": [8_399_000 + 20 * i for i in range(32)],
        "lastTrade": {"price": 8_400_010, "size": 3, "side": "buy", "ts": 900},
        "positionLots": 0, "freeCashCents": 100000, "equityCents": 100000, "realizedPnlCents": 0,
        "unrealizedPnlCents": 0, "openOrders": [], "halted": False, "haltReason": None,
        "stakeCents": 10_000, "floorCents": 9_800, "ceilingCents": 10_100, "step": 0, "done": False,
    }
    obs.update(overrides)
    return json.dumps({"type": "snapshot", "observation": obs})


class FakeSocket:
    def __init__(self, messages):
        self.messages, self.sent = list(messages), []

    async def recv(self):
        if not self.messages:
            raise RuntimeError("end of script")
        return self.messages.pop(0)

    async def send(self, text):
        self.sent.append(json.loads(text))


class FakeConnect:
    def __init__(self, socket):
        self.socket = socket

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, base_url, *args, **kwargs):
        pass

    def health(self):
        return {"ok": True, "execution": "fake"}

    def instruments(self):
        return {"instruments": [{"symbol": "BTC-USDT", "priceTickCents": 10, "maxOrderLots": 10, "maxPositionLots": 10}]}

    def request(self, method, path):
        return {"bars": getattr(FakeClient, "bars", [])}

    def ws_url(self):
        return "ws://localhost:3000/v1/stream"


def run_script(monkeypatch, messages):
    socket = FakeSocket(messages)
    monkeypatch.setattr(live, "ModelClient", FakeClient)
    monkeypatch.setattr(live, "DECISION_INTERVAL", 0.0)
    monkeypatch.setattr(live.websockets, "connect", FakeConnect(socket), raising=False)
    monkeypatch.setattr(live, "load_session_model", lambda: (lambda features: 0.95, SessionParams(threshold=0.0, vol_gate=0.0)))
    with pytest.raises(RuntimeError):
        asyncio.run(live.session("http://localhost:3000", "session"))
    return [m for m in socket.sent if m.get("op") != "ping"]


def test_flat_confident_snapshot_places_one_ioc(monkeypatch):
    sent = run_script(monkeypatch, [snapshot()])
    assert [m["op"] for m in sent] == ["place"]
    order = sent[0]["order"]
    assert order["type"] == "ioc" and order["side"] == "buy" and order["price"] % 10 == 0 and 1 <= order["size"] <= 10


def test_open_position_gets_a_take_profit_then_a_stop(monkeypatch):
    sent = run_script(monkeypatch, [snapshot(positionLots=4), snapshot(positionLots=4, unrealizedPnlCents=-500)])
    assert sent[0]["op"] == "place" and sent[0]["order"]["type"] == "limit" and sent[0]["order"]["side"] == "sell"
    assert sent[1] == {"op": "flatten"}


def test_halted_and_foreign_markets_send_nothing(monkeypatch):
    sent = run_script(monkeypatch, [snapshot(halted=True, haltReason="ceiling"), snapshot(market="SOL-USD")])
    assert sent == []


def test_session_profiles_idle_uses_the_artifact_and_active_trades_more(monkeypatch):
    idle_score, idle = live.load_session_model(profile="idle")
    active_score, active = live.load_session_model(profile="active")
    assert idle.threshold > 0.7 and active.threshold == 0.30
    assert 0.0 <= active_score([0.0] * 11) <= 1.0
    monkeypatch.setenv("MODEL_PROFILE", "idle")
    assert live.load_session_model()[1].threshold == idle.threshold
    monkeypatch.setenv("MODEL_PROFILE", "nonsense")
    with pytest.raises(SystemExit):
        live.load_session_model()


def test_transformer_entry_confidence_defaults_to_0_40_and_can_be_overridden(monkeypatch):
    class Stub:
        entry_threshold = 0.72
    monkeypatch.setattr(live, "TransformerScorer", lambda: Stub())
    monkeypatch.delenv("TRANSFORMER_ENTRY", raising=False)
    assert live.load_transformer_model()[1].threshold == 0.40
    monkeypatch.setenv("TRANSFORMER_ENTRY", "0.55")
    assert live.load_transformer_model()[1].threshold == 0.55
    monkeypatch.setenv("TRANSFORMER_ENTRY", "7")
    with pytest.raises(SystemExit):
        live.load_transformer_model()
