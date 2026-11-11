import numpy as np

from ymzz.indicators import rsi
from ymzz.strategies import (
    STRATEGIES,
    bollinger_reversion_signal,
    compute_signals,
    grid_exposure,
    rsi_three_signal,
    state_cross,
)


def test_every_strategy_has_at_most_three_decision_indicators():
    assert set(STRATEGIES) == {"rsi_single", "rsi_three", "macd", "bollinger", "ma_cross", "grid", "rsi_macd", "hf_trigger"}
    for spec in STRATEGIES.values():
        assert 1 <= len(spec.decision_indicators) <= 3


def test_rsi_period_changes_values_and_matches_hand_calc():
    close = [10, 11, 12, 11, 10]
    short = rsi(close, 2)
    longer = rsi(close, 3)
    assert short[2] == 100
    assert short[3] == 50
    assert short[4] == 25
    assert not np.allclose(short[3:], longer[3:], equal_nan=True)
    series = [float(i) + (i % 3) for i in range(1, 80)]
    _, ind_a, _ = compute_signals("rsi_three", series, {"rsi_short": 6, "rsi_mid": 12, "rsi_long": 24}, False)
    _, ind_b, _ = compute_signals("rsi_three", series, {"rsi_short": 14, "rsi_mid": 19, "rsi_long": 24}, False)
    assert ind_a["rsi_short"] != ind_b["rsi_short"]
    assert ind_a["rsi_mid"] != ind_b["rsi_mid"]


def test_rsi_three_trigger_is_short_cross_not_sorted():
    short = [40, 55, 45, 60]
    mid = [50, 50, 50, 50]
    long = [30, 30, 30, 70]
    sig = rsi_three_signal(short, mid, long, False)
    assert sig[1] == 1
    assert sig[2] == 0
    assert sig[3] == 0


def test_cross_and_grid_and_bollinger_rules():
    fast = [1, 1, 3]
    slow = [2, 2, 2]
    assert state_cross(fast, slow, False)[-1] == 1
    assert state_cross([3, 3, 1], [2, 2, 2], True)[-1] == -1
    exposure = grid_exposure([115, 105, 120, 79], 80, 120, 4)
    assert exposure[0] == 0.25
    assert exposure[1] == 0.5
    assert exposure[2] == 0
    assert exposure[3] == 1
    sig = bollinger_reversion_signal([10, 9], [10, 9.5], [11, 11])
    assert sig[-1] == 1
