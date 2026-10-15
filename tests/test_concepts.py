"""Market-structure building blocks: FVG, swings, S/R clustering, volume profile."""
import numpy as np
import pandas as pd

from concepts import add_indicators, cluster_levels, volume_profile
from config import Config


def candles(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx, dtype=float)


def test_bullish_fvg_detected_on_gap_candle():
    cfg = Config(fvg_min_atr=0.0)
    df = candles([(100, 101, 99, 100, 1)] * 20 + [
        (100, 101, 99, 100.5, 1),   # i-2: high 101
        (100.5, 106, 100, 105, 1),  # displacement
        (105, 108, 103, 107, 1),    # i:   low 103 > 101  -> bullish FVG
    ])
    d = add_indicators(df, cfg)
    assert d["bull_fvg"].iloc[-1]
    assert not d["bear_fvg"].iloc[-1]


def test_no_fvg_when_candles_overlap():
    cfg = Config(fvg_min_atr=0.0)
    df = candles([(100, 101, 99, 100, 1)] * 25)
    d = add_indicators(df, cfg)
    assert not d["bull_fvg"].any() and not d["bear_fvg"].any()


def test_swing_high_only_visible_after_confirmation():
    """A swing high at bar 10 must not be known before bar 10 + swing_lookback (no lookahead)."""
    cfg = Config(swing_lookback=3)
    highs = [110 - abs(i - 10) for i in range(25)]   # one clear peak at bar 10
    df = candles([(99, h, 98, 99, 1) for h in highs])
    sh = add_indicators(df, cfg)["sh_conf"]
    assert sh.iloc[:13].isna().all()
    assert sh.iloc[13] == 110


def test_cluster_levels_groups_equal_highs():
    levels = cluster_levels([100.0, 100.2, 99.9, 150.0, np.nan], tol=0.5)
    assert len(levels) == 2
    (p1, n1), (p2, n2) = levels
    assert n1 == 3 and abs(p1 - 100.03) < 0.01
    assert n2 == 1 and p2 == 150.0


def test_volume_profile_poc_sits_where_volume_traded():
    lows = np.array([100, 100, 100, 110, 119.5])
    highs = np.array([101, 101, 101, 111, 120])
    vols = np.array([50, 50, 50, 1, 1])
    poc, val, vah = volume_profile(highs, lows, vols)
    assert 100 <= poc <= 101
    assert val <= poc <= vah
