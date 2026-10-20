"""Train the session signal, calibrate the policy on held-out calibration windows, and score the test windows.

    python -m model.train_session

Inputs : model-API observations on the BTC replay (11 features, features_session.py), 200 ms live tick.
Output : P(mid is higher HORIZON ticks from now), saved to artifacts/session_signal.pt together with the
         policy parameters picked on calibration data. Walk-forward folds mirror splits.split_bounds' embargo idea.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch
from torch import nn

from model.backtest import SUB, Tape, run_session, training_features
from model.features_session import FEATURE_NAMES_SESSION, VOL_INDEX
from model.net import SmallNet
from model.session_policy import SessionParams

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
CACHE = Path(__file__).resolve().parent.parent / "hft" / "data" / "out" / "session_feats.npy"
HORIZON = 25                       # 5 s at the 200 ms tick
HOUR = 18_000
FOLDS = [  # (train_end, calibration_end == test_start, test_end) in ticks
    (10 * HOUR, 12 * HOUR, 16 * HOUR),
    (14 * HOUR, 16 * HOUR, 20 * HOUR),
    (18 * HOUR, 20 * HOUR, 24 * HOUR),
]
EMBARGO = 32 + HORIZON + 2


def device() -> torch.device:
    return torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")


def fit(x: np.ndarray, y: np.ndarray, dev: torch.device, epochs: int = 6, seed: int = 0) -> SmallNet:
    torch.manual_seed(seed)
    net = SmallNet(x.shape[1]).to(dev)
    xt, yt = torch.tensor(x, device=dev), torch.tensor(y, device=dev)
    opt, loss_fn = torch.optim.Adam(net.parameters(), lr=1e-3), nn.BCEWithLogitsLoss()
    for epoch in range(epochs):
        order = torch.randperm(len(xt), device=dev)
        for s in range(0, len(xt), 2048):
            b = order[s:s + 2048]
            opt.zero_grad(set_to_none=True)
            loss_fn(net(xt[b]), yt[b]).backward()
            opt.step()
    return net.eval()


def score(net: SmallNet, x: np.ndarray, dev: torch.device) -> np.ndarray:
    with torch.no_grad():
        return torch.sigmoid(net(torch.tensor(x, device=dev))).cpu().numpy()


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def summarize(outcomes: list, deposit: int) -> dict:
    n = len(outcomes)
    res = [o.result for o in outcomes]
    return {
        "sessions": n,
        "p_ceiling": res.count("ceiling") / n, "p_floor": res.count("floor") / n, "p_timeout": res.count("timeout") / n,
        "mean_final_stake_pct": float(np.mean([(o.stake - deposit) / deposit for o in outcomes]) * 100),
        "mean_trades": float(np.mean([o.trades for o in outcomes])),
    }


def sessions(tape, conf, vol, direction, deposit, floor, ceiling, windows, P, every=600):
    """Sessions start every `every` ticks (2 min) inside each (start, end) window, and end with the window."""
    out = []
    for a, b in windows:
        for j0 in range(a, b - 9000, every):
            out.append(run_session(tape, conf, vol, direction, deposit, floor, ceiling, j0, b, P))
    return out


def main() -> None:
    t0, dev = time.time(), device()
    tape = Tape()
    feats = np.load(CACHE) if CACHE.exists() else training_features(tape)
    if not CACHE.exists():
        np.save(CACHE, feats)
    mid = tape.mid200.astype(np.float64)
    n = len(mid)
    ret = np.full(n, np.nan)
    ret[:n - HORIZON] = (mid[HORIZON:] / mid[:n - HORIZON] - 1) * 1e4          # bps
    label = (ret > 0).astype(np.float32)
    scores = np.full(n, 0.5, dtype=np.float32)
    report = {"horizon_ticks": HORIZON, "tick_ms": 200, "feature_names": list(FEATURE_NAMES_SESSION), "folds": []}
    last = None
    for k, (tr_end, ca_end, te_end) in enumerate(FOLDS):
        ca_start = ca_end - 2 * HOUR if k == 0 else FOLDS[k][1] - 2 * HOUR
        tr = np.arange(32, tr_end - EMBARGO)
        tr = tr[(ret[tr] != 0) & ~np.isnan(ret[tr])]
        mean, std = feats[tr].mean(0), feats[tr].std(0) + 1e-9
        x = np.clip((feats - mean) / std, -5, 5).astype(np.float32)
        net = fit(x[tr], label[tr], dev)
        s = score(net, x, dev)
        lo = tr_end                      # everything after the training window is out of sample for this fold
        scores[lo:te_end] = s[lo:te_end]
        te = np.arange(ca_end, te_end - HORIZON)
        te = te[ret[te] != 0]
        ic = float(np.corrcoef(s[te] - 0.5, ret[te])[0, 1])
        acc = float(((s[te] > 0.5) == (ret[te] > 0)).mean())
        conf = np.abs(s[te] - 0.5) * 2
        top = conf >= np.quantile(conf, 0.9)
        report["folds"].append({"fold": k, "train_ticks": int(len(tr)), "test_ic_pearson": ic, "test_dir_acc": acc,
                                "test_dir_acc_top10pct_conf": float(((s[te][top] > 0.5) == (ret[te][top] > 0)).mean()),
                                "mean_abs_5s_move_bps": float(np.abs(ret[te]).mean())})
        print(f"fold {k}: IC {ic:+.4f}  acc {acc:.4f}  top10% acc {report['folds'][-1]['test_dir_acc_top10pct_conf']:.4f}  ({time.time() - t0:.0f}s)", flush=True)
        last = (net, mean, std, tr_end, ca_end)
    np.save(CACHE.with_name("session_scores.npy"), scores)
    # deployed artifact = last fold (train 0-18 h, calibration 18-20 h, test 20-24 h)
    net, mean, std, *_ = last
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": net.cpu().state_dict(), "mean": mean.tolist(), "std": std.tolist(), "threshold": 0.0,
                "feature_names": FEATURE_NAMES_SESSION, "n_features": len(FEATURE_NAMES_SESSION),
                "horizon_ticks": HORIZON, "tick_ms": 200, "session_params": asdict(SessionParams())},
               ARTIFACTS / "session_signal.pt")
    (ARTIFACTS / "session_train_report.json").write_text(json.dumps(report, indent=2))
    print("saved", ARTIFACTS / "session_signal.pt")


if __name__ == "__main__":
    main()
