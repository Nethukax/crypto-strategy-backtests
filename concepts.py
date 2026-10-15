"""Market-structure concepts turned into code.

Everything here is CAUSAL: a value at bar i only uses data from bars <= i.
Swing points need `swing_lookback` future bars to be confirmed, so they are
shifted forward and only become visible once confirmed (no lookahead).
"""
import numpy as np
import pandas as pd


def add_indicators(df: pd.DataFrame, cfg) -> pd.DataFrame:
    df = df.copy()
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    pc = c.shift(1)

    # ATR (volatility yardstick for every tolerance in the bot)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / cfg.atr_period, adjust=False).mean()

    # Volume: average of the PREVIOUS n bars, spike = this bar much bigger
    df["vol_avg"] = df["volume"].rolling(cfg.vol_period).mean().shift(1)
    df["vol_spike"] = df["volume"] > cfg.vol_spike_mult * df["vol_avg"]

    # Fair Value Gaps (3-candle imbalance), confirmed at the close of bar i
    #   bullish: low[i]  > high[i-2]   bearish: high[i] < low[i-2]
    min_gap = cfg.fvg_min_atr * df["atr"]
    df["bull_fvg"] = (l - h.shift(2)) > min_gap
    df["bear_fvg"] = (l.shift(2) - h) > min_gap

    # Swing highs/lows, only visible `k` bars later when they are confirmed
    k = cfg.swing_lookback
    w = 2 * k + 1
    is_sh = h == h.rolling(w, center=True).max()
    is_sl = l == l.rolling(w, center=True).min()
    df["sh_conf"] = h.where(is_sh).shift(k)
    df["sl_conf"] = l.where(is_sl).shift(k)

    # Candle patterns
    po, pcl = o.shift(1), c.shift(1)
    df["engulf_bull"] = (c > o) & (pcl < po) & (c >= po) & (o <= pcl)
    df["engulf_bear"] = (c < o) & (pcl > po) & (c <= po) & (o >= pcl)

    df["hour"] = df.index.hour
    df["date"] = df.index.date
    return df


def cluster_levels(prices, tol):
    """Group nearby swing prices into support/resistance levels.
    Returns [(level_price, touches)]. 2+ touches = equal highs/lows = liquidity pool."""
    prices = sorted(p for p in prices if not np.isnan(p))
    clusters = []
    for p in prices:
        if clusters and p - clusters[-1][-1] <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [(float(np.mean(cl)), len(cl)) for cl in clusters]


def volume_profile(highs, lows, vols, bins=40, value_area=0.70):
    """Volume-at-price. Each bar's volume is spread over the price bins it covered.
    Returns (POC, value_area_low, value_area_high)."""
    lo, hi = float(np.min(lows)), float(np.max(highs))
    if hi <= lo:
        return lo, lo, hi
    edges = np.linspace(lo, hi, bins + 1)
    prof = np.zeros(bins)
    a = np.clip(np.searchsorted(edges, lows, "right") - 1, 0, bins - 1)
    b = np.clip(np.searchsorted(edges, highs, "right") - 1, 0, bins - 1)
    for ai, bi, v in zip(a, b, vols):
        prof[ai:bi + 1] += v / (bi - ai + 1)
    p = int(np.argmax(prof))
    centers = (edges[:-1] + edges[1:]) / 2
    # expand the value area outward from the POC until it holds 70% of volume
    total, acc, lo_i, hi_i = prof.sum(), prof[p], p, p
    while total > 0 and acc / total < value_area and (lo_i > 0 or hi_i < bins - 1):
        down = prof[lo_i - 1] if lo_i > 0 else -1
        up = prof[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1
            acc += prof[hi_i]
        else:
            lo_i -= 1
            acc += prof[lo_i]
    return float(centers[p]), float(edges[lo_i]), float(edges[hi_i + 1])


def _pandas_freq(tf):
    return tf[:-1] + "min" if tf.endswith("m") else tf


def htf_bias(df, cfg):
    """Direction from a higher timeframe (default 1h), aligned onto the trading candles.

    +1 = bullish (only longs), -1 = bearish (only shorts), 0 = unclear (no trades).
    A 1h candle is only used once it has fully CLOSED, so there is no lookahead.
    """
    from data import TF_MIN
    bar = pd.Timedelta(minutes=TF_MIN[cfg.timeframe])
    if cfg.bias_mode == "none":
        return np.zeros(len(df))
    htf = df.resample(_pandas_freq(cfg.bias_timeframe), label="right", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    htf = htf[htf.index <= df.index[-1] + bar]          # drop the unfinished 1h candle
    h, l, c = htf["high"].to_numpy(), htf["low"].to_numpy(), htf["close"].to_numpy()
    n = len(htf)
    bias = np.zeros(n)

    if cfg.bias_mode == "structure":
        # Market structure: close above the last confirmed swing high = bullish (break of structure),
        # close below the last confirmed swing low = bearish. Holds until the opposite break.
        k, state, last_sh, last_sl = cfg.swing_lookback, 0, np.nan, np.nan
        for j in range(n):
            m = j - k
            if m - k >= 0:
                if h[m] == h[m - k:m + k + 1].max():
                    last_sh = h[m]
                if l[m] == l[m - k:m + k + 1].min():
                    last_sl = l[m]
            if not np.isnan(last_sh) and c[j] > last_sh:
                state = 1
            elif not np.isnan(last_sl) and c[j] < last_sl:
                state = -1
            bias[j] = state
    elif cfg.bias_mode == "ema":
        e = pd.Series(c).ewm(span=cfg.bias_ema, adjust=False).mean()
        slope = e - e.shift(3)
        bias = np.where((c > e) & (slope > 0), 1, np.where((c < e) & (slope < 0), -1, 0)).astype(float)
    else:
        raise ValueError(f"unknown bias_mode {cfg.bias_mode}")

    # index of htf = candle CLOSE time; a 15m candle may use it once the 15m candle has closed too
    s = pd.Series(bias, index=htf.index)
    aligned = s.reindex(df.index + bar, method="ffill").to_numpy()
    return np.nan_to_num(aligned, nan=0.0)
