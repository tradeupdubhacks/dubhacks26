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


def call(obs, expected, held=0, horizon=300, ids=None, idle_ms=0):
    return decide(obs, BTC, expected, 10.0, 0.0, horizon, held, ids or IdSource(), idle_ms)


def test_position_headroom_blocks_the_extra_lot():
    # The instrument cap is 10 lots. A full book does not buy an 11th.
    held = call(observation(positionLots=2), expected=40.0, held=300, idle_ms=5_000)
    assert held.orders
    assert held.orders[0]["side"] == "buy"
    decision = call(observation(positionLots=10), expected=40.0, held=300, idle_ms=5_000)
    assert decision.orders == []


def test_sell_signal_reduces_a_max_long():
    decision = call(observation(positionLots=10), expected=-40.0, held=300, idle_ms=5_000)
    assert decision.orders
    assert decision.orders[0]["side"] == "sell"
    assert decision.orders[0]["size"] == 1
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


def test_pace_repeats_on_each_five_second_window():
    position = 0
    ids = IdSource()
    for _ in range(6):
        decision = call(
            observation(positionLots=position, stakeCents=100_000),
            expected=1.5,
            held=10,
            idle_ms=5_000,
            ids=ids,
        )
        assert decision.reason == "pace"
        assert len(decision.orders) == 1
        order = decision.orders[0]
        assert order["size"] == 1
        position += order["size"] if order["side"] == "buy" else -order["size"]
    assert position == 6


def test_btc_lot_is_not_frozen_for_an_hour():
    # One lot is no longer the whole position. Five seconds of silence adds
    # another buy instead of selling the first one back.
    book = observation(
        mid=8_000_000,
        bids=[{"price": 7_999_990, "size": 40}],
        asks=[{"price": 8_000_000, "size": 25}],
        positionLots=1,
        stakeCents=10_000,
        floorCents=0,
        ceilingCents=20_000,
    )
    held = call(book, expected=40.0, held=50, horizon=18_000, idle_ms=4_999)
    assert held.orders == []
    decision = call(book, expected=40.0, held=50, horizon=18_000, idle_ms=5_000)
    assert decision.reason == "pace"
    assert decision.orders[0]["side"] == "buy"
    assert decision.orders[0]["size"] == 1
    # 80 bps of $80,000 is $64, past the few dollars the touch moves in 50ms.
    assert decision.orders[0]["price"] >= 8_000_000 + 5_000
    assert decision.orders[0]["price"] % 10 == 0


def test_btc_buy_crosses_through_the_latency_gap():
    decision = call(
        observation(
            mid=8_000_000,
            bids=[{"price": 7_999_990, "size": 40}],
            asks=[{"price": 8_000_000, "size": 25}],
            stakeCents=10_000,
            floorCents=0,
            ceilingCents=20_000,
        ),
        expected=0.2,
        idle_ms=5_000,
    )
    order = decision.orders[0]
    assert order["side"] == "buy"
    assert order["size"] == 1
    assert order["type"] == "ioc"
    assert order["price"] >= 8_000_000 + 5_000
    assert order["price"] % 10 == 0


def test_flat_book_trades_once_the_five_second_pace_is_due():
    waiting = call(observation(), expected=0.0, idle_ms=4_999)
    assert waiting.orders == []
    decision = call(observation(), expected=0.0, idle_ms=5_000)
    assert decision.reason == "pace"
    assert decision.orders[0]["side"] == "buy"
    assert decision.orders[0]["size"] == 1
    assert decision.orders[0]["type"] == "ioc"


def test_a_mild_dip_does_not_sell_the_long():
    decision = call(observation(positionLots=2, stakeCents=100_000), expected=-4.0, held=10, idle_ms=5_000)
    assert decision.orders[0]["side"] == "buy"
    assert decision.orders[0]["size"] == 1


def test_buys_continue_past_two_lots():
    # stake/price used to cap this book at one or two lots and the next pace sold.
    position = 0
    ids = IdSource()
    for _ in range(8):
        decision = call(
            observation(positionLots=position),
            expected=1.0,
            held=50,
            idle_ms=5_000,
            ids=ids,
        )
        assert decision.reason == "pace"
        assert decision.orders[0]["side"] == "buy"
        assert decision.orders[0]["size"] == 1
        position += 1
    assert position == 8


def test_full_book_is_held_instead_of_peeled():
    decision = call(observation(positionLots=10), expected=30.0, held=10, idle_ms=5_000)
    assert decision.orders == []
    assert decision.reason == "hold"


def test_wide_sol_spread_keeps_buying():
    # 16 cents on a $104 mid is the SOL reference ladder, and it is wider than 5 bps.
    # Each buy is many lots so the notional is about one BTC lot, and the next
    # paces keep buying instead of selling the clip back.
    limits = policy.InstrumentLimits("SOL-USD", 1, 2_000, 10_000)
    position = 0
    ids = IdSource()
    for _ in range(6):
        decision = policy.decide(
            observation(
                market="SOL-USD",
                mid=10_400,
                bids=[{"price": 10_392, "size": 500}],
                asks=[{"price": 10_408, "size": 500}],
                positionLots=position,
                stakeCents=10_000,
                floorCents=0,
                ceilingCents=20_000,
            ),
            limits,
            0.2,
            10.0,
            0.0,
            300,
            10,
            ids,
            5_000,
        )
        assert decision.reason == "pace"
        order = decision.orders[0]
        assert order["side"] == "buy"
        assert order["size"] > 1
        position += order["size"]
    assert position > 6


def test_rate_window_keeps_new_ids_under_the_documented_limit():
    sent = [0.0] * 18
    assert within_rate_limit(sent, now=0.2, additional=1, limit=18) is False
    assert within_rate_limit(sent, now=1.2, additional=1, limit=18) is True
