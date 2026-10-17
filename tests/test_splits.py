import pytest

from model.features import features_from_observation
from model.splits import split_bounds


def test_slices_do_not_overlap_and_keep_a_label_gap():
    bounds = split_bounds(1000, horizon=8)
    assert bounds["train_end"] + bounds["horizon"] <= bounds["calibration_start"]
    assert bounds["calibration_end"] + bounds["horizon"] <= bounds["test_start"]
    assert bounds["test_start"] < bounds["test_end"]


def test_short_sample_is_rejected():
    with pytest.raises(ValueError):
        split_bounds(20, horizon=8)


def test_features_use_only_the_observation_payload():
    observation = {
        "mid": 10000,
        "bids": [{"price": 9999, "size": 10}],
        "asks": [{"price": 10001, "size": 30}],
        "mids": [9900, 10000],
    }
    imbalance, mid_change, spread, mid_var = features_from_observation(observation)
    assert imbalance < 0
    assert mid_change > 0
    assert spread > 0
    assert mid_var == 0
