"""Trend engine sanity: no edge on random data, clear edge on trending data,
and the portfolio simulator reproduces the single-coin trades exactly."""
import numpy as np
import pandas as pd
import pytest

from data import synthetic
from portfolio import run_portfolio
from trend import FROZEN, backtest

NO_COSTS = dict(FROZEN, funding_per_8h=0.0)


def series(drift, seed, n=9000):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.02, n) + drift * np.sign(np.sin(np.arange(n) / 400))
    c = 100 * np.exp(np.cumsum(steps))
    o = np.r_[100, c[:-1]]
    idx = pd.date_range("2020-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame(dict(open=o, high=np.maximum(o, c) * 1.004, low=np.minimum(o, c) * 0.996,
                             close=c, volume=1.0), index=idx)


def test_random_walk_has_no_edge_without_costs():
    t, _ = backtest(series(0.0, 9), NO_COSTS, 0.0, 0.0)
    assert len(t) > 50
    assert abs(t.R.mean()) < 0.25


def test_trending_market_is_profitable():
    t, _ = backtest(series(0.003, 9), NO_COSTS, 0.0, 0.0)
    assert t.R.mean() > 0.5


def test_stopped_trades_lose_about_one_r():
    t, _ = backtest(series(0.0, 3), NO_COSTS, 0.0, 0.0)
    first_bar_stops = t[(t.reason == "trailing stop") & (t.bars == 1)]
    assert (t.R >= -1.0 - 1e-9).sum() > 0.9 * len(t)   # only gaps can lose more than 1R
    if len(first_bar_stops):
        assert first_bar_stops.R.max() <= 1e-9


def test_portfolio_reproduces_single_coin_trades():
    data = {f"C{k}": synthetic(600, "4h", seed=300 + k) for k in range(3)}
    pt, *_ = run_portfolio(data, FROZEN, 0.00055, 0.0005)
    single = pd.concat([backtest(df, FROZEN, 0.00055, 0.0005)[0].assign(coin=c) for c, df in data.items()])
    single = single[single.reason == "trailing stop"]
    m = pt.merge(single, on=["coin", "entry_time"], suffixes=("_p", "_s"))
    assert len(pt) > 20
    assert len(m) == len(pt) == len(single)
    assert np.abs(m.R_p - m.R_s).max() == pytest.approx(0, abs=1e-9)
