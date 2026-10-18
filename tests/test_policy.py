import model.policy as policy
from model.policy import Decision, IdSource, InstrumentLimits, decide, notional_cents, round_tick, within_rate_limit


BTC = InstrumentLimits("BTC-USDT", 10, 10, 10)


def observation(**overrides):
    base = {
        "market": "BTC-USDT",
        "clock": "wall",
        "ts": 1200,
        "mid": 6000000,
        "priceSource": "tape",
        "bids": [{"price": 5999990, "size": 40}],
        "asks": [{"price": 6000010, "size": 25}],
        "mids": [5999900, 6000000],
        "positionLots": 0,
        "openOrders": [],
        "halted": False,
        "haltReason": None,
        "stakeCents": 10000,
        "floorCents": 0,
        "ceilingCents": 200000,
        "freeCashCents": 100000,
        "equityCents": 100000,
    }
    base.update(overrides)
    return base


def test_tick_rounding_matches_btc_increment():
    assert round_tick(6000001, 10, "buy") == 6000000
    assert round_tick(6000001, 10, "sell") == 6000010
    assert round_tick(10400, 1, "buy") == 10400


def call(obs, expected, held=0, horizon=300, ids=None):
    return decide(obs, BTC, expected, 10.0, 0.0, horizon, held, ids or IdSource())


def test_position_headroom_blocks_the_extra_lot():
    decision = call(observation(positionLots=1), expected=40.0, held=300)
    assert decision.orders == []


def test_sell_signal_reduces_a_max_long():
    decision = call(observation(positionLots=10), expected=-40.0, held=300)
    assert decision.orders
    assert decision.orders[0]["side"] == "sell"
    assert decision.orders[0]["type"] == "ioc"


def test_halt_sends_nothing():
    decision = call(observation(halted=True), expected=40.0)
    assert decision == Decision(reason="halted")


def test_stale_feed_cancels_and_does_not_place():
    decision = call(observation(priceSource="stale"), expected=40.0)
    assert decision.cancel_all is True
    assert decision.orders == []


def test_order_ids_are_not_reused():
    ids = IdSource()
    first = call(observation(), expected=30.0, ids=ids)
    second = call(observation(), expected=30.0, ids=ids)
    seen = [order["clientOrderId"] for order in first.orders + second.orders]
    assert seen
    assert len(seen) == len(set(seen))


def test_flat_signal_does_not_quote_the_touch():
    decision = call(observation(), expected=0.0)
    assert decision.orders == []
    assert decision.reason == "hold"


def test_open_position_is_held_for_the_forecast():
    decision = call(observation(positionLots=1), expected=-40.0, held=10)
    assert decision.orders == []
    assert decision.reason == "hold"


def test_five_dollar_stake_can_enter():
    decision = call(observation(stakeCents=500, floorCents=0, ceilingCents=1000), expected=30.0)
    assert decision.orders
    assert decision.orders[0]["side"] == "buy"
    assert decision.orders[0]["type"] == "ioc"


def test_near_rail_flattens_instead_of_quoting():
    price = 6000000
    assert notional_cents(price, 1) == 6000
    decision = call(observation(stakeCents=20, floorCents=0, positionLots=1), expected=0.0)
    assert decision.flatten is True
    assert decision.orders == []


def test_rate_window_keeps_new_ids_under_the_documented_limit():
    sent = [0.0] * 18
    assert within_rate_limit(sent, now=0.2, additional=1, limit=18) is False
    assert within_rate_limit(sent, now=1.2, additional=1, limit=18) is True
