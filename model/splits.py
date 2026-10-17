"""Time splits with a gap so a label cannot fall inside the next slice."""

from __future__ import annotations


def split_bounds(count: int, horizon: int) -> dict[str, int]:
    if horizon < 1:
        raise ValueError("horizon must be positive")
    usable = count - horizon
    if usable < horizon * 6:
        raise ValueError("not enough rows to split with an embargo")
    train_end = int(usable * 0.60)
    calibration_start = train_end + horizon
    calibration_end = int(usable * 0.80)
    test_start = calibration_end + horizon
    test_end = usable
    if not (0 < train_end <= calibration_start < calibration_end <= test_start < test_end):
        raise ValueError("split bounds overlap")
    return {
        "train_end": train_end,
        "calibration_start": calibration_start,
        "calibration_end": calibration_end,
        "test_start": test_start,
        "test_end": test_end,
        "horizon": horizon,
    }
