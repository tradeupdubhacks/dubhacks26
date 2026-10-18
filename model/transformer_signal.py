"""Transformer over the last 30 s of top-of-book ticks. Predicts the BTC mid-price move over the next 5 s, in bps.

The same `grid_features` builds the inputs for training (on a regular 200 ms grid recorded from Binance / the replay) and for the live bot
(`TickStream` resamples the server's snapshots onto that grid), so the two cannot drift apart.

Inputs per tick (11): book imbalance, micro-price offset, spread, log total size, log bid size, log ask size, 1-tick mid return,
1-tick change of the two log sizes, 5-tick change of the imbalance and of the bid price. Window = 150 ticks = 30 s.
Everything is read from `bids[0]`, `asks[0]` and `ts` of a model-API observation.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np
import torch
from torch import nn

TICK_MS = 200
WIN = 150
LOOKBACK = 5                       # longest lag used by a feature
HISTORY = WIN + LOOKBACK
FEATURE_NAMES = (
    "imbalance", "micro_dev_bps", "spread_bps", "log_depth", "log_bid_size", "log_ask_size",
    "mid_ret_bps", "d_log_bid_size", "d_log_ask_size", "d_imbalance_5", "d_bid_5_bps",
)
ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "transformer_signal.pt"


def _lag(values: np.ndarray, k: int) -> np.ndarray:
    return np.concatenate([np.full(k, values[0]), values[:-k]])


def grid_features(bid: np.ndarray, ask: np.ndarray, bid_qty: np.ndarray, ask_qty: np.ndarray) -> np.ndarray:
    """(N, 11) features from top-of-book arrays on the 200 ms grid. Prices in dollars, sizes in BTC."""
    bid, ask, bq, aq = (np.asarray(a, dtype=np.float64) for a in (bid, ask, bid_qty, ask_qty))
    mid = (bid + ask) / 2
    imbalance = (bq - aq) / (bq + aq)
    micro = (ask * bq + bid * aq) / (bq + aq)
    log_bq, log_aq = np.log1p(bq * 1000), np.log1p(aq * 1000)
    ret = np.r_[0.0, np.diff(mid) / mid[:-1]] * 1e4
    columns = [
        imbalance, (micro / mid - 1) * 1e4, (ask - bid) / mid * 1e4, np.log1p((bq + aq) * 1000), log_bq, log_aq,
        ret, log_bq - _lag(log_bq, 1), log_aq - _lag(log_aq, 1), imbalance - _lag(imbalance, 5), (bid - _lag(bid, 5)) / mid * 1e4,
    ]
    return np.stack(columns, 1).astype(np.float32)


class TickNet(nn.Module):
    def __init__(self, n_features: int = len(FEATURE_NAMES), d: int = 64, layers: int = 2, heads: int = 4) -> None:
        super().__init__()
        self.inp = nn.Linear(n_features, d)
        self.pos = nn.Parameter(torch.zeros(1, WIN, d))
        self.enc = nn.TransformerEncoder(nn.TransformerEncoderLayer(d, heads, 128, 0.1, batch_first=True, norm_first=True), layers)
        self.head = nn.Sequential(nn.LayerNorm(2 * d), nn.Linear(2 * d, 32), nn.ReLU(), nn.Linear(32, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.enc(self.inp(x) + self.pos)
        return self.head(torch.cat([h[:, -1], h.mean(1)], 1)).squeeze(-1)


class TickStream:
    """Collects model-API snapshots (any spacing) and exposes the last HISTORY ticks of the 200 ms grid."""

    def __init__(self) -> None:
        self.snaps: deque[tuple[float, float, float, float, float]] = deque(maxlen=4000)

    def update(self, observation: dict) -> bool:
        bids, asks = observation.get("bids") or [], observation.get("asks") or []
        ts = observation.get("ts")
        if not bids or not asks or ts is None:
            return False
        if self.snaps and ts < self.snaps[-1][0]:
            self.snaps.clear()                                  # the market clock restarted: a new session
        self.snaps.append((float(ts), int(bids[0]["price"]) / 100, int(asks[0]["price"]) / 100,
                           int(bids[0]["size"]) * 0.001, int(asks[0]["size"]) * 0.001))
        return True

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        """Top of book at HISTORY consecutive 200 ms ticks ending at the latest snapshot's tick, or None while warming up."""
        if not self.snaps:
            return None
        data = np.array(self.snaps)
        end = np.floor(data[-1, 0] / TICK_MS) * TICK_MS
        grid = end - TICK_MS * np.arange(HISTORY - 1, -1, -1)
        pos = np.searchsorted(data[:, 0], grid, side="right") - 1
        if pos[0] < 0:
            return None
        picked = data[pos]
        return picked[:, 1], picked[:, 2], picked[:, 3], picked[:, 4]


class TransformerScorer:
    def __init__(self, path: Path = ARTIFACT) -> None:
        if not path.exists():
            raise SystemExit(f"Missing {path}. Run python -m model.train_transformer first.")
        blob = torch.load(path, map_location="cpu", weights_only=False)
        self.net = TickNet(len(blob["feature_names"]))
        self.net.load_state_dict(blob["state_dict"])
        self.net.eval()
        self.mean = np.asarray(blob["mean"], dtype=np.float32)
        self.std = np.asarray(blob["std"], dtype=np.float32)
        self.scale = float(blob["scale"])                      # |predicted bps| that counts as full confidence
        self.entry_threshold = float(blob["entry_threshold"])  # confidence above which the validation day's top 10 % of ticks fall
        self.meta = blob

    def predict(self, stream: TickStream) -> float | None:
        arrays = stream.arrays()
        if arrays is None:
            return None
        window = grid_features(*arrays)[-WIN:]
        x = np.clip((window - self.mean) / self.std, -6, 6).astype(np.float32)
        with torch.no_grad():
            return float(self.net(torch.from_numpy(x).unsqueeze(0)).item())

    def score(self, stream: TickStream) -> float:
        """Probability-like score in [0, 1] for decide_session: 0.5 plus half the clipped prediction over `scale`."""
        pred = self.predict(stream)
        if pred is None:
            return 0.5
        return 0.5 + 0.5 * float(np.clip(pred / self.scale, -1.0, 1.0))
