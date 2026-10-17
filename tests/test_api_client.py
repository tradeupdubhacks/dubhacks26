import urllib.error
import urllib.request

import pytest

from model.client import ModelClient


def _up() -> bool:
    try:
        urllib.request.urlopen("http://localhost:3000/v1/health", timeout=0.5)
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


server = pytest.mark.skipif(not _up(), reason="model API is not running on port 3000")


@server
def test_health_is_fake_execution():
    body = ModelClient().health()
    assert body["ok"] is True
    assert body["execution"] == "fake"


@server
def test_reset_is_refused_while_a_session_is_open():
    client = ModelClient()
    account = client.account()
    if account.get("stakeCents") is None:
        pytest.skip("no live session is open")
    with pytest.raises(Exception) as caught:
        client.reset({"instrument": "BTC-USDT"})
    assert getattr(caught.value, "code", "") == "session_open"


@server
def test_post_only_ack_uses_documented_fields():
    client = ModelClient()
    account = client.account()
    if account.get("halted"):
        pytest.skip("session is halted")
    if account.get("stakeCents") is None and account.get("clock") != "step":
        client.reset({"instrument": "BTC-USDT", "episodeSteps": 10})
        client.step({"dtMs": 100})
        account = client.account()
    asks = account.get("asks") or []
    if not asks:
        pytest.skip("book has no ask")
    tick = 10 if account.get("market") == "BTC-USDT" else 1
    price = int(asks[0]["price"]) - tick
    body = client.request(
        "POST",
        "/v1/orders",
        {
            "clientOrderId": "contract-probe-1",
            "side": "buy",
            "size": 1,
            "type": "post_only",
            "price": price,
        },
    )
    event = body["events"][0]
    assert event["type"] in ("ack", "reject")
    assert "clientOrderId" in event
