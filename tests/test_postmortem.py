"""The post-mortem replay must follow the same rules as the backtester."""
import numpy as np
import pytest

from postmortem import simulate


def bars(highs, lows, opens=None):
    H, L = np.array(highs, float), np.array(lows, float)
    return H, L, np.array(opens if opens else [100.0] * len(highs), float)


def test_replay_target_and_stop():
    H, L, O = bars([100, 103, 100], [100, 100, 100])
    assert simulate(H, L, O, 1, 100, 99, 102, 1, 2) == pytest.approx(2.0)
    H, L, O = bars([100, 100, 100], [100, 98, 100])
    assert simulate(H, L, O, 1, 100, 99, 102, 1, 2) == -1.0


def test_replay_checks_stop_first_like_the_backtester():
    H, L, O = bars([100, 103, 100], [100, 98, 100])
    assert simulate(H, L, O, 1, 100, 99, 102, 1, 2) == -1.0


def test_replay_exits_at_session_close_open():
    H, L, O = bars([100, 100.5, 101], [100, 99.8, 101], [100, 100, 100.5])
    assert simulate(H, L, O, 1, 100, 99, 102, 1, 2) == pytest.approx(0.5)


def test_wider_stop_survives_a_shakeout():
    H, L, O = bars([100, 100, 103, 100], [100, 98.6, 100, 100])
    assert simulate(H, L, O, 1, 100, 99, 102, 1, 3) == -1.0          # 1R stop: shaken out
    assert simulate(H, L, O, 1, 100, 98, 102, 1, 3) == pytest.approx(1.0)  # 2R stop: survives, +2/2
