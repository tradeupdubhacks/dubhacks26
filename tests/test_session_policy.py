import math

from model.features_session import FEATURE_NAMES_SESSION, session_features
from model.policy import IdSource, InstrumentLimits
from model.session_policy import SessionParams, SessionState, decide_session
from model.sizing import plan_trade

BTC = InstrumentLimits("BTC-USDT", 10, 10, 10)


def observation(**overrides):
    base = {
        "market": "BTC-USDT", "mid": 8_400_000, "priceSource": "tape",
        "bids": [{"price": 8_399_990, "size": 400}], "asks": [{"price": 8_400_010, "size": 100}],
        "mids": [8_399_000 + 20 * i for i in range(32)],
        "lastTrade": {"price": 8_400_010, "size": 3, "side": "buy", "ts": 10},
        "positionLots": 0, "unrealizedPnlCents": 0, "openOrders": [], "halted": False,
        "stakeCents": 10_000, "floorCents": 9_800, "ceilingCents": 10_100,
    }
    base.update(overrides)
    return base


def test_features_have_the_documented_width_and_signs():
    f = session_features(observation())
    assert len(f) == len(FEATURE_NAMES_SESSION)
    assert f[0] > 0 and f[1] > 0            # bid-heavy book, micro-price above mid
    assert f[6] > 0 and f[9] > 0            # rising mids, buy print
    assert all(math.isfinite(v) for v in f)


def test_features_need_a_two_sided_book():
    assert session_features(observation(bids=[])) is None


def test_plan_never_risks_more_than_the_floor_margin():
    plan = plan_trade(10_000, 9_800, 10_100, 8_400_000, 10, 25, 12.5)
    assert plan is not None and 1 <= plan.lots <= 10
    assert plan.max_loss_cents <= 10_000 - 9_800


def test_plan_refuses_when_take_profit_does_not_cover_fees():
    assert plan_trade(10_000, 9_800, 10_100, 8_400_000, 10, 6, 6) is None


def test_plan_refuses_at_the_floor():
    assert plan_trade(9_800, 9_800, 10_100, 8_400_000, 10, 25, 12.5) is None


def test_entry_is_a_tick_aligned_ioc_with_a_price_cap():
    d = decide_session(observation(), BTC, 0.9, session_features(observation()), SessionParams(vol_gate=0.0), SessionState(), IdSource())
    assert d.reason == "entry" and len(d.orders) == 1
    order = d.orders[0]
    assert order["type"] == "ioc" and order["side"] == "buy" and order["price"] % 10 == 0
    assert 1 <= order["size"] <= BTC.max_order_lots


def test_low_confidence_does_not_trade():
    params = SessionParams(threshold=0.5)
    d = decide_session(observation(), BTC, 0.55, session_features(observation()), params, SessionState(), IdSource())
    assert d.reason == "no_signal" and not d.orders


def test_halted_and_stale_send_nothing_or_cancel():
    assert decide_session(observation(halted=True), BTC, 0.9, [0.0] * 11, SessionParams(), SessionState(), IdSource()).reason == "halted"
    d = decide_session(observation(priceSource="stale"), BTC, 0.9, [0.0] * 11, SessionParams(), SessionState(), IdSource())
    assert d.cancel_all and not d.orders


def test_open_position_gets_one_take_profit_then_holds():
    obs = observation(positionLots=5, unrealizedPnlCents=0)
    state, ids, params = SessionState(), IdSource(), SessionParams(take_profit_bps=25, stop_bps=25)
    first = decide_session(obs, BTC, 0.9, [0.0] * 11, params, state, ids)
    assert first.reason == "take_profit"
    order = first.orders[0]
    assert order["side"] == "sell" and order["size"] == 5 and order["type"] == "limit" and order["price"] > 8_400_000
    held = decide_session(observation(positionLots=5, openOrders=[{**order, "status": "open"}]), BTC, 0.9, [0.0] * 11, params, state, ids)
    assert held.reason == "hold" and not held.orders


def test_stop_flattens_when_the_loss_reaches_the_stop():
    obs = observation(positionLots=5, unrealizedPnlCents=-200)     # 5 lots * $84 * 25 bps ~ 105 cents
    d = decide_session(obs, BTC, 0.9, [0.0] * 11, SessionParams(stop_bps=25), SessionState(), IdSource())
    assert d.flatten and d.reason == "stop"
