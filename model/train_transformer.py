"""Train the tick transformer and save model/artifacts/transformer_signal.pt.

    python -m model.train_transformer

Data (200 ms top of book, recorded):
  train       9 Binance days (hft/data/ext/grid200_*.npz, via hft/download_extra.py + hft/research/11_bookticker_grid.py), 2023-06 .. 2024-03
  validation  2023-12-15  (sets the confidence scale and the entry threshold; never trained on)
  test        2026-10-01  the day the server replays, so the website's session is out of sample for this model
Label: mid-price change over the next 25 ticks (5 s) in bps, clipped to +-15. Loss: Huber. The device is the Apple GPU when available.
"""

from __future__ import annotations

import glob
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn

from model.transformer_signal import ARTIFACT, FEATURE_NAMES, HISTORY, WIN, TickNet, grid_features

ROOT = Path(__file__).resolve().parent.parent
HORIZON = 25
VALIDATION_DAY = "2023-12-15"
TEST_DAY = "2026-10-01"
EPOCHS, STRIDE, BATCH = 3, 8, 512


def load_days() -> dict[str, tuple[np.ndarray, ...]]:
    days = {}
    for path in sorted(glob.glob(str(ROOT / "hft" / "data" / "ext" / "grid200_*.npz"))):
        z = np.load(path)
        days[Path(path).stem.split("grid200_")[1]] = (z["bid"], z["ask"], z["bq"], z["aq"])
    g = np.load(ROOT / "hft" / "data" / "out" / "BTCUSDT_20261001_grid50ms.npz")
    days[TEST_DAY] = (g["bid"][::4] / 100.0, g["ask"][::4] / 100.0, g["bidsz"][::4] * 0.001, g["asksz"][::4] * 0.001)
    return days


def prepare(arrays) -> tuple[np.ndarray, np.ndarray]:
    bid, ask, bq, aq = arrays
    mid = (bid + ask) / 2
    y = np.full(len(mid), np.nan, dtype=np.float32)
    y[:len(mid) - HORIZON] = (mid[HORIZON:] / mid[:len(mid) - HORIZON] - 1) * 1e4
    return grid_features(bid, ask, bq, aq), y


def windows(x: torch.Tensor, ends: torch.Tensor) -> torch.Tensor:
    offsets = torch.arange(-WIN + 1, 1, device=x.device)
    return x[ends[:, None] + offsets[None, :]]


@torch.no_grad()
def predict(net: nn.Module, x: np.ndarray, mean, std, device) -> tuple[np.ndarray, np.ndarray]:
    xt = torch.tensor(np.clip((x - mean) / std, -6, 6).astype(np.float32), device=device)
    ends = np.arange(WIN, len(x) - HORIZON)
    out = []
    net.eval()
    for i in range(0, len(ends), 2048):
        out.append(net(windows(xt, torch.tensor(ends[i:i + 2048], device=device))).cpu().numpy())
    return np.concatenate(out), ends


def report(pred: np.ndarray, y: np.ndarray) -> dict:
    ok = ~np.isnan(y)
    p, t = pred[ok], y[ok]
    res = {"ic": float(np.corrcoef(p, t)[0, 1]), "mean_abs_move_bps": float(np.abs(t).mean())}
    c = np.abs(p)
    for q in (0.9, 0.99, 0.999):
        top = c >= np.quantile(c, q)
        res[f"gross_bps_top_{round((1 - q) * 100, 1)}pct"] = float((np.sign(p[top]) * t[top]).mean())
    return res


def main() -> None:
    device = torch.device("mps") if torch.backends.mps.is_available() else torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    torch.manual_seed(0)
    np.random.seed(0)
    days = load_days()
    data = {d: prepare(a) for d, a in days.items()}
    train_days = [d for d in data if d not in (VALIDATION_DAY, TEST_DAY)]
    print(f"device {device} | train {len(train_days)} days, validation {VALIDATION_DAY}, test {TEST_DAY}", flush=True)

    X = np.concatenate([data[d][0] for d in train_days])
    y = np.concatenate([data[d][1] for d in train_days])
    ends, off = [], 0
    for d in train_days:
        n = len(data[d][1])
        ends.append(np.arange(WIN, n - HORIZON, STRIDE) + off)
        off += n
    ends = np.concatenate(ends)
    mean, std = X.mean(0), X.std(0) + 1e-6
    xt = torch.tensor(np.clip((X - mean) / std, -6, 6).astype(np.float32), device=device)
    yt = torch.tensor(np.clip(np.nan_to_num(y), -15, 15), device=device)
    ends_t = torch.tensor(ends, device=device)

    net = TickNet().to(device)
    opt = torch.optim.AdamW(net.parameters(), 5e-4, weight_decay=1e-2)
    loss_fn = nn.HuberLoss(delta=3.0)
    t0 = time.time()
    for epoch in range(EPOCHS):
        net.train()
        order = torch.randperm(len(ends_t), device=device)
        total = 0.0
        for i in range(0, len(ends_t) - BATCH, BATCH):
            b = ends_t[order[i:i + BATCH]]
            opt.zero_grad()
            loss = loss_fn(net(windows(xt, b)), yt[b])
            loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            opt.step()
            total += loss.item()
        print(f"epoch {epoch + 1}/{EPOCHS} loss {total / (len(ends_t) // BATCH):.4f} ({time.time() - t0:.0f}s)", flush=True)

    val_pred, val_ends = predict(net, data[VALIDATION_DAY][0], mean, std, device)
    test_pred, test_ends = predict(net, data[TEST_DAY][0], mean, std, device)
    scale = float(np.quantile(np.abs(val_pred), 0.99))
    entry = float(np.quantile(np.minimum(np.abs(val_pred) / scale, 1.0), 0.90))
    val = report(val_pred, data[VALIDATION_DAY][1][val_ends])
    test = report(test_pred, data[TEST_DAY][1][test_ends])
    print("validation", {k: round(v, 3) for k, v in val.items()})
    print("test (replay day)", {k: round(v, 3) for k, v in test.items()})
    print(f"scale {scale:.3f} bps (99th percentile |prediction|), entry threshold {entry:.3f}")

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "state_dict": {k: v.cpu() for k, v in net.state_dict().items()}, "mean": mean.tolist(), "std": std.tolist(),
        "feature_names": FEATURE_NAMES, "window": WIN, "history": HISTORY, "horizon_ticks": HORIZON,
        "scale": scale, "entry_threshold": entry, "validation": val, "test_replay_day": test,
        "train_days": train_days, "validation_day": VALIDATION_DAY, "test_day": TEST_DAY,
    }, ARTIFACT)
    print("saved", ARTIFACT)


if __name__ == "__main__":
    main()
