"""AMD (Accumulation - Manipulation - Distribution) strategy, bar by bar.

Daily flow (all times UTC):
  1. ACCUMULATION  00:00-07:00  record the Asian range (high/low = liquidity on both sides)
  2. MANIPULATION  07:00-12:00  price runs one side of the range (stop hunt / liquidity sweep)
       - wick beyond and closes back inside            -> SWEEP  (fake move, fade it)
       - 2+ closes outside WITH a volume spike         -> BREAKOUT (real move, follow it)
  3. DISTRIBUTION  until 21:00
       SWEEP setup:    wait for a displacement candle that leaves a Fair Value Gap
                       back toward the range -> enter, stop beyond the sweep wick,
                       target the opposite side of the range (the other liquidity pool)
       BREAKOUT setup: wait for a retest of the broken range edge that holds
                       (old resistance becomes support) -> enter with the breakout

Confluence (each adds +1 to the score; `min_confluence` sets how many are required):
  - volume spike on the sweep/breakout or the entry candle
  - entry on the right side of the 24h volume Point of Control (buy discount / sell premium)
  - the swept or broken level was a prior support/resistance pool (equal highs/lows)
  - engulfing candle on entry
"""
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

from concepts import add_indicators, cluster_levels, volume_profile, htf_bias
from data import TF_MIN


@dataclass
class Signal:
    i: int
    time: pd.Timestamp
    side: int              # +1 long, -1 short
    entry: float
    stop: float
    target: float
    setup: str
    score: int
    reasons: list = field(default_factory=list)
    order: str = "market"   # "market" = fill at close, "limit" = resting order at `entry`

    @property
    def rr(self):
        return abs(self.target - self.entry) / abs(self.entry - self.stop)


class AMDStrategy:
    def __init__(self, cfg):
        self.cfg = cfg

    # ------------------------------------------------------------------ setup
    def prepare(self, df: pd.DataFrame):
        cfg = self.cfg
        tf = TF_MIN[cfg.timeframe]
        acc_hours = cfg.accumulation_end - cfg.accumulation_start
        self.min_acc_bars = max(3, int(0.8 * acc_hours * 60 / tf))   # works for any timeframe
        self.poc_bars = max(10, int(cfg.poc_lookback_hours * 60 / tf))
        self.df = add_indicators(df, cfg)
        d = self.df
        self.O, self.H, self.L, self.C = (d[x].to_numpy() for x in ("open", "high", "low", "close"))
        self.V = d["volume"].to_numpy()
        self.ATR = d["atr"].to_numpy()
        self.VS = d["vol_spike"].to_numpy()
        self.BULL, self.BEAR = d["bull_fvg"].to_numpy(), d["bear_fvg"].to_numpy()
        self.EB, self.ES = d["engulf_bull"].to_numpy(), d["engulf_bear"].to_numpy()
        self.SH, self.SL = d["sh_conf"].to_numpy(), d["sl_conf"].to_numpy()
        self.hours, self.dates, self.idx = d["hour"].to_numpy(), d["date"].to_numpy(), d.index
        self.BIAS = htf_bias(d, cfg)
        self._reset_day(None)
        return self

    def _reset_day(self, day):
        self.day = day
        self.acc_high, self.acc_low, self.acc_bars = -np.inf, np.inf, 0
        self.range_ok = None
        self.trades_today = 0
        self._reset_setup()

    def _reset_setup(self):
        self.probes = {}       # side -> dict  (price currently poking outside the range)
        self.sweep = None      # confirmed manipulation, waiting for displacement
        self.breakout = None   # confirmed breakout, waiting for retest

    # ------------------------------------------------------------ helpers
    def _poc(self, i):
        s = max(0, i - self.poc_bars + 1)
        return volume_profile(self.H[s:i + 1], self.L[s:i + 1], self.V[s:i + 1])

    def _levels(self, end, tol):
        """Support/resistance from swings CONFIRMED at or before bar `end`."""
        s = max(0, end - self.cfg.sr_lookback_bars)
        prices = np.concatenate([self.SH[s:end + 1], self.SL[s:end + 1]])
        return cluster_levels(prices, tol)

    def _pick_target(self, side, entry, stop, primary, levels):
        cfg = self.cfg
        risk = side * (entry - stop)
        if risk <= 0:
            return None
        cost = entry * (2 * cfg.fee_rate + 2 * cfg.slippage)      # worst case: market in, stop out
        if cfg.entry_mode == "limit":
            cost = entry * (cfg.maker_fee + cfg.fee_rate + cfg.slippage)
        if cost / risk > cfg.max_cost_r:     # stop too tight: costs would eat the edge
            return None

        def capped(t):
            rr = side * (t - entry) / risk
            return entry + side * min(rr, cfg.max_rr) * risk if rr >= cfg.min_rr else None

        t = capped(primary)
        if t is not None:
            return t
        # primary too close -> next resistance (long) / support (short) beyond it
        beyond = sorted((p for p, _ in levels if side * (p - primary) > 0), key=lambda p: side * p)
        for p in beyond:
            t = capped(p)
            if t is not None:
                return t
        return None

    # ------------------------------------------------------------ main loop
    def on_bar(self, i):
        """Call once per CLOSED bar, in order. Returns a Signal or None."""
        cfg = self.cfg
        day, hr = self.dates[i], self.hours[i]
        if day != self.day:
            self._reset_day(day)
        o, h, l, c, atr = self.O[i], self.H[i], self.L[i], self.C[i], self.ATR[i]

        # 1) ACCUMULATION
        if cfg.accumulation_start <= hr < cfg.accumulation_end:
            self.acc_high, self.acc_low = max(self.acc_high, h), min(self.acc_low, l)
            self.acc_bars += 1
            return None
        if hr < cfg.accumulation_end or hr >= cfg.session_close or self.acc_bars < self.min_acc_bars:
            return None
        if np.isnan(atr) or self.trades_today >= cfg.max_trades_per_day:
            return None
        if self.range_ok is None:
            height = self.acc_high - self.acc_low
            self.range_ok = cfg.min_range_atr * atr <= height <= cfg.max_range_atr * atr
        if not self.range_ok:
            return None

        # 3) DISTRIBUTION entries for already-confirmed setups
        if self.sweep:
            return self._sweep_entry(i)
        if self.breakout:
            return self._breakout_entry(i)

        # 2) MANIPULATION: watch both edges of the range
        vs = bool(self.VS[i])
        for side, level in ((+1, self.acc_low), (-1, self.acc_high)):
            # side=+1: lows get swept -> long idea ; side=-1: highs swept -> short idea
            ext = l if side == 1 else h
            p = self.probes.get(side)
            if p is None:
                if hr < cfg.manipulation_end and side * (level - ext) > 0:
                    p = self.probes[side] = dict(start=i, extreme=ext, outside=0, vol=vs)
                else:
                    continue
            else:
                if side * ext < side * p["extreme"]:
                    p["extreme"] = ext
                p["vol"] = p["vol"] or vs

            if side * (level - c) > 0:                      # closed outside the range
                p["outside"] += 1
                if cfg.enable_breakout_setup and p["outside"] >= 2 and p["vol"]:
                    self.breakout = dict(side=-side, level=level, start=i)
                    self.probes.clear()
                    return None
                if i - p["start"] > cfg.max_bars_after_sweep:
                    del self.probes[side]
            else:                                           # closed back inside = stop hunt
                self.sweep = dict(side=side, level=level, extreme=p["extreme"],
                                  start=p["start"], vol=p["vol"])
                self.probes.clear()
                return self._sweep_entry(i)
        return None

    def _bias_ok(self, i, side):
        return self.cfg.bias_mode == "none" or self.BIAS[i] == side

    def _bias_reason(self, side):
        if self.cfg.bias_mode == "none":
            return []
        return [f"{self.cfg.bias_timeframe} bias {'bullish' if side == 1 else 'bearish'} ({self.cfg.bias_mode})"]

    def _score(self, i, side, entry, swept_level, setup_vol, tol, end_for_levels):
        score, reasons = 0, []
        if setup_vol or self.VS[i]:
            score += 1
            reasons.append("volume spike")
        poc, val, vah = self._poc(i)
        if side * (poc - entry) > 0:
            score += 1
            reasons.append(f"{'discount below' if side == 1 else 'premium above'} POC {poc:.2f}")
        levels = self._levels(end_for_levels, tol)
        if any(n >= 2 and abs(p - swept_level) <= tol for p, n in levels):
            score += 1
            reasons.append("level was a prior S/R liquidity pool (equal highs/lows)")
        if (self.EB if side == 1 else self.ES)[i]:
            score += 1
            reasons.append("engulfing candle")
        return score, reasons, levels

    def _sweep_entry(self, i):
        cfg, s = self.cfg, self.sweep
        side = s["side"]
        h, l, c, atr = self.H[i], self.L[i], self.C[i], self.ATR[i]
        ext = l if side == 1 else h
        if side * ext < side * s["extreme"]:
            s["extreme"] = ext
        if side * (s["level"] - c) > 0 or i - s["start"] > cfg.max_bars_after_sweep:
            self.sweep = None                                # failed / too slow
            return None
        if not (self.BULL if side == 1 else self.BEAR)[i]:  # need displacement + FVG
            return None
        if not self._bias_ok(i, side):                     # trade only with the 1h direction
            return None

        if cfg.entry_mode == "limit":                      # rest an order in the middle of the gap
            if side == 1:
                entry, order = (self.H[i - 2] + l) / 2, "limit"
            else:
                entry, order = (self.L[i - 2] + h) / 2, "limit"
        else:
            entry, order = c, "market"
        stop = s["extreme"] - side * cfg.stop_buffer_atr * atr
        opposite = self.acc_high if side == 1 else self.acc_low
        tol = cfg.level_tolerance_atr * atr
        score, extra, levels = self._score(i, side, entry, s["level"], s["vol"], tol, s["start"])
        if score < cfg.min_confluence:
            return None
        target = self._pick_target(side, entry, stop, opposite, self._levels(i, tol))
        if target is None:
            return None
        self.sweep = None
        self.trades_today += 1
        reasons = [f"Asian range {self.acc_low:.2f}-{self.acc_high:.2f} (accumulation)",
                   f"liquidity sweep of Asian {'low' if side == 1 else 'high'} (manipulation)",
                   "displacement candle left a Fair Value Gap"] + self._bias_reason(side) + extra
        return Signal(i, self.idx[i], side, entry, stop, target, "AMD sweep", score, reasons, order)

    def _breakout_entry(self, i):
        cfg, b = self.cfg, self.breakout
        side, level = b["side"], b["level"]
        o, h, l, c, atr = self.O[i], self.H[i], self.L[i], self.C[i], self.ATR[i]
        if i - b["start"] > cfg.breakout_retest_bars or side * (c - level) < -0.5 * atr:
            self.breakout = None
            return None
        against = l if side == 1 else h
        retest = side * (against - level) <= cfg.retest_tolerance_atr * atr
        if not (retest and side * (c - level) > 0 and side * (c - o) > 0):
            return None
        if not self._bias_ok(i, side):
            return None

        entry = c
        stop = against - side * cfg.stop_buffer_atr * atr
        tol = cfg.level_tolerance_atr * atr
        score, extra, _ = self._score(i, side, entry, level, True, tol, b["start"])
        score -= 1          # volume was mandatory for the breakout, don't double count
        extra = [r for r in extra if r != "volume spike"]
        if score < cfg.min_confluence - 1:
            return None
        risk = side * (entry - stop)
        target = self._pick_target(side, entry, stop, entry + side * 2 * risk, self._levels(i, tol))
        if target is None:
            return None
        self.breakout = None
        self.trades_today += 1
        reasons = [f"broke Asian {'high' if side == 1 else 'low'} {level:.2f} with volume + 2 closes",
                   "retest of broken level held (resistance -> support flip)"] + self._bias_reason(side) + extra
        return Signal(i, self.idx[i], side, entry, stop, target, "Breakout retest", score, reasons)
