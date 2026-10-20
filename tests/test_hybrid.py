import pytest

from model.live import hybrid_config, model_policy
from model.policy import HybridConfig, IdSource, InstrumentLimits, book_imbalance, decide_hybrid

BTC = InstrumentLimits("BTC-USDT", 10, 100_000, 100_000)


def observation(bid_size=50, ask_size=50, position=0, stake=10_000, mid=8_000_000, **extra):
    base = {
        "market": "BTC-USDT",
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


def test_book_imbalance_is_signed_by_the_heavier_side():
    assert book_imbalance(observation(80, 20)) == pytest.approx(0.6)
    assert book_imbalance(observation(20, 80)) == pytest.approx(-0.6)


def test_enters_only_when_forecast_and_book_agree():
    ids = IdSource("t")
    buy = decide_hybrid(observation(90, 10), BTC, 3.0, 2.0, ids)
    assert buy.reason == "enter" and buy.orders[0]["side"] == "buy" and buy.orders[0]["size"] == 200
    assert decide_hybrid(observation(90, 10), BTC, 1.0, 2.0, ids).reason == "wait"  # forecast too weak
    assert decide_hybrid(observation(60, 40), BTC, 3.0, 2.0, ids).reason == "wait"  # book not leaning enough


def test_holds_until_the_forecast_fades_or_the_book_leans_hard_down():
    ids = IdSource("t")
    assert decide_hybrid(observation(40, 60, position=200), BTC, 1.0, 2.0, ids).reason == "hold"
    weak = decide_hybrid(observation(60, 40, position=200), BTC, -0.5, 2.0, ids)
    assert weak.reason == "exit" and weak.orders[0]["side"] == "sell" and weak.orders[0]["size"] == 200
    lean = decide_hybrid(observation(10, 90, position=200), BTC, 3.0, 2.0, ids)
    assert lean.reason == "exit" and lean.orders[0]["side"] == "sell"


def test_size_scales_down_with_a_small_stake():
    # A $5 stake at 200x leverage is $1,000 of notional: twelve $80 lots.
    buy = decide_hybrid(observation(90, 10, stake=500), BTC, 3.0, 2.0, IdSource("t"))
    assert buy.orders[0]["size"] == 12


def test_sends_nothing_while_halted_or_with_an_order_working():
    ids = IdSource("t")
    assert decide_hybrid(observation(90, 10, halted=True), BTC, 3.0, 2.0, ids).reason == "halted"
    working = [{"clientOrderId": "t-1", "side": "buy", "status": "pending"}]
    assert decide_hybrid(observation(90, 10, openOrders=working), BTC, 3.0, 2.0, ids).reason == "working"


@pytest.mark.parametrize("value,expected", [(None, "hybrid"), ("", "hybrid"), ("PACE", "pace"), ("hybrid", "hybrid")])
def test_policy_flag_defaults_to_hybrid(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("MODEL_POLICY", raising=False)
    else:
        monkeypatch.setenv("MODEL_POLICY", value)
    assert model_policy() == expected


def test_unknown_policy_is_refused(monkeypatch):
    monkeypatch.setenv("MODEL_POLICY", "yolo")
    with pytest.raises(SystemExit):
        model_policy()


def test_hybrid_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("HYBRID_LOTS", "50")
    monkeypatch.setenv("HYBRID_ENTER_IMBALANCE", "0.3")
    config = hybrid_config()
    assert config.lots == 50 and config.enter_imbalance == 0.3 and config.exit_bps == HybridConfig().exit_bps
