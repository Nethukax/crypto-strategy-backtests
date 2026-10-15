"""The most important tests: the strategies must never use future data.

Method: compute signals on the full history, then recompute using ONLY the data
up to each signal. If anything peeks ahead, the two disagree.
"""
import numpy as np
import pytest

from concepts import htf_bias
from config import Config
from data import synthetic
from strategy import AMDStrategy
from trend import FROZEN, backtest as trend_backtest


def signals(df, cfg):
    s = AMDStrategy(cfg).prepare(df)
    return [x for x in (s.on_bar(i) for i in range(len(df))) if x]


def last_signal(df, cfg):
    s = AMDStrategy(cfg).prepare(df)
    out = None
    for i in range(len(df)):
        out = s.on_bar(i)
    return out


@pytest.mark.parametrize("mode", ["structure", "ema"])
def test_htf_bias_uses_only_closed_candles(mode):
    cfg = Config(bias_mode=mode)
    df = synthetic(25)
    full = htf_bias(df, cfg)
    for t in np.random.default_rng(0).integers(200, len(df), 40):
        assert htf_bias(df.iloc[:t + 1], cfg)[-1] == full[t]


@pytest.mark.parametrize("entry_mode", ["limit", "market"])
def test_amd_signals_identical_on_truncated_data(entry_mode):
    cfg = Config(entry_mode=entry_mode)
    df = synthetic(60)
    full = signals(df, cfg)
    assert len(full) >= 3, "test needs signals to mean anything"
    for sig in full[:: max(1, len(full) // 3)][:3]:
        again = last_signal(df.loc[:sig.time], cfg)
        assert again is not None
        assert (again.time, again.side, again.order) == (sig.time, sig.side, sig.order)
        assert again.entry == pytest.approx(sig.entry)
        assert again.stop == pytest.approx(sig.stop)


def test_trend_entries_identical_on_truncated_data():
    df = synthetic(600, "4h", seed=5)
    full, _ = trend_backtest(df, FROZEN, 0.00055, 0.0005)
    assert len(full) >= 5
    for _, tr in full.iloc[:: max(1, len(full) // 5)].iterrows():
        t, _ = trend_backtest(df.loc[:tr.entry_time], FROZEN, 0.00055, 0.0005)
        last = t.iloc[-1]
        assert last.entry_time == tr.entry_time
        assert last.side == tr.side
        assert last.entry == pytest.approx(tr.entry)
