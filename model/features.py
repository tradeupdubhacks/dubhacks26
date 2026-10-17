"""Signals from one observation. Every input is already in that payload."""

from __future__ import annotations

FEATURE_NAMES = ("imbalance", "mid_change", "spread", "mid_var")


def features_from_observation(observation: dict) -> list[float] | None:
    bids = observation.get("bids") or []
    asks = observation.get("asks") or []
    if not bids or not asks:
        return None
    bid_size = sum(int(level["size"]) for level in bids)
    ask_size = sum(int(level["size"]) for level in asks)
    depth = bid_size + ask_size
    imbalance = (bid_size - ask_size) / depth if depth else 0.0

    mid = int(observation.get("mid") or 0)
    if mid <= 0:
        best_bid = int(bids[0]["price"])
        best_ask = int(asks[0]["price"])
        mid = (best_bid + best_ask) // 2
    if mid <= 0:
        return None
    spread = (int(asks[0]["price"]) - int(bids[0]["price"])) / mid

    mids = [int(value) for value in (observation.get("mids") or []) if int(value) > 0]
    if not mids or mids[-1] != mid:
        mids.append(mid)
    mid_change = (mids[-1] - mids[0]) / mids[0] if len(mids) >= 2 else 0.0
    returns = [
        (right - left) / left
        for left, right in zip(mids, mids[1:])
        if left
    ]
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        mid_var = sum((value - mean) ** 2 for value in returns) / len(returns)
    else:
        mid_var = 0.0
    return [imbalance, mid_change, spread, mid_var]
