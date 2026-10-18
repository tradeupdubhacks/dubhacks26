import pytest

from model.features import FEATURE_NAMES, INPUT_DIM, LOOKBACK, features_from_bars
from model.splits import split_bounds


def test_slices_do_not_overlap_and_keep_a_label_gap():
    bounds = split_bounds(1000, horizon=8)
    assert bounds["train_end"] + bounds["horizon"] <= bounds["calibration_start"]
    assert bounds["calibration_end"] + bounds["horizon"] <= bounds["test_start"]
    assert bounds["test_start"] < bounds["test_end"]


def test_short_sample_is_rejected():
    with pytest.raises(ValueError):
        split_bounds(20, horizon=8)


def test_features_use_only_the_bar_window():
    bar = (9999, 10001, 10, 30, 10000, 1, 5, 1)
    values = features_from_bars([bar] * (LOOKBACK + 1))
    assert values is not None
    assert len(values) == INPUT_DIM
    assert values[FEATURE_NAMES.index("imbalance")] < 0
    assert values[FEATURE_NAMES.index("spread_bps")] > 0
    assert values[FEATURE_NAMES.index("trade_sign")] == 1
