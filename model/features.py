"""Causal features from 200ms bars. Live and training share this math.

A bar is (bid, ask, bid_size, ask_size, mid, trade_sign, trade_size, book)
with prices in cents and sizes in lots. trade_sign is the aggressor of the
last trade in that bar, or 0 when the bar had no trade. book is 1 when the
bid and ask are a real top of book.
"""

from __future__ import annotations

import numpy as np

BAR_MS = 200
LOOKBACK = 600
WARMUP = LOOKBACK + 1
SEQ_LEN = 64
# 15s, 30s, 60s, 2min, 5min. Short horizons rarely clear a 5 bps taker fee.
HORIZONS = (75, 150, 300, 600, 1500)

FEATURE_NAMES = (
    "imbalance",
    "imbalance_5s",
    "imbalance_30s",
    "micro_bps",
    "spread_bps",
    "ret_1s",
    "ret_5s",
    "ret_15s",
    "ret_30s",
    "ret_60s",
    "ret_120s",
    "vol_30s",
    "trade_sign",
    "flow_5s",
    "flow_30s",
    "book",
)
SEQ_NAMES = ("imbalance", "micro_bps", "ret_1bar", "trade_sign")
N_FEAT = len(FEATURE_NAMES)
N_SEQ = len(SEQ_NAMES)
INPUT_DIM = N_FEAT + N_SEQ * SEQ_LEN


def bar_from_observation(observation: dict) -> tuple | None:
    bids = observation.get("bids") or []
    asks = observation.get("asks") or []
    if not bids or not asks:
        return None
    bid = int(bids[0]["price"])
    ask = int(asks[0]["price"])
    if bid <= 0 or ask <= bid:
        return None
    mid = int(observation.get("mid") or (bid + ask) // 2)
    if mid <= 0:
        return None
    sign = 0
    size = 0
    trade = observation.get("lastTrade")
    if trade:
        trade_ts = int(trade.get("ts") or -1)
        obs_ts = int(observation.get("ts") or 0)
        if trade_ts // BAR_MS == obs_ts // BAR_MS:
            sign = 1 if trade.get("side") == "buy" else -1
            size = max(0, int(trade.get("size") or 0))
    return (
        bid,
        ask,
        max(0, int(bids[0]["size"])),
        max(0, int(asks[0]["size"])),
        mid,
        sign,
        size,
        1,
    )


def _lag_return(mid: np.ndarray, lag: int) -> np.ndarray:
    out = np.zeros(mid.shape[0], dtype=np.float64)
    if lag <= 0 or lag >= mid.shape[0]:
        return out
    prev = mid[:-lag]
    np.divide(mid[lag:] - prev, prev, out=out[lag:], where=prev > 0)
    out[lag:] *= 1e4
    return out


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    out = np.zeros(values.shape[0], dtype=np.float64)
    if window <= 1:
        return values.astype(np.float64)
    cumulative = np.cumsum(values)
    out[window - 1 :] = (cumulative[window - 1 :] - np.concatenate(([0.0], cumulative[:-window]))) / window
    return out


def _rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    out = np.zeros(values.shape[0], dtype=np.float64)
    cumulative = np.cumsum(values)
    out[window - 1 :] = cumulative[window - 1 :] - np.concatenate(([0.0], cumulative[:-window]))
    return out


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    total = _rolling_sum(values, window)
    squares = _rolling_sum(values * values, window)
    mean = total / window
    variance = np.maximum(squares / window - mean * mean, 0.0)
    std = np.sqrt(variance)
    std[: window - 1] = 0.0
    return std


def series_features(
    bid: np.ndarray,
    ask: np.ndarray,
    bid_sz: np.ndarray,
    ask_sz: np.ndarray,
    mid: np.ndarray,
    trade_sign: np.ndarray,
    trade_sz: np.ndarray,
    book: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (features[n, 16], channels[4, n]) using only past and current bars."""
    mid_f = mid.astype(np.float64)
    bid_f = bid.astype(np.float64)
    ask_f = ask.astype(np.float64)
    bid_sz_f = bid_sz.astype(np.float64)
    ask_sz_f = ask_sz.astype(np.float64)
    depth = bid_sz_f + ask_sz_f
    has_book = (book > 0) & (depth > 0) & (mid_f > 0)
    imbalance = np.zeros(mid_f.shape[0], dtype=np.float64)
    imbalance[has_book] = (bid_sz_f[has_book] - ask_sz_f[has_book]) / depth[has_book]
    micro = np.where(has_book, (ask_f * bid_sz_f + bid_f * ask_sz_f) / np.maximum(depth, 1.0), mid_f)
    micro_bps = np.zeros(mid_f.shape[0], dtype=np.float64)
    np.divide(micro - mid_f, mid_f, out=micro_bps, where=mid_f > 0)
    micro_bps *= 1e4
    spread_bps = np.zeros(mid_f.shape[0], dtype=np.float64)
    spread_bps[has_book] = (ask_f[has_book] - bid_f[has_book]) / mid_f[has_book] * 1e4
    ret_1 = _lag_return(mid_f, 1)
    signed = trade_sign.astype(np.float64) * trade_sz.astype(np.float64)
    size = trade_sz.astype(np.float64)
    columns = [
        imbalance,
        _rolling_mean(imbalance, 25),
        _rolling_mean(imbalance, 150),
        micro_bps,
        spread_bps,
        _lag_return(mid_f, 5),
        _lag_return(mid_f, 25),
        _lag_return(mid_f, 75),
        _lag_return(mid_f, 150),
        _lag_return(mid_f, 300),
        _lag_return(mid_f, 600),
        _rolling_std(ret_1, 150),
        trade_sign.astype(np.float64),
        _rolling_sum(signed, 25) / (_rolling_sum(size, 25) + 1.0),
        _rolling_sum(signed, 150) / (_rolling_sum(size, 150) + 1.0),
        (book > 0).astype(np.float64),
    ]
    features = np.stack(columns, axis=1).astype(np.float32)
    channels = np.stack([imbalance, micro_bps, ret_1, trade_sign.astype(np.float64)], axis=0).astype(np.float32)
    return features, channels


def pack_window(features: np.ndarray, channels: np.ndarray) -> np.ndarray:
    """Pack the last bar of a window into the network input."""
    index = features.shape[0] - 1
    seq = channels[:, index - SEQ_LEN + 1 : index + 1]
    return np.concatenate([features[index], seq.reshape(-1)]).astype(np.float32)


def features_from_bars(bars: list[tuple]) -> list[float] | None:
    if len(bars) < WARMUP:
        return None
    window = bars[-WARMUP:]
    columns = [np.asarray([bar[i] for bar in window]) for i in range(8)]
    features, channels = series_features(*columns)
    if not np.isfinite(features[-1]).all() or not np.isfinite(channels[:, -1]).all():
        return None
    return pack_window(features, channels).tolist()


def features_from_observation(observation: dict, history: list[tuple] | None = None) -> list[float] | None:
    """One observation is not a long enough window. Pass the rolling bar history."""
    bar = bar_from_observation(observation)
    if bar is None:
        return None
    bars = list(history or [])
    bars.append(bar)
    return features_from_bars(bars)
