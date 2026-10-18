import pytest

from model.hft import Fees, HftConfig, HftTrader
from model.live import strategy
from model.policy import IdSource, InstrumentLimits

BTC = InstrumentLimits("BTC-USDT", 10, 100_000, 100_000)
SOLANA = Fees(model="solana", taker_bps=0, solana_cents=0.1)


def observation(mid, position=0, stake=10_000, spread=10, **extra):
    base = {
        "market": "BTC-USDT",
        "mid": mid,
        "bids": [{"price": mid - spread // 2, "size": 50}],
        "asks": [{"price": mid + spread // 2, "size": 50}],
        "positionLots": position,
        "openOrders": [],
        "stakeCents": stake,
        "halted": False,
        "priceSource": "tape",
    }
    base.update(extra)
    return base


def test_buys_on_a_one_tick_downtick_and_not_on_an_uptick():
    trader = HftTrader(HftConfig(), SOLANA)
    assert trader.decide(observation(8_000_000), BTC, IdSource("t")).reason == "no_downtick"  # first look
    assert trader.decide(observation(8_000_010), BTC, IdSource("t")).reason == "no_downtick"
    decision = trader.decide(observation(8_000_000), BTC, IdSource("t"))
    assert decision.reason == "buy_downtick"
    assert decision.orders[0]["side"] == "buy" and decision.orders[0]["type"] == "ioc"


def test_size_lets_one_tick_cover_the_round_trip_fees_twice():
    trader = HftTrader(HftConfig(tick_cover=2), SOLANA)
    lots = trader.size(8_000_000, 10_000, BTC)
    # A 10-cent tick on a 0.001 BTC lot is 0.01 cents. Round trip is 0.2 cents, covered twice: 40 lots.
    assert lots == 40
    assert lots * BTC.price_tick_cents / 1000 >= 2 * 2 * SOLANA.solana_cents


def test_size_is_capped_by_leverage_on_the_stake():
    trader = HftTrader(HftConfig(max_leverage=50), SOLANA)
    # A $5 stake at 50x is $250 of notional: three $80 lots.
    assert trader.size(8_000_000, 500, BTC) == 3


def test_exchange_fees_fall_back_to_one_lot():
    trader = HftTrader(HftConfig(), Fees(model="exchange", taker_bps=5, solana_cents=0))
    assert trader.size(8_000_000, 10_000, BTC) == 1


def test_sells_on_the_first_uptick_that_beats_the_solana_fee():
    trader = HftTrader(HftConfig(), SOLANA)
    trader.on_events([{"type": "fill", "clientOrderId": "t-1", "side": "buy", "price": 8_000_005, "size": 40, "fee": 0}], "t")
    trader.last_mid = 8_000_000
    # Bid equal to the price paid: the sell fee makes it a loss, so hold.
    assert trader.decide(observation(8_000_010, position=40), BTC, IdSource("t")).reason == "hold"
    # Bid one tick above the price paid: 0.4 cents gained beats the 0.1-cent fee.
    decision = trader.decide(observation(8_000_020, position=40), BTC, IdSource("t"))
    assert decision.reason == "sell_uptick"
    assert decision.orders[0]["side"] == "sell" and decision.orders[0]["size"] == 40


def test_reads_fees_from_the_instruments_body():
    fees = Fees.from_instruments({"fees": {"model": "solana", "makerBps": 0, "takerBps": 0, "solanaFeeCents": 0.103}})
    assert fees.model == "solana" and fees.taker_bps == 0 and fees.on(80_000) == pytest.approx(0.103)


def test_ignores_fills_from_other_bots():
    trader = HftTrader(HftConfig(), SOLANA)
    trader.on_events([{"type": "fill", "clientOrderId": "other-1", "side": "buy", "price": 8_000_000, "size": 3, "fee": 0}], "t")
    assert trader.lots == 0


@pytest.mark.parametrize("value,expected", [(None, "model"), ("", "model"), ("model", "model"), ("HFT", "hft")])
def test_feature_flag_defaults_to_the_model(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("BOT_STRATEGY", raising=False)
    else:
        monkeypatch.setenv("BOT_STRATEGY", value)
    assert strategy() == expected


def test_unknown_strategy_is_refused(monkeypatch):
    monkeypatch.setenv("BOT_STRATEGY", "dip")
    with pytest.raises(SystemExit):
        strategy()
