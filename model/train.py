"""Fit the signal net on the RTX 4080, then score a held-out BTC slice.

Rows come from POST /v1/reset and POST /v1/step when that server is idle.
If it is not running, the same feature fields are built from the local
BTCUSDT book ticker. That file is the recording the model API replays.
The GPU only fits the network. It does not simulate fills.
"""

from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

import torch
from torch import nn

from model.client import ApiError, ModelClient
from model.features import FEATURE_NAMES, features_from_observation
from model.net import SignalNet
from model.splits import split_bounds

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"
TAPE = ROOT.parent / "hft" / "data" / "raw" / "BTCUSDT_20261001_book_ticker.csv.gz"
RECORD = ROOT.parent / "hft" / "data" / "out" / "BTCUSDT_20261001_record.npz"
HORIZON = 8
TAKER_FEE = 0.0005
STRIDE = 400
MAX_ROWS = 80_000


def training_device() -> torch.device:
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print(f"training on {torch.cuda.get_device_name(0)}")
        return device
    print("CUDA is missing, training on CPU")
    return torch.device("cpu")


def _row_from_ticker(row: dict, mids: list[int]) -> tuple[list[float], int] | None:
    bid = int(round(float(row["bid_price"]) * 100))
    ask = int(round(float(row["ask_price"]) * 100))
    if ask <= bid or bid <= 0:
        return None
    mid = (bid + ask) // 2
    mids.append(mid)
    observation = {
        "mid": mid,
        "bids": [{"price": bid, "size": max(1, int(float(row["bid_amount"]) * 1000))}],
        "asks": [{"price": ask, "size": max(1, int(float(row["ask_amount"]) * 1000))}],
        "mids": mids[-32:],
        "priceSource": "tape",
    }
    features = features_from_observation(observation)
    if features is None:
        return None
    return features, mid


def collect_from_tape(path: Path = TAPE) -> list[tuple[list[float], int]]:
    rows: list[tuple[list[float], int]] = []
    mids: list[int] = []
    seen = 0
    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            seen += 1
            if seen % STRIDE != 0:
                continue
            parsed = _row_from_ticker(row, mids)
            if parsed is None:
                continue
            rows.append(parsed)
            if len(rows) >= MAX_ROWS:
                break
    print(f"tape rows {len(rows)} from {path.name}, stride {STRIDE}")
    return rows


def collect_from_api(client: ModelClient, steps: int = 2000) -> list[tuple[list[float], int]]:
    try:
        opened = client.reset({"instrument": "BTC-USDT", "episodeSteps": steps})
    except ApiError as error:
        if error.code == "session_open":
            raise SystemExit(
                "Reset was refused because a live session is open. Train when the server is idle."
            ) from error
        raise
    observation = opened["observation"]
    rows: list[tuple[list[float], int]] = []
    for _ in range(steps):
        features = features_from_observation(observation)
        if features is not None:
            rows.append((features, int(observation["mid"])))
        try:
            stepped = client.step({"dtMs": 100})
        except ApiError as error:
            if error.code == "step_not_for_live_mid":
                raise SystemExit(
                    "step is refused during a live SOL session. Train while the server is idle."
                ) from error
            raise
        observation = stepped["observation"]
        if stepped.get("done"):
            break
    print(f"api rows {len(rows)}")
    return rows


def _standardize(rows: list[list[float]], train_end: int) -> tuple[list[float], list[float]]:
    width = len(rows[0])
    mean = []
    std = []
    for index in range(width):
        column = [row[index] for row in rows[:train_end]]
        center = sum(column) / len(column)
        variance = sum((value - center) ** 2 for value in column) / len(column)
        mean.append(center)
        std.append(variance ** 0.5 or 1.0)
    return mean, std


def _tensor(rows: list[list[float]], mean: list[float], std: list[float], device: torch.device) -> torch.Tensor:
    scaled = [
        [(value - center) / scale for value, center, scale in zip(row, mean, std)]
        for row in rows
    ]
    return torch.tensor(scaled, dtype=torch.float32, device=device)


def fit(
    features: list[list[float]],
    labels: list[float],
    mean: list[float],
    std: list[float],
    device: torch.device,
    epochs: int = 8,
) -> SignalNet:
    network = SignalNet().to(device)
    inputs = _tensor(features, mean, std, device)
    targets = torch.tensor(labels, dtype=torch.float32, device=device)
    optimizer = torch.optim.Adam(network.parameters(), lr=1e-3)
    loss_fn = nn.BCEWithLogitsLoss()
    network.train()
    count = inputs.shape[0]
    for epoch in range(epochs):
        order = torch.randperm(count, device=device)
        total = 0.0
        for start in range(0, count, 1024):
            batch = order[start : start + 1024]
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(network(inputs[batch]), targets[batch])
            loss.backward()
            optimizer.step()
            total += float(loss.detach()) * len(batch)
        print(f"epoch {epoch + 1} loss {total / count:.4f}")
    network.eval()
    return network


def probabilities(
    network: SignalNet,
    features: list[list[float]],
    mean: list[float],
    std: list[float],
    device: torch.device,
) -> list[float]:
    if not features:
        return []
    with torch.no_grad():
        logits = network(_tensor(features, mean, std, device))
        return torch.sigmoid(logits).detach().cpu().tolist()


def signal_pnl(
    scores: list[float],
    mids: list[int],
    future_mids: list[int],
    imbalances: list[float],
    spreads: list[float],
    threshold: float,
) -> tuple[float, int]:
    total = 0.0
    trades = 0
    for score, mid, future, imbalance, spread in zip(scores, mids, future_mids, imbalances, spreads):
        if mid <= 0:
            continue
        confidence = abs(score - 0.5) * 2
        direction = 1.0 if score >= 0.5 else -1.0
        agrees = (imbalance > 0) == (direction > 0)
        if confidence < threshold or abs(imbalance) <= TAKER_FEE or abs(imbalance) <= spread or not agrees:
            continue
        total += direction * ((future - mid) / mid) - TAKER_FEE
        trades += 1
    return total, trades


def choose_threshold(
    scores: list[float],
    mids: list[int],
    future_mids: list[int],
    imbalances: list[float],
    spreads: list[float],
) -> float:
    best_threshold = 0.99
    best_pnl = 0.0
    for step in range(1, 20):
        threshold = step / 20
        pnl, _trades = signal_pnl(scores, mids, future_mids, imbalances, spreads, threshold)
        if pnl > best_pnl:
            best_pnl = pnl
            best_threshold = threshold
    return best_threshold


def join_touch_note() -> str:
    if not RECORD.exists():
        return "Join-the-touch baseline file is absent. Run hft/run_backtest.py to produce it."
    try:
        import numpy as np

        records = np.load(RECORD)
        last = records[records.files[0]][-1]
        equity = float(last["balance"] + last["position"] * last["price"] - last["fee"])
    except Exception as error:
        return f"Could not read the join-the-touch record: {error}"
    return (
        f"Separate HftBacktest join-the-touch equity after fees on the same day: {equity:.6f}. "
        "That number is not a fill from this bot or from the model API."
    )


def evaluate(rows: list[tuple[list[float], int]], source: str) -> dict:
    device = training_device()
    bounds = split_bounds(len(rows), HORIZON)
    features = [row[0] for row in rows]
    mids = [row[1] for row in rows]
    train_end = bounds["train_end"]
    horizon = bounds["horizon"]
    train_x = features[:train_end]
    train_y = [1.0 if mids[index + horizon] > mids[index] else 0.0 for index in range(train_end)]
    mean, std = _standardize(train_x, train_end)
    network = fit(train_x, train_y, mean, std, device)

    def slice_scores(start: int, end: int) -> tuple[list[float], list[int], list[int], list[float], list[float]]:
        window = features[start:end]
        scores = probabilities(network, window, mean, std, device)
        now = mids[start:end]
        future = mids[start + horizon : end + horizon]
        imbalances = [row[0] for row in window]
        spreads = [row[2] for row in window]
        return scores, now, future, imbalances, spreads

    calibration = slice_scores(bounds["calibration_start"], bounds["calibration_end"])
    threshold = choose_threshold(*calibration)
    held = slice_scores(bounds["test_start"], bounds["test_end"])
    held_pnl, trades = signal_pnl(*held, threshold)
    threshold_note = (
        "A calibration threshold beat doing nothing after the 5 bps taker fee."
        if threshold < 0.99
        else "No taker threshold beat a flat book after the 5 bps fee, so the saved threshold refuses IOC crosses. Passive post-only quotes still run."
    )
    network = network.to("cpu")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": network.state_dict(),
            "mean": mean,
            "std": std,
            "threshold": threshold,
            "feature_names": FEATURE_NAMES,
        },
        ARTIFACTS / "signal.pt",
    )
    report = {
        "source": source,
        "rows": len(rows),
        "device": torch.cuda.get_device_name(0) if device.type == "cuda" else "cpu",
        "splits": bounds,
        "threshold": threshold,
        "threshold_note": threshold_note,
        "held_out_return_after_taker_fee": held_pnl,
        "held_out_trades": trades,
        "flat_return": 0.0,
        "window": "BTCUSDT book ticker 2026-10-01, strided. One recorded day, not a live trading result.",
        "baseline": join_touch_note(),
        "note": (
            "Held-out return sums signed mid moves after a 5 bps taker fee when the signal trades. "
            "Flat does nothing and returns 0. Fees inside a model-API reward must not be subtracted again. "
            "This is not evidence of a profitable strategy."
        ),
    }
    (ARTIFACTS / "eval_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report


def load_rows(base_url: str) -> tuple[list[tuple[list[float], int]], str]:
    client = ModelClient(base_url, timeout=3.0)
    try:
        client.health()
        account = client.account()
    except Exception as error:
        print(f"Model API is not available ({error}). Using the local BTCUSDT book ticker.")
        return collect_from_tape(), "local book ticker, because the model API was not reachable"
    if account.get("stakeCents") is not None:
        print("A live session is open, so reset was not called. Using the local BTCUSDT book ticker.")
        return collect_from_tape(), "local book ticker, because a live session was open"
    return collect_from_api(client), "model API reset and step on BTC-USDT"


def main() -> None:
    rows, source = load_rows("http://127.0.0.1:3000")
    if len(rows) <= HORIZON * 6:
        raise SystemExit("Not enough rows to train.")
    evaluate(rows, source)


if __name__ == "__main__":
    main()
