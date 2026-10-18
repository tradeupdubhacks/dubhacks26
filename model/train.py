"""Fit the return net on the GPU, then choose a taker rule that clears the fee.

The 2026-10-01 book ticker is the day the server replays. docs/model-issues.md
holds out the slice that starts at 16:22 UTC. Weights are fit on September
aggTrades plus the morning of that day. The afternoon picks the entry rule.
The evening is scored once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from torch import nn

from model.bars import SEPTEMBER, ensure_agg_days, load_agg_day, load_tardis_day
from model.features import (
    BAR_MS,
    HORIZONS,
    INPUT_DIM,
    LOOKBACK,
    N_FEAT,
    N_SEQ,
    SEQ_LEN,
    FEATURE_NAMES,
    series_features,
)
from model.net import SignalNet
from model.policy import desired_lots, notional_cents

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"
# Book-ticker timestamp where the held-out replay in docs/model-issues.md begins.
HOLDOUT_US = 1_790_871_729_793_000
ENTERS = (1.0, 2.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 36.0)
MIN_ENTRIES = 10


def training_device() -> torch.device:
    if torch.cuda.is_available():
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        name = torch.cuda.get_device_name(0)
        print(f"CUDA is working. Training on {name}.")
        return torch.device("cuda")
    print("CUDA is not available. Training on CPU.")
    return torch.device("cpu")


@dataclass
class Day:
    date: str
    has_book: bool
    ts: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    mid: np.ndarray
    feat: np.ndarray
    channels: np.ndarray
    labels: np.ndarray


def _labels(mid: np.ndarray) -> np.ndarray:
    base = mid.astype(np.float64)
    out = np.full((mid.shape[0], len(HORIZONS)), np.nan, dtype=np.float32)
    for index, horizon in enumerate(HORIZONS):
        future = np.full(mid.shape[0], np.nan)
        future[:-horizon] = base[horizon:]
        valid = (base > 0) & (future > 0)
        column = np.zeros(mid.shape[0], dtype=np.float32)
        column[valid] = ((future[valid] - base[valid]) / base[valid] * 1e4).astype(np.float32)
        out[:, index] = np.where(valid, column, np.nan)
    return out


def _day_from_bars(date: str, bars: dict[str, np.ndarray], has_book: bool) -> Day:
    start = int(bars["day_start_ms"][0])
    count = bars["mid"].shape[0]
    features, channels = series_features(
        bars["bid"],
        bars["ask"],
        bars["bid_sz"],
        bars["ask_sz"],
        bars["mid"],
        bars["trade_sign"],
        bars["trade_sz"],
        bars["book"],
    )
    return Day(
        date=date,
        has_book=has_book,
        ts=start + np.arange(count, dtype=np.int64) * BAR_MS,
        bid=bars["bid"],
        ask=bars["ask"],
        mid=bars["mid"],
        feat=features,
        channels=channels,
        labels=_labels(bars["mid"]),
    )


def load_days() -> list[Day]:
    ensure_agg_days()
    days = [_day_from_bars("2026-10-01", load_tardis_day(), True)]
    for date in SEPTEMBER:
        bars = load_agg_day(date)
        if bars is None:
            continue
        days.append(_day_from_bars(date, bars, False))
    print(f"loaded {len(days)} days")
    return days


def _pairs(day_index: int, mask: np.ndarray) -> np.ndarray:
    times = np.flatnonzero(mask).astype(np.int32)
    out = np.empty((times.shape[0], 2), dtype=np.int32)
    out[:, 0] = day_index
    out[:, 1] = times
    return out


def split_indices(days: list[Day]) -> dict[str, np.ndarray]:
    october = next(day for day in days if day.date == "2026-10-01")
    october_index = days.index(october)
    holdout_ms = HOLDOUT_US // 1000
    test_start = holdout_ms - (holdout_ms - int(october.ts[0])) % BAR_MS
    calib_start = int(october.ts[0]) + 14 * 3600 * 1000
    horizon_ms = HORIZONS[-1] * BAR_MS
    feature_ok = (np.arange(october.mid.shape[0]) >= LOOKBACK) & (october.mid > 0)
    label_ok = np.isfinite(october.labels).all(axis=1)
    train_book = _pairs(
        october_index,
        feature_ok & label_ok & (october.ts + horizon_ms < calib_start),
    )
    blocks = [train_book]
    for index, day in enumerate(days):
        if day.date == "2026-10-01":
            continue
        ok = (np.arange(day.mid.shape[0]) >= LOOKBACK) & (day.mid > 0) & np.isfinite(day.labels).all(axis=1)
        blocks.append(_pairs(index, ok))
    flow = np.concatenate([block for block in blocks[1:]], axis=0) if len(blocks) > 1 else np.empty((0, 2), np.int32)
    return {
        "train_book": train_book,
        "train_flow": flow,
        "calib_start": np.asarray([calib_start]),
        "test_start": np.asarray([test_start]),
        "october_index": np.asarray([october_index]),
    }


def batch_matrix(days: list[Day], pairs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    count = pairs.shape[0]
    features = np.empty((count, INPUT_DIM), dtype=np.float32)
    targets = np.empty((count, len(HORIZONS)), dtype=np.float32)
    for day_index in np.unique(pairs[:, 0]):
        selected = pairs[:, 0] == day_index
        times = pairs[selected, 1]
        day = days[int(day_index)]
        offsets = times[:, None] + np.arange(-SEQ_LEN + 1, 1, dtype=np.int32)[None, :]
        seq = np.transpose(day.channels[:, offsets], (1, 0, 2)).reshape(times.shape[0], -1)
        features[selected] = np.concatenate([day.feat[times], seq], axis=1)
        targets[selected] = day.labels[times]
    return features, targets


def _demean_labels(days: list[Day], book: np.ndarray, flow: np.ndarray) -> np.ndarray:
    """Remove the training-set drift so a rally does not become a permanent long bias."""
    total = np.zeros(len(HORIZONS), dtype=np.float64)
    count = 0
    for pairs in (book, flow):
        if pairs.size == 0:
            continue
        for start in range(0, pairs.shape[0], 100_000):
            chunk = pairs[start : start + 100_000]
            for day_index in np.unique(chunk[:, 0]):
                times = chunk[chunk[:, 0] == day_index, 1]
                values = days[int(day_index)].labels[times]
                valid = np.isfinite(values).all(axis=1)
                total += np.nansum(values[valid], axis=0)
                count += int(valid.sum())
    center = total / max(count, 1)
    for day in days:
        day.labels = day.labels - center.astype(np.float32)
    return center.astype(np.float32)


def _mean_std(days: list[Day], book: np.ndarray, flow: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(7)
    parts = [book]
    if flow.shape[0] > 0:
        take = min(flow.shape[0], 300_000)
        parts.append(flow[rng.choice(flow.shape[0], size=take, replace=False)])
    sample = np.concatenate(parts, axis=0)
    if sample.shape[0] > 400_000:
        sample = sample[rng.choice(sample.shape[0], size=400_000, replace=False)]
    total = np.zeros(INPUT_DIM, dtype=np.float64)
    squares = np.zeros(INPUT_DIM, dtype=np.float64)
    count = 0
    for start in range(0, sample.shape[0], 32_768):
        features, _targets = batch_matrix(days, sample[start : start + 32_768])
        values = features.astype(np.float64)
        total += values.sum(axis=0)
        squares += np.square(values).sum(axis=0)
        count += features.shape[0]
    mean = total / count
    variance = np.maximum(squares / count - np.square(mean), 0.0)
    std = np.sqrt(variance)
    std[std < 1e-6] = 1.0
    return mean.astype(np.float32), std.astype(np.float32)


def _predict(
    network: SignalNet,
    days: list[Day],
    pairs: np.ndarray,
    mean: torch.Tensor,
    std: torch.Tensor,
    device: torch.device,
) -> np.ndarray:
    network.eval()
    out = np.zeros((pairs.shape[0], len(HORIZONS)), dtype=np.float32)
    amp = device.type == "cuda"
    with torch.no_grad():
        for start in range(0, pairs.shape[0], 8192):
            chunk = pairs[start : start + 8192]
            features, _targets = batch_matrix(days, chunk)
            inputs = torch.tensor(features, device=device)
            inputs = (inputs - mean) / std
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=amp):
                pred = network(inputs).float()
            out[start : start + chunk.shape[0]] = pred.cpu().numpy()
    return out


def _span_predictions(
    network: SignalNet,
    days: list[Day],
    day_index: int,
    start: int,
    stop: int,
    mean: torch.Tensor,
    std: torch.Tensor,
    device: torch.device,
) -> np.ndarray:
    times = np.arange(start, stop, dtype=np.int32)
    valid = times >= LOOKBACK
    pairs = np.empty((int(valid.sum()), 2), dtype=np.int32)
    pairs[:, 0] = day_index
    pairs[:, 1] = times[valid]
    predicted = _predict(network, days, pairs, mean, std, device) if pairs.shape[0] else np.zeros((0, len(HORIZONS)))
    full = np.zeros((stop - start, len(HORIZONS)), dtype=np.float32)
    full[valid] = predicted
    return full


def mark(cash: int, position: int, mid: int) -> int:
    if position >= 0:
        return cash + notional_cents(mid, position)
    return cash - notional_cents(mid, -position)


def simulate(
    expected: np.ndarray,
    bid: np.ndarray,
    ask: np.ndarray,
    mid: np.ndarray,
    enter: float,
    exit_bps: float,
    horizon: int,
    capacity: int = 10,
) -> dict[str, int]:
    position = 0
    cash = 0
    fees = 0
    fills = 0
    entries = 0
    opened = -10**9
    peak = 0
    trough = 0
    last = mid.shape[0] - 1
    for index in range(last):
        held = 0 if position == 0 else index - opened
        signal = float(expected[index])
        if int(mid[index]) <= 0 or int(ask[index]) <= int(bid[index]):
            signal = 0.0
        desired = desired_lots(signal, position, held, enter, exit_bps, horizon, capacity)
        if desired != position:
            price_index = index + 1
            delta = desired - position
            if delta > 0:
                price = int(ask[price_index])
                notion = notional_cents(price, delta)
                fee = notion * 5 // 10_000
                cash -= notion + fee
            else:
                price = int(bid[price_index])
                notion = notional_cents(price, -delta)
                fee = notion * 5 // 10_000
                cash += notion - fee
            fees += fee
            fills += 1
            if position == 0 or (position > 0) != (desired > 0):
                entries += 1
                opened = index
            if desired == 0:
                opened = -10**9
            position = desired
        equity = mark(cash, position, int(mid[min(index + 1, last)]))
        peak = max(peak, equity)
        trough = min(trough, equity)
    if position != 0:
        if position > 0:
            notion = notional_cents(int(bid[last]), position)
            fee = notion * 5 // 10_000
            cash += notion - fee
        else:
            notion = notional_cents(int(ask[last]), -position)
            fee = notion * 5 // 10_000
            cash -= notion + fee
        fees += fee
        fills += 1
        position = 0
    equity = cash
    return {
        "pnl_cents": equity,
        "fees_cents": fees,
        "fills": fills,
        "entries": entries,
        "peak_cents": max(peak, equity),
        "trough_cents": min(trough, equity),
    }


def choose_rule(expected: np.ndarray, bid: np.ndarray, ask: np.ndarray, mid: np.ndarray) -> dict:
    best: dict | None = None
    rejected = {"fee_floor": 0, "few_entries": 0, "thin_edge": 0, "kept": 0}
    for horizon_index, horizon in enumerate(HORIZONS):
        column = expected[:, horizon_index]
        for enter in ENTERS:
            for exit_bps in (0.0, enter / 2):
                result = simulate(column, bid, ask, mid, enter, exit_bps, horizon)
                if result["entries"] < MIN_ENTRIES:
                    rejected["few_entries"] += 1
                    continue
                rejected["kept"] += 1
                candidate = {
                    "horizon_index": horizon_index,
                    "horizon_bars": horizon,
                    "enter_bps": enter,
                    "exit_bps": exit_bps,
                    **result,
                }
                if best is None or candidate["pnl_cents"] > best["pnl_cents"]:
                    best = candidate
    if best is None:
        # #region agent log
        try:
            import json, time
            with open(r"C:\Akash\Coding\dubhacks26\debug-a0c237.log", "a", encoding="utf-8") as _f:
                _f.write(json.dumps({"sessionId": "a0c237", "hypothesisId": "B", "location": "train.py:choose_rule", "message": "flat rule", "data": rejected, "timestamp": int(time.time() * 1000), "runId": "post-fix"}) + "\n")
        except Exception:
            pass
        # #endregion
        return {
            "horizon_index": 2,
            "horizon_bars": HORIZONS[2],
            "enter_bps": 1_000_000.0,
            "exit_bps": 0.0,
            "pnl_cents": 0,
            "fees_cents": 0,
            "fills": 0,
            "entries": 0,
            "peak_cents": 0,
            "trough_cents": 0,
            "flat": True,
        }
    best["flat"] = False
    # #region agent log
    try:
        import json, time
        with open(r"C:\Akash\Coding\dubhacks26\debug-a0c237.log", "a", encoding="utf-8") as _f:
            _f.write(json.dumps({"sessionId": "a0c237", "hypothesisId": "B", "location": "train.py:choose_rule", "message": "trading rule", "data": {"enterBps": best["enter_bps"], "exitBps": best["exit_bps"], "horizonBars": best["horizon_bars"], "entries": best["entries"], "pnlCents": best["pnl_cents"], "rejected": rejected}, "timestamp": int(time.time() * 1000), "runId": "post-fix"}) + "\n")
    except Exception:
        pass
    # #endregion
    return best


def race(
    expected: np.ndarray,
    bid: np.ndarray,
    ask: np.ndarray,
    mid: np.ndarray,
    rule: dict,
    stop_cents: int = 500,
) -> dict[str, int | str | None]:
    """One $5 deposit. Floor is a $5 loss and ceiling is a $5 gain, on  the pool."""
    position = 0
    cash = 0
    opened = -10**9
    outcome = "unfinished"
    end = 0
    last = mid.shape[0] - 1
    column = expected[:, rule["horizon_index"]]
    for index in range(last):
        equity = mark(cash, position, int(mid[index]))
        if equity <= -stop_cents:
            outcome = "floor"
            end = index
            break
        if equity >= stop_cents:
            outcome = "ceiling"
            end = index
            break
        room = min(stop_cents + equity, stop_cents - equity)
        capacity = 0
        for size in range(1, 11):
            notion = notional_cents(int(mid[index]), size)
            budget = notion * 50 // 10_000 + notion * 5 // 10_000 + 1
            if budget < room:
                capacity = size
            else:
                break
        held = 0 if position == 0 else index - opened
        signal = float(column[index]) if capacity > 0 or position != 0 else 0.0
        desired = desired_lots(
            signal,
            position,
            held,
            rule["enter_bps"],
            rule["exit_bps"],
            rule["horizon_bars"],
            max(capacity, 0),
        )
        if capacity < 1:
            desired = 0
        if desired == position or int(ask[index + 1]) <= int(bid[index + 1]):
            end = index
            continue
        delta = desired - position
        if delta > 0:
            notion = notional_cents(int(ask[index + 1]), delta)
            fee = notion * 5 // 10_000
            cash -= notion + fee
        else:
            notion = notional_cents(int(bid[index + 1]), -delta)
            fee = notion * 5 // 10_000
            cash += notion - fee
        if position == 0 or (position > 0) != (desired > 0):
            opened = index
        if desired == 0:
            opened = -10**9
        position = desired
        end = index
    else:
        if position > 0:
            notion = notional_cents(int(bid[last]), position)
            cash += notion - notion * 5 // 10_000
        elif position < 0:
            notion = notional_cents(int(ask[last]), -position)
            cash -= notion + notion * 5 // 10_000
        position = 0
        equity = cash
        if equity >= stop_cents:
            outcome = "ceiling"
        elif equity <= -stop_cents:
            outcome = "floor"
        end = last
    return {"outcome": outcome, "bars": end, "seconds": int(end * BAR_MS / 1000)}


def fit(
    days: list[Day],
    book: np.ndarray,
    flow: np.ndarray,
    mean: np.ndarray,
    std: np.ndarray,
    calib_expected_index: tuple[int, int, int],
    device: torch.device,
) -> tuple[SignalNet, dict]:
    network = SignalNet().to(device)
    optimizer = torch.optim.AdamW(network.parameters(), lr=3e-4, weight_decay=1e-4)
    steps = 30
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=steps)
    loss_fn = nn.HuberLoss(delta=5.0, reduction="none")
    mean_t = torch.tensor(mean, device=device)
    std_t = torch.tensor(std, device=device)
    amp = device.type == "cuda"
    rng = np.random.default_rng(7)
    torch.manual_seed(7)
    best_state = {key: value.detach().cpu().clone() for key, value in network.state_dict().items()}
    best_rule: dict = {"pnl_cents": -10**12, "flat": True}
    best_loss = 1e18
    stale = 0
    day_index, calib_lo, calib_hi = calib_expected_index
    october = days[day_index]
    for epoch in range(steps):
        network.train()
        if flow.shape[0] > 0:
            take = min(flow.shape[0], max(book.shape[0] * 4, 1))
            chosen = flow[rng.choice(flow.shape[0], size=take, replace=False)]
            pairs = np.concatenate([book, chosen], axis=0)
        else:
            pairs = book
        order = rng.permutation(pairs.shape[0])
        total = 0.0
        seen = 0
        for start in range(0, pairs.shape[0], 16384):
            batch = pairs[order[start : start + 16384]]
            features, targets = batch_matrix(days, batch)
            inputs = torch.tensor(features, device=device)
            labels = torch.tensor(targets, device=device)
            inputs = (inputs - mean_t) / std_t
            optimizer.zero_grad(set_to_none=True)
            with torch.autocast(device_type=device.type, dtype=torch.bfloat16, enabled=amp):
                pred = network(inputs)
                weight = 1.0 + 3.0 * (labels.abs() > 10).to(pred.dtype)
                loss = (loss_fn(pred.float(), labels) * weight.float()).mean()
            loss.backward()
            nn.utils.clip_grad_norm_(network.parameters(), 1.0)
            optimizer.step()
            total += float(loss.detach()) * batch.shape[0]
            seen += batch.shape[0]
        scheduler.step()
        span = _span_predictions(network, days, day_index, calib_lo, calib_hi, mean_t, std_t, device)
        rule = choose_rule(span, october.bid[calib_lo:calib_hi], october.ask[calib_lo:calib_hi], october.mid[calib_lo:calib_hi])
        memory = ""
        if device.type == "cuda":
            memory = f" gpu {torch.cuda.max_memory_allocated() / (1024 ** 2):.0f} MiB"
        loss_now = total / max(seen, 1)
        print(
            f"epoch {epoch + 1} loss {loss_now:.4f} "
            f"calib pnl {rule['pnl_cents'] / 100:.2f} usd entries {rule['entries']}{memory}"
        )
        tradable = rule["entries"] > 0 and not rule.get("flat")
        if not tradable:
            if loss_now < best_loss:
                best_loss = loss_now
                best_rule = rule
                best_state = {key: value.detach().cpu().clone() for key, value in network.state_dict().items()}
            continue
        if rule["pnl_cents"] > best_rule.get("pnl_cents", -10**12) or best_rule.get("flat"):
            best_rule = rule
            best_state = {key: value.detach().cpu().clone() for key, value in network.state_dict().items()}
            stale = 0
        else:
            stale += 1
        if epoch + 1 >= 12 and stale >= 6:
            print(f"stopping at epoch {epoch + 1}; calibration pnl stopped rising")
            break
    network.load_state_dict(best_state)
    network.eval()
    return network, best_rule


def _bar_index(day: Day, ts_ms: int) -> int:
    return int((ts_ms - int(day.ts[0])) // BAR_MS)


def evaluate() -> dict:
    device = training_device()
    # A real kernel, so "CUDA is working" is not only a name lookup.
    if device.type == "cuda":
        probe = torch.randn(1024, 1024, device=device)
        probe = probe @ probe
        torch.cuda.synchronize()
        print(f"CUDA matmul ok, result mean {float(probe.mean()):.4f}")
    days = load_days()
    splits = split_indices(days)
    book = splits["train_book"]
    flow = splits["train_flow"]
    print(f"train book rows {book.shape[0]} flow rows {flow.shape[0]}")
    if book.shape[0] < 1000:
        raise SystemExit("Not enough October morning rows to train.")
    drift = _demean_labels(days, book, flow)
    print("removed training drift bps", np.round(drift, 3).tolist())
    mean, std = _mean_std(days, book, flow)
    october_index = int(splits["october_index"][0])
    october = days[october_index]
    calib_lo = _bar_index(october, int(splits["calib_start"][0]))
    test_lo = _bar_index(october, int(splits["test_start"][0]))
    network, rule = fit(days, book, flow, mean, std, (october_index, calib_lo, test_lo), device)
    mean_t = torch.tensor(mean, device=device)
    std_t = torch.tensor(std, device=device)
    network = network.to(device)
    test_hi = october.mid.shape[0] - 1
    test_span = _span_predictions(network, days, october_index, test_lo, test_hi, mean_t, std_t, device)
    scored = simulate(
        test_span[:, rule["horizon_index"]],
        october.bid[test_lo:test_hi],
        october.ask[test_lo:test_hi],
        october.mid[test_lo:test_hi],
        rule["enter_bps"],
        rule["exit_bps"],
        rule["horizon_bars"],
    )
    stake_race = race(
        test_span,
        october.bid[test_lo:test_hi],
        october.ask[test_lo:test_hi],
        october.mid[test_lo:test_hi],
        rule,
    )
    hours = (test_hi - test_lo) * BAR_MS / 3_600_000
    flat_note = (
        "No calibration rule placed enough orders, so the saved entry level stays flat."
        if rule.get("flat")
        else "The entry level is the calibration rule with the highest pnl after taker fees, among rules that placed orders."
    )
    # #region agent log
    try:
        import json, time
        with open(r"C:\Akash\Coding\dubhacks26\debug-a0c237.log", "a", encoding="utf-8") as _f:
            _f.write(json.dumps({"sessionId": "a0c237", "hypothesisId": "D", "location": "train.py:evaluate", "message": "unseen", "data": {"enterBps": rule["enter_bps"], "entries": scored["entries"], "fills": scored["fills"], "pnlCents": scored["pnl_cents"], "feesCents": scored["fees_cents"], "hours": round(hours, 2), "flat": bool(rule.get("flat"))}, "timestamp": int(time.time() * 1000), "runId": "post-fix"}) + "\n")
    except Exception:
        pass
    # #endregion
    network = network.to("cpu")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": network.state_dict(),
            "mean": mean.tolist(),
            "std": std.tolist(),
            "feature_names": FEATURE_NAMES,
            "n_feat": N_FEAT,
            "n_seq": N_SEQ,
            "seq_len": SEQ_LEN,
            "n_horizons": len(HORIZONS),
            "horizons": list(HORIZONS),
            "horizon_index": int(rule["horizon_index"]),
            "horizon_bars": int(rule["horizon_bars"]),
            "enter_bps": float(rule["enter_bps"]),
            "exit_bps": float(rule["exit_bps"]),
            "bar_ms": BAR_MS,
            "warmup": LOOKBACK + 1,
        },
        ARTIFACTS / "signal.pt",
    )
    report = {
        "cuda": device.type == "cuda",
        "device": torch.cuda.get_device_name(0) if device.type == "cuda" else "cpu",
        "days": [day.date for day in days],
        "train_book_rows": int(book.shape[0]),
        "train_flow_rows": int(flow.shape[0]),
        "horizon_bars": int(rule["horizon_bars"]),
        "horizon_seconds": int(rule["horizon_bars"] * BAR_MS / 1000),
        "enter_bps": float(rule["enter_bps"]),
        "exit_bps": float(rule["exit_bps"]),
        "calibration_pnl_usd": rule["pnl_cents"] / 100,
        "calibration_entries": int(rule["entries"]),
        "held_out_pnl_usd": scored["pnl_cents"] / 100,
        "held_out_fees_usd": scored["fees_cents"] / 100,
        "held_out_fills": int(scored["fills"]),
        "held_out_entries": int(scored["entries"]),
        "held_out_hours": round(hours, 2),
        "five_dollar_race": stake_race,
        "flat_return_usd": 0.0,
        "threshold_note": flat_note,
        "window": (
            "Held-out slice of BTCUSDT 2026-10-01 starting at the timestamp in docs/model-issues.md. "
            "September aggTrades and the morning of 2026-10-01 fit the weights. "
            "The 14:00-16:22 UTC stretch picked the entry rule."
        ),
        "note": (
            "PnL is taker fills at the next 200ms touch, with the server's 5 bps fee. "
            "The bot no longer joins both sides of the touch. "
            "A flat book returns 0. This is one recorded day, not a live trading result."
        ),
    }
    (ARTIFACTS / "eval_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if device.type == "cuda":
        print(f"peak GPU memory {torch.cuda.max_memory_allocated() / (1024 ** 2):.0f} MiB")
    return report


def main() -> None:
    evaluate()


if __name__ == "__main__":
    main()
