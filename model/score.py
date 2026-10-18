"""Load the saved network and turn live observations into an expected return."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch

from model.features import WARMUP, bar_from_observation, features_from_bars
from model.net import SignalNet

ARTIFACTS = Path(__file__).resolve().parent / "artifacts" / "signal.pt"


class Scorer:
    def __init__(self, path: Path = ARTIFACTS) -> None:
        if not path.exists():
            raise SystemExit(f"Missing {path}. Run python -m model.train first.")
        blob = torch.load(path, map_location="cpu", weights_only=False)
        self.net = SignalNet(blob["n_feat"], blob["n_seq"], blob["seq_len"], blob["n_horizons"])
        self.net.load_state_dict(blob["state_dict"])
        self.net.eval()
        self.mean = np.asarray(blob["mean"], dtype=np.float32)
        self.std = np.asarray(blob["std"], dtype=np.float32)
        self.horizon_index = int(blob["horizon_index"])
        self.horizon_bars = int(blob["horizon_bars"])
        self.enter_bps = float(blob["enter_bps"])
        self.exit_bps = float(blob["exit_bps"])
        self.bar_ms = int(blob["bar_ms"])
        self.bars: list[tuple] = []
        self.bucket: int | None = None
        self.ready = False

    def reset(self) -> None:
        self.bars.clear()
        self.bucket = None
        self.ready = False

    def seed(self, bars: list[dict]) -> int:
        """Load server history from GET /v1/bars so the lookback window is full at once.

        Bars before the session have negative ts. Returns how many bars the window holds.
        """
        for item in bars:
            sign = {"buy": 1, "sell": -1}.get(item.get("tradeSide"), 0)
            bar = (
                int(item["bid"]),
                int(item["ask"]),
                max(0, int(item["bidSize"])),
                max(0, int(item["askSize"])),
                int(item["mid"]),
                sign,
                max(0, int(item.get("tradeSize") or 0)) if sign else 0,
                1,
            )
            self._push(bar, int(item["ts"]) // self.bar_ms)
        return len(self.bars)

    def update(self, observation: dict) -> float:
        bar = bar_from_observation(observation)
        if bar is None:
            return 0.0
        self._push(bar, int(observation.get("ts") or 0) // self.bar_ms)
        vector = features_from_bars(self.bars)
        if vector is None:
            return 0.0
        self.ready = True
        scaled = (np.asarray(vector, dtype=np.float32) - self.mean) / self.std
        with torch.no_grad():
            pred = self.net(torch.from_numpy(scaled).unsqueeze(0))
        return float(pred[0, self.horizon_index])

    def _push(self, bar: tuple, bucket: int) -> None:
        if self.bucket is not None and bucket < self.bucket:
            self.reset()
        if self.bucket is None:
            self.bars = [bar]
            self.bucket = bucket
        elif bucket == self.bucket:
            self.bars[-1] = bar
        else:
            gap = min(bucket - self.bucket, WARMUP)
            last = self.bars[-1]
            quiet = (last[0], last[1], last[2], last[3], last[4], 0, 0, last[7])
            for _ in range(gap - 1):
                self.bars.append(quiet)
            self.bars.append(bar)
            self.bucket = bucket
            if len(self.bars) > WARMUP:
                self.bars = self.bars[-WARMUP:]
