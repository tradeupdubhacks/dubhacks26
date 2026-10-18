import asyncio
import json

import numpy as np
import pytest

pytest.importorskip("websockets")
pytest.importorskip("torch")

import model.live_session as live
from model.session_policy import SessionParams
from model.transformer_signal import ARTIFACT, HISTORY, TICK_MS, WIN, TickStream, TransformerScorer, grid_features
from tests.test_live_session import FakeClient, FakeConnect, FakeSocket


def book(n=400, seed=3):
    rng = np.random.default_rng(seed)
    mid = 8_400_000 + np.cumsum(rng.integers(-20, 21, n) * 10)               # cents, 10-cent tick
    bid = mid - 5
    ask = mid + 5
    bsz = rng.integers(200, 3000, n)                                          # lots
    asz = rng.integers(200, 3000, n)
    return bid, ask, bsz, asz


def observation(i, bid, ask, bsz, asz, ts=None):
    return {"market": "BTC-USDT", "ts": i * TICK_MS if ts is None else ts, "mid": int((bid[i] + ask[i]) // 2), "priceSource": "tape",
            "bids": [{"price": int(bid[i]), "size": int(bsz[i])}], "asks": [{"price": int(ask[i]), "size": int(asz[i])}],
            "mids": [], "lastTrade": None, "positionLots": 0, "openOrders": [], "halted": False,
            "stakeCents": 10_000, "floorCents": 9_800, "ceilingCents": 10_100, "unrealizedPnlCents": 0}


def test_live_stream_builds_exactly_the_training_features():
    bid, ask, bsz, asz = book()
    train = grid_features(bid / 100, ask / 100, bsz * 0.001, asz * 0.001)       # what training sees
    stream = TickStream()
    for i in range(300):
        stream.update(observation(i, bid, ask, bsz, asz))
        stream.update(observation(i, bid, ask, bsz, asz, ts=i * TICK_MS + 60))  # extra snapshot, same book (any spacing is fine)
        if i >= HISTORY:
            live_rows = grid_features(*stream.arrays())[-WIN:]
            assert np.allclose(live_rows, train[i - WIN + 1:i + 1], atol=1e-5), i


def test_stream_is_not_ready_until_enough_history():
    bid, ask, bsz, asz = book()
    stream = TickStream()
    for i in range(HISTORY - 2):
        stream.update(observation(i, bid, ask, bsz, asz))
    assert stream.arrays() is None
    for i in range(HISTORY - 2, HISTORY + 3):
        stream.update(observation(i, bid, ask, bsz, asz))
    assert stream.arrays() is not None


def test_a_restarted_market_clock_clears_the_history():
    bid, ask, bsz, asz = book()
    stream = TickStream()
    for i in range(HISTORY + 5):
        stream.update(observation(i, bid, ask, bsz, asz))
    stream.update(observation(0, bid, ask, bsz, asz))                           # ts goes back to 0: a new session
    assert stream.arrays() is None


@pytest.mark.skipif(not ARTIFACT.exists(), reason="transformer artifact not trained")
def test_trained_scorer_predicts_finite_values_and_scores_stay_in_range():
    bid, ask, bsz, asz = book()
    scorer, stream = TransformerScorer(), TickStream()
    assert scorer.score(stream) == 0.5                                           # not ready: no opinion
    for i in range(HISTORY + 5):
        stream.update(observation(i, bid, ask, bsz, asz))
    pred = scorer.predict(stream)
    assert pred is not None and np.isfinite(pred)
    assert 0.0 <= scorer.score(stream) <= 1.0
    assert 0.0 < scorer.entry_threshold < 1.0 and scorer.scale > 0


def test_live_transformer_mode_enters_on_a_confident_score(monkeypatch):
    class Confident:
        def score(self, stream):
            return 0.95

    bid, ask, bsz, asz = book(60)
    messages = [json.dumps({"type": "snapshot", "observation": observation(i, bid, ask, bsz, asz)}) for i in range(6)]
    socket = FakeSocket(messages)
    monkeypatch.setattr(live, "ModelClient", FakeClient)
    monkeypatch.setattr(live, "DECISION_INTERVAL", 0.0)
    monkeypatch.setattr(live.websockets, "connect", FakeConnect(socket), raising=False)
    monkeypatch.setattr(live, "load_transformer_model", lambda: (Confident(), SessionParams(take_profit_bps=0, stop_bps=25, max_hold_steps=25, cooldown_steps=5, threshold=0.5, vol_gate=0.0, sizing_bps=1.0)))
    with pytest.raises(RuntimeError):
        asyncio.run(live.session("http://localhost:3000", "transformer"))
    places = [m for m in socket.sent if m.get("op") == "place"]
    assert len(places) == 1                                                       # one entry, not one per snapshot (cooldown + in-flight view)
    order = places[0]["order"]
    assert order["type"] == "ioc" and order["side"] == "buy" and 1 <= order["size"] <= 10
