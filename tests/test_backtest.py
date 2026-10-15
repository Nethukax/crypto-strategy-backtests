"""The backtester's fill rules, checked on hand-made candles with a scripted strategy.
Costs are set to zero so every R value is exact."""
import pandas as pd
import pytest

import backtest
from config import Config
from strategy import Signal

ZERO_COST = dict(fee_rate=0.0, maker_fee=0.0, slippage=0.0)


class ScriptedStrategy:
    """Stands in for AMDStrategy: emits the given signals at the given bars."""
    script = {}

    def __init__(self, cfg):
        self.cfg = cfg

    def prepare(self, df):
        self.O, self.H, self.L = (df[x].to_numpy() for x in ("open", "high", "low"))
        self.hours, self.dates, self.idx = df.index.hour.to_numpy(), df.index.date, df.index
        return self

    def on_bar(self, i):
        return self.script.get(i)


def flat(n=20, start="2026-01-01 08:00"):
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}, index=idx)


def sig(df, i, side, entry, stop, target, order="market"):
    return Signal(i, df.index[i], side, entry, stop, target, "test", 0, [], order)


@pytest.fixture
def run(monkeypatch):
    def _run(df, script, **cfg_kw):
        ScriptedStrategy.script = script
        monkeypatch.setattr(backtest, "AMDStrategy", ScriptedStrategy)
        trades, _ = backtest.run_backtest(df, Config(**ZERO_COST, **cfg_kw))
        return trades
    return _run


def test_target_hit_pays_reward_to_risk(run):
    df = flat()
    df.loc[df.index[1], "high"] = 102.5
    t = run(df, {0: sig(df, 0, 1, 100, 99, 102)})
    assert t.exit_reason.tolist() == ["target"]
    assert t.R.iloc[0] == pytest.approx(2.0)


def test_stop_assumed_first_when_both_hit_in_one_candle(run):
    df = flat()
    df.loc[df.index[1], ["high", "low"]] = [102.5, 98.9]
    t = run(df, {0: sig(df, 0, 1, 100, 99, 102)})
    assert t.exit_reason.tolist() == ["stop"]
    assert t.R.iloc[0] == pytest.approx(-1.0)


def test_short_stop_gap_fills_at_open(run):
    df = flat()
    df.loc[df.index[1], ["open", "high"]] = [103, 103]   # gaps straight over the stop at 101
    t = run(df, {0: sig(df, 0, -1, 100, 101, 98)})
    assert t.exit_reason.tolist() == ["stop (gap)"]
    assert t.R.iloc[0] == pytest.approx(-3.0)


def test_limit_order_never_touched_is_not_a_trade(run):
    df = flat()
    t = run(df, {0: sig(df, 0, 1, 99.5, 99, 101, order="limit")})
    assert t.empty
    assert backtest.run_backtest.missed == 1


def test_limit_filled_and_stopped_in_same_candle_is_a_loss(run):
    df = flat()
    df.loc[df.index[1], "low"] = 98.8
    t = run(df, {0: sig(df, 0, 1, 99.5, 99, 101, order="limit")})
    assert t.exit_reason.tolist() == ["stop"]
    assert t.R.iloc[0] == pytest.approx(-1.0)


def test_position_closed_at_session_close(run):
    df = flat(start="2026-01-01 20:00")
    t = run(df, {1: sig(df, 1, 1, 100, 99, 102)})
    assert t.exit_reason.tolist() == ["session close"]
    assert t.exit_time.iloc[0].hour == 21


def test_risk_per_trade_sizes_the_position(run):
    df = flat()
    df.loc[df.index[1], "low"] = 98
    t = run(df, {0: sig(df, 0, 1, 100, 99, 102)})
    assert t.pnl.iloc[0] == pytest.approx(-0.01 * 1000)   # 1% of the 1000 starting equity
