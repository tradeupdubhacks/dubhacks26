"""Calibrate on 10-14 h, test on 14-24 h (out of sample), report session outcomes.

    python -m model.evaluate_session

A session = deposit D, floor, ceiling = D * (1 + goal). It starts every 2 minutes inside a window and ends at the
floor, at the ceiling, or when the window ends. It is NOT a profit claim: one recorded day, windows overlap.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from model import backtest, sizing
from model.backtest import Tape, run_session
from model.session_policy import SessionParams
from model.train_session import ARTIFACTS, CACHE, HOUR, summarize

CALIB = [(10 * HOUR, 14 * HOUR)]
TEST = [(14 * HOUR, 18 * HOUR), (18 * HOUR, 24 * HOUR)]
EVERY = 600
DEPOSIT = 10_000
CONFIGS = {"goal+1%/floor-2%": (0.01, 0.02), "goal+1%/floor-1%": (0.01, 0.01),
           "goal+5%/floor-5%": (0.05, 0.05), "goal+5%/floor-10%": (0.05, 0.10)}


def run(tape, conf, vol, direction, deposit, goal, loss, windows, P):
    floor, ceiling = int(round(deposit * (1 - loss))), int(round(deposit * (1 + goal)))
    out, hours = [], []
    for a, b in windows:
        for j0 in range(a, b - 9000, EVERY):
            out.append(run_session(tape, conf, vol, direction, deposit, floor, ceiling, j0, b, P))
            hours.append(j0 // HOUR)
    return out, np.asarray(hours)


def stats(outcomes, hours, deposit, boots=400, seed=0):
    r = [o.result for o in outcomes]
    ceil = np.array([x == "ceiling" for x in r], float)
    floor = np.array([x == "floor" for x in r], float)
    pnl = np.array([(o.stake - deposit) / deposit * 100 for o in outcomes])
    rng, uniq = np.random.default_rng(seed), np.unique(hours)
    boot = []
    for _ in range(boots):
        pick = rng.choice(uniq, len(uniq))
        idx = np.concatenate([np.flatnonzero(hours == h) for h in pick])
        boot.append((ceil[idx].mean(), floor[idx].mean(), pnl[idx].mean()))
    lo, hi = np.percentile(boot, [2.5, 97.5], axis=0)
    return {"sessions": len(r), "p_ceiling": ceil.mean(), "p_ceiling_ci": [lo[0], hi[0]], "p_floor": floor.mean(),
            "p_floor_ci": [lo[1], hi[1]], "p_timeout": 1 - ceil.mean() - floor.mean(), "mean_stake_change_pct": pnl.mean(),
            "mean_stake_change_ci": [lo[2], hi[2]], "mean_trades": float(np.mean([o.trades for o in outcomes]))}


def main() -> None:
    # The reported results use Binance's regular-user rates; the platform default is now 0 (see src/types.ts).
    sizing.TAKER_BPS, sizing.MAKER_BPS = 5.0, 2.0
    backtest.FEES.update(taker=5.0, maker=2.0)
    tape = Tape()
    feats = np.load(CACHE)
    scores = np.load(CACHE.with_name("session_scores.npy"))
    conf, vol = np.abs(scores - 0.5) * 2, feats[:, 7]
    model_dir = np.where(scores >= 0.5, 1, -1)
    rng = np.random.default_rng(1)
    random_dir = rng.choice([-1, 1], len(scores))
    ca = np.concatenate([np.arange(a, b) for a, b in CALIB])
    report = {"calibration": "10-14 h (scores from the fold trained on 0-10 h)", "test": "14-24 h, out of sample",
              "deposit_cents": DEPOSIT, "configs": {}}
    for name, (goal, loss) in CONFIGS.items():
        grid = []
        for tp, stop_mult, hold, tq, vq in itertools.product((15, 25, 40, 60), (0.5, 1.0), (3000, 9000), (0.0, 0.9, 0.99), (0.0, 0.8)):
            P = SessionParams(take_profit_bps=tp, stop_bps=tp * stop_mult, max_hold_steps=hold,
                              threshold=float(np.quantile(conf[ca], tq)), vol_gate=float(np.quantile(vol[ca], vq)))
            out, hrs = run(tape, conf, vol, model_dir, DEPOSIT, goal, loss, CALIB, P)
            s = summarize(out, DEPOSIT)
            grid.append((s["mean_final_stake_pct"], s["p_ceiling"], P))
        best_ev = max(grid, key=lambda g: (g[0], g[1]))
        best_p = max(grid, key=lambda g: (g[1], g[0]))
        entry = {"calibration_best_ev": {"ev_pct": best_ev[0], "p_ceiling": best_ev[1], "params": asdict(best_ev[2])},
                 "calibration_best_p_ceiling": {"ev_pct": best_p[0], "p_ceiling": best_p[1], "params": asdict(best_p[2])},
                 "test": {}}
        for label, chosen in (("ev_max", best_ev[2]), ("p_ceiling_max", best_p[2])):
            res = {}
            for rule, d in (("model", model_dir), ("random_dir", random_dir), ("always_long", np.ones(len(scores), int))):
                out, hrs = run(tape, conf, vol, d, DEPOSIT, goal, loss, TEST, chosen)
                res[rule] = stats(out, hrs, DEPOSIT)
            backtest.FEES.update(taker=0.0, maker=0.0)
            out, hrs = run(tape, conf, vol, model_dir, DEPOSIT, goal, loss, TEST, chosen)
            res["model_zero_fees_diagnostic"] = stats(out, hrs, DEPOSIT)
            backtest.FEES.update(taker=5.0, maker=2.0)
            entry["test"][label] = res
        report["configs"][name] = entry
        m = entry["test"]["p_ceiling_max"]["model"]; e = entry["test"]["ev_max"]["model"]
        print(f"{name}: calib best EV {best_ev[0]:+.2f}% (P ceil {best_ev[1]:.2f}) | test ev_max model: P ceil {e['p_ceiling']:.2f} "
              f"floor {e['p_floor']:.2f} EV {e['mean_stake_change_pct']:+.2f}% | p_ceiling_max: P ceil {m['p_ceiling']:.2f} floor {m['p_floor']:.2f} EV {m['mean_stake_change_pct']:+.2f}%", flush=True)
    (ARTIFACTS / "session_eval_report.json").write_text(json.dumps(report, indent=2, default=float))
    deploy(report)


def deploy(report: dict, config: str = "goal+1%/floor-2%") -> None:
    """Ship the capital-preserving (calibration EV-max) parameters inside the artifact the live bot loads."""
    import torch
    path = ARTIFACTS / "session_signal.pt"
    blob = torch.load(path, map_location="cpu", weights_only=False)
    blob["session_params"] = report["configs"][config]["calibration_best_ev"]["params"]
    blob["session_params_note"] = f"calibration EV-max for {config}; mostly idle by design, see session_eval_report.json"
    torch.save(blob, path)
    print("deployed params:", blob["session_params"])


if __name__ == "__main__":
    main()
