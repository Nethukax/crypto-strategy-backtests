"""Candle data: download from the exchange (cached to CSV) or generate synthetic test data."""
import os
import time
import numpy as np
import pandas as pd

TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


def make_exchange(cfg, auth=False, sandbox=False):
    import ccxt
    params = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
    if auth:
        params["apiKey"] = os.environ["EXCHANGE_API_KEY"]
        params["secret"] = os.environ["EXCHANGE_API_SECRET"]
    ex = getattr(ccxt, cfg.exchange)(params)
    if sandbox:
        ex.set_sandbox_mode(True)
    return ex


def to_frame(rows):
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").set_index("ts").sort_index().astype(float)


def _fetch_with_retry(ex, *args, **kwargs):
    for attempt in range(4):
        try:
            return ex.fetch_ohlcv(*args, **kwargs)
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))      # exchange hiccup: wait and retry


def fetch_history(cfg, days=180, cache_dir="data", max_age_hours=6):
    """Download `days` of closed candles (public endpoint, no API key)."""
    os.makedirs(cache_dir, exist_ok=True)
    name = f"{cfg.exchange}_{cfg.symbol.replace('/', '').replace(':', '_')}_{cfg.timeframe}_{days}d.csv"
    path = os.path.join(cache_dir, name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_hours * 3600:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    ex = make_exchange(cfg)
    step = TF_MIN[cfg.timeframe] * 60_000
    since = ex.milliseconds() - days * 86_400_000
    rows = []
    while since < ex.milliseconds():
        batch = _fetch_with_retry(ex, cfg.symbol, cfg.timeframe, since=since, limit=1000)
        if not batch:
            if rows:
                break
            since += 1000 * step               # coin wasn't listed yet: jump forward
            continue
        rows += batch
        since = batch[-1][0] + 1
        if len(batch) < 2 or batch[-1][0] >= ex.milliseconds() - 60_000:
            break
    if not rows:
        raise ValueError(f"no candles returned for {cfg.symbol}")
    df = to_frame(rows).iloc[:-1]   # drop the still-forming candle
    df.to_csv(path)
    return df


def synthetic(days=120, tf="15m", seed=7, start_price=60000.0):
    """Fake market for testing the code offline. NOT for judging profitability."""
    rng = np.random.default_rng(seed)
    step = TF_MIN[tf]
    n, sub = days * 1440 // step, 12
    idx = pd.date_range("2026-01-01", periods=n, freq=f"{step}min", tz="UTC")
    hours = idx.hour.to_numpy()
    sess_vol = np.where(hours < 7, 0.5, np.where(hours < 16, 1.4, 1.0))     # quiet Asia
    regime = np.exp(np.cumsum(rng.normal(0, 0.02, n)))                       # vol clustering
    regime /= regime.mean()
    sigma = 0.0022 * sess_vol * np.clip(regime, 0.4, 2.5) / np.sqrt(sub)
    micro = rng.standard_t(4, (n, sub)) * sigma[:, None] * 0.7
    path = start_price * np.exp(np.cumsum(micro.ravel())).reshape(n, sub)
    close = path[:, -1]
    open_ = np.concatenate([[start_price], close[:-1]])
    high = np.maximum(path.max(1), open_)
    low = np.minimum(path.min(1), open_)
    vol = (np.abs(np.log(close / open_)) / sigma.clip(1e-9) + 0.5) * rng.lognormal(3, 0.4, n) * sess_vol
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
