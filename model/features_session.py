"""Session-model inputs. Every value comes from one model-API observation.

Fields read: bids[0], asks[0], mid, mids (up to 32, oldest first), lastTrade.
Nothing here uses position, equity, or the stake, so the signal does not depend on what the bot holds.
"""

from __future__ import annotations

import math

FEATURE_NAMES_SESSION = (
    "imbalance_top",
    "micro_dev_bps",
    "spread_bps",
    "ret_1_bps",
    "ret_5_bps",
    "ret_15_bps",
    "ret_31_bps",
    "vol_bps",
    "log_top_size",
    "trade_signed_log_size",
    "trade_vs_mid_bps",
)
VOL_INDEX = FEATURE_NAMES_SESSION.index("vol_bps")


def _ret_bps(mids: list[int], lag: int) -> float:
    if len(mids) <= lag or mids[-1 - lag] <= 0:
        return 0.0
    return (mids[-1] / mids[-1 - lag] - 1.0) * 1e4


def session_features(observation: dict) -> list[float] | None:
    bids = observation.get("bids") or []
    asks = observation.get("asks") or []
    if not bids or not asks:
        return None
    bid, ask = int(bids[0]["price"]), int(asks[0]["price"])
    bid_size, ask_size = int(bids[0]["size"]), int(asks[0]["size"])
    mid = int(observation.get("mid") or 0) or (bid + ask) // 2
    if mid <= 0 or ask <= bid:
        return None
    depth = bid_size + ask_size
    imbalance = (bid_size - ask_size) / depth if depth else 0.0
    micro = (ask * bid_size + bid * ask_size) / depth if depth else float(mid)
    mids = [int(value) for value in (observation.get("mids") or []) if int(value) > 0]
    if not mids or mids[-1] != mid:
        mids.append(mid)
    steps = [(right / left - 1.0) * 1e4 for left, right in zip(mids, mids[1:])]
    vol = math.sqrt(sum(value * value for value in steps) / len(steps)) if steps else 0.0
    trade = observation.get("lastTrade")
    if trade:
        sign = 1.0 if trade.get("side") == "buy" else -1.0
        trade_flow = sign * math.log1p(int(trade.get("size") or 0))
        trade_gap = (int(trade.get("price") or mid) / mid - 1.0) * 1e4
    else:
        trade_flow, trade_gap = 0.0, 0.0
    return [
        imbalance,
        (micro / mid - 1.0) * 1e4,
        (ask - bid) / mid * 1e4,
        _ret_bps(mids, 1),
        _ret_bps(mids, 5),
        _ret_bps(mids, 15),
        _ret_bps(mids, 31),
        vol,
        math.log1p(depth),
        trade_flow,
        trade_gap,
    ]
