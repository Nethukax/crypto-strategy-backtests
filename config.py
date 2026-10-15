"""All tunable settings in one place. Change values here, not in the code."""
from dataclasses import dataclass


@dataclass
class Config:
    # ---- Market ----
    exchange: str = "bybit"            # "bybit" or "binanceusdm" (Binance USDT-M futures)
    symbol: str = "TAO/USDT:USDT"      # ccxt perpetual symbol (Bittensor)
    timeframe: str = "15m"

    # ---- AMD sessions (UTC hours) ----
    # Accumulation = Asian session range, Manipulation = London open sweep,
    # Distribution = the real move through London/New York.
    accumulation_start: int = 0
    accumulation_end: int = 7          # 00:00-07:00 UTC builds the range
    manipulation_end: int = 12         # a sweep must START before 12:00 UTC
    session_close: int = 21            # flat by 21:00 UTC, no overnight holds

    # ---- Concept parameters ----
    swing_lookback: int = 3            # bars each side to confirm a swing high/low
    atr_period: int = 14
    vol_period: int = 20
    vol_spike_mult: float = 1.5        # volume > 1.5x average = spike
    fvg_min_atr: float = 0.15          # ignore tiny gaps (< 0.15 ATR)
    level_tolerance_atr: float = 0.5   # how close prices must be to count as the "same" level
    min_range_atr: float = 1.5         # Asian range must be a real range...
    max_range_atr: float = 12.0        # ...but not a trend day already
    max_bars_after_sweep: int = 10     # displacement must come within this many bars
    poc_lookback_hours: int = 24       # volume profile window
    sr_lookback_bars: int = 400        # support/resistance memory
    stop_buffer_atr: float = 0.1
    retest_tolerance_atr: float = 0.25
    min_confluence: int = 1            # extra confirmations required (0-4)
    enable_breakout_setup: bool = True

    # ---- Higher-timeframe direction (bias) ----
    bias_timeframe: str = "1h"         # direction comes from this chart, trades happen on `timeframe`
    bias_mode: str = "structure"       # "structure" (break of 1h swing highs/lows), "ema", or "none"
    bias_ema: int = 50

    # ---- Entries ----
    entry_mode: str = "limit"          # "limit" = order at the FVG midpoint, "market" = at candle close
    limit_wait_bars: int = 4           # cancel the limit if not filled within 4 candles
    breakout_retest_bars: int = 12

    # ---- Risk ----
    risk_per_trade: float = 0.01       # 1% of equity lost if the stop is hit
    min_rr: float = 1.5
    max_cost_r: float = 0.25           # skip trades where fees+slippage > 25% of the risk
    max_rr: float = 4.0
    max_leverage: float = 5.0          # hard cap on position size
    max_daily_loss: float = 0.03       # stop trading for the day after -3%
    max_trades_per_day: int = 2
    fee_rate: float = 0.00055          # taker fee (market orders, stop-loss)
    maker_fee: float = 0.0002          # maker fee (limit entry, take-profit)
    slippage: float = 0.0005           # TAO is thinner than BTC, so assume more slippage
    starting_equity: float = 1000.0    # backtest only

    # ---- Live ----
    live: bool = False                 # False = TESTNET. Real money needs True AND the env var.
    candles_to_load: int = 1000
