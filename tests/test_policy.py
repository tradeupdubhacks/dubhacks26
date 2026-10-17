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


def test_position_headroom_blocks_the_extra_lot():
    decision = decide(
        observation(positionLots=10),
        BTC,
        score=0.5,
        threshold=0.99,
        ids=IdSource(),
        features=[0.0, 0.0, 0.0001, 0.0],
    )
    assert all(order["side"] != "buy" for order in decision.orders)
    assert decision.orders
    assert decision.orders[0]["side"] == "sell"
    assert decision.orders[0]["size"] == 1


def test_halt_sends_nothing():
    decision = decide(observation(halted=True), BTC, 0.9, 0.2, IdSource(), [0.2, 0.0, 0.01, 0.0])
    assert decision == Decision(reason="halted")


def test_stale_feed_cancels_and_does_not_place():
    decision = decide(
        observation(priceSource="stale"),
        BTC,
        0.9,
        0.2,
        ids=IdSource(),
        features=[0.4, 0.0, 0.0001, 0.0],
    )
    assert decision.cancel_all is True
    assert decision.orders == []


def test_order_ids_are_not_reused():
    ids = IdSource()
    features = [0.0, 0.0, 0.0001, 0.0]
    first = decide(observation(), BTC, 0.5, 0.99, ids, features)
    second = decide(observation(), BTC, 0.5, 0.99, ids, features)
    seen = [order["clientOrderId"] for order in first.orders + second.orders]
    assert seen
    assert len(seen) == len(set(seen))


def test_near_rail_flattens_instead_of_quoting():
    price = 6000000
    assert notional_cents(price, 1) == 6000
    decision = decide(
        observation(stakeCents=6000, floorCents=0),
        BTC,
        0.5,
        0.99,
        IdSource(),
        [0.0, 0.0, 0.0001, 0.0],
    )
    assert decision.flatten is True
    assert decision.orders == []


def test_rate_window_keeps_new_ids_under_the_documented_limit():
    sent = [0.0] * 18
    assert within_rate_limit(sent, now=0.2, additional=1, limit=18) is False
    assert within_rate_limit(sent, now=1.2, additional=1, limit=18) is True
