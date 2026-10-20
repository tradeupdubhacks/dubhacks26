import pytest

from model.hft import Fees, HftConfig, HftTrader, imbalance
from model.live import strategy
from model.policy import IdSource, InstrumentLimits

BTC = InstrumentLimits("BTC-USDT", 10, 100_000, 100_000)
SOLANA = Fees(model="solana", taker_bps=0, solana_cents=0.1)


def observation(bid_size=50, ask_size=50, position=0, ts=0, stake=10_000, mid=8_000_000, **extra):
    base = {
        "market": "BTC-USDT",
        "ts": ts,
        "mid": mid,
        "bids": [{"price": mid - 5, "size": bid_size}],
        "asks": [{"price": mid + 5, "size": ask_size}],
        "positionLots": position,
        "openOrders": [],
        "stakeCents": stake,
        "halted": False,
        "priceSource": "tape",
    }
    base.update(extra)
    return base


def trader(**config):
    return HftTrader(HftConfig(**config), SOLANA)


def test_imbalance_is_signed_by_the_heavier_side():
    assert imbalance(observation(80, 20)) == pytest.approx(0.6)
    assert imbalance(observation(20, 80)) == pytest.approx(-0.6)


def test_enters_long_when_bids_outweigh_asks_and_short_when_asks_do():
    long = trader().decide(observation(90, 10), BTC, IdSource("t"))
    assert long.reason == "enter_long" and long.orders[0]["side"] == "buy" and long.orders[0]["type"] == "ioc"
    short = trader().decide(observation(10, 90), BTC, IdSource("t"))
    assert short.reason == "enter_short" and short.orders[0]["side"] == "sell"


def test_no_trade_on_a_balanced_book():
    assert trader().decide(observation(55, 45), BTC, IdSource("t")).reason == "no_signal"


def test_holds_while_the_book_leans_its_way_and_exits_when_it_turns():
    t = trader()
    assert t.decide(observation(90, 10, position=100, ts=0), BTC, IdSource("t")).reason == "hold"
    assert t.decide(observation(70, 30, position=100, ts=1_000), BTC, IdSource("t")).reason == "hold"
    out = t.decide(observation(40, 60, position=100, ts=1_200), BTC, IdSource("t"))
    assert out.reason == "exit_turned" and out.orders[0] == {**out.orders[0], "side": "sell", "size": 100}


def test_exits_after_the_hold_limit_even_if_the_signal_stays():
    t = trader(max_hold_s=5)
    t.decide(observation(10, 90, position=-100, ts=0), BTC, IdSource("t"))
    out = t.decide(observation(10, 90, position=-100, ts=5_000), BTC, IdSource("t"))
    assert out.reason == "exit_timeout" and out.orders[0]["side"] == "buy" and out.orders[0]["size"] == 100


def test_size_is_capped_by_leverage_on_the_stake():
    # A $5 stake at 150x is $750 of notional: nine $80 lots.
    assert trader().size(8_000_000, 500, BTC) == 9
    assert trader().size(8_000_000, 10_000, BTC) == 150


def test_round_trips_report_cash_including_fees():
    t = trader()
    assert t.on_events([{"type": "fill", "clientOrderId": "t-1", "side": "sell", "price": 8_000_000, "size": 100, "fee": 0}], "t") == []
    closed = t.on_events([{"type": "fill", "clientOrderId": "t-2", "side": "buy", "price": 7_999_000, "size": 100, "fee": 1}], "t")
    # Shorted 100 lots (0.1 BTC) at $80,000.00 and covered at $79,990.00: 100 cents, less the 1-cent fee.
    assert closed == [pytest.approx(99.0)]


def test_ignores_fills_from_other_bots():
    t = trader()
    t.on_events([{"type": "fill", "clientOrderId": "other-1", "side": "buy", "price": 8_000_000, "size": 3, "fee": 0}], "t")
    assert t.lots == 0


def test_reads_fees_from_the_instruments_body():
    fees = Fees.from_instruments({"fees": {"model": "solana", "makerBps": 0, "takerBps": 0, "solanaFeeCents": 0.103}})
    assert fees.model == "solana" and fees.taker_bps == 0 and fees.on(80_000) == pytest.approx(0.103)


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
