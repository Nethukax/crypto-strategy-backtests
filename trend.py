"""Trend-following test (Donchian breakout + ATR trailing stop).

The rules below were FIXED BEFORE looking at any results, using textbook
"Turtle"-style values. Nothing was tuned, so every coin is a fair test.
Don't edit FROZEN after seeing results and re-run; that turns it into curve-fitting.

RULES (4h candles):
  Long  when a candle CLOSES above the highest high of the previous 120 candles (20 days).
  Short when a candle CLOSES below the lowest low  of the previous 120 candles.
  Stop: 3 x ATR from entry, then trails 3 x ATR behind the best close since entry.
  Exit only when the trailing stop is hit. No target: let winners run.
  Risk 1% of equity per trade, max 3x leverage, one position per coin.

COSTS: taker fee both sides, slippage both sides, and perpetual funding of
0.01% per 8h charged on every position (conservative: shorts often receive it).

  python trend.py                         # 4 years, Bybit
  python trend.py --exchange binanceusdm
  python trend.py --synthetic             # offline test of the code
"""
import argparse
from dataclasses import replace

import numpy as np
import pandas as pd

from config import Config
from data import fetch_history, synthetic, TF_MIN

FROZEN = dict(timeframe="4h", breakout_bars=120, atr_bars=30, stop_atr=3.0,
              risk=0.01, max_leverage=3.0, allow_short=True, funding_per_8h=0.0001)
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "ADA", "NEAR", "TAO"]


def backtest(df, p, fee, slip, start_equity=1000.0):
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    atr = pd.Series(tr).ewm(alpha=1 / p["atr_bars"], adjust=False).mean().to_numpy()
    n = p["breakout_bars"]
    upper = df["high"].rolling(n).max().shift(1).to_numpy()     # previous n candles only
    lower = df["low"].rolling(n).min().shift(1).to_numpy()
    fund_per_bar = p["funding_per_8h"] * TF_MIN[p["timeframe"]] / 480
    k, times = p["stop_atr"], df.index

    eq, pos, trades, curve = start_equity, None, [], []

    def close(i, px, why):
        nonlocal eq, pos
        s, q, e = pos["side"], pos["qty"], pos["entry"]
        pnl = s * (px - e) * q - fee * (e + px) * q - pos["funding"]
        eq += pnl
        trades.append(dict(entry_time=pos["time"], exit_time=times[i], side=s, entry=e, exit=px,
                           bars=i - pos["i"], pnl=pnl, R=pnl / pos["risk_usd"], reason=why))
        pos = None

    for i in range(len(c)):
        if pos:
            s = pos["side"]
            pos["funding"] += fund_per_bar * pos["qty"] * c[i]
            if (l[i] <= pos["stop"]) if s == 1 else (h[i] >= pos["stop"]):
                px = min(o[i], pos["stop"]) if s == 1 else max(o[i], pos["stop"])   # gaps fill worse
                close(i, px * (1 - s * slip), "trailing stop")
            else:
                if s == 1:
                    pos["best"] = max(pos["best"], c[i])
                    pos["stop"] = max(pos["stop"], pos["best"] - k * atr[i])
                else:
                    pos["best"] = min(pos["best"], c[i])
                    pos["stop"] = min(pos["stop"], pos["best"] + k * atr[i])

        if pos is None and i >= n and not np.isnan(upper[i]):
            side = 1 if c[i] > upper[i] else (-1 if p["allow_short"] and c[i] < lower[i] else 0)
            if side:
                entry = c[i] * (1 + side * slip)
                stop = entry - side * k * atr[i]
                risk_px = abs(entry - stop)
                qty = min(eq * p["risk"] / risk_px, eq * p["max_leverage"] / entry)
                pos = dict(side=side, entry=entry, stop=stop, best=c[i], qty=qty, i=i, time=times[i],
                           risk_usd=qty * risk_px, funding=0.0)

        curve.append(eq + (pos["side"] * (c[i] - pos["entry"]) * pos["qty"] if pos else 0))

    if pos:
        close(len(c) - 1, c[-1] * (1 - pos["side"] * slip), "end of data (still open)")
        curve[-1] = eq
    return pd.DataFrame(trades), pd.Series(curve, index=times)


def summarize(t):
    if t.empty:
        return dict(trades=0, PF=np.nan, avgR=np.nan, win=np.nan, totalR=0.0)
    R = t.R
    loss = -R[R <= 0].sum()
    return dict(trades=len(R), PF=round(R[R > 0].sum() / loss, 2) if loss > 0 else np.inf,
                avgR=round(R.mean(), 2), win=round(100 * (R > 0).mean()), totalR=round(R.sum(), 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--exchange")
    ap.add_argument("--synthetic", action="store_true")
    args = ap.parse_args()

    base = replace(Config(), timeframe=FROZEN["timeframe"])
    if args.exchange:
        base.exchange = args.exchange

    rows, all_trades = [], []
    for k, coin in enumerate(COINS):
        cfg = replace(base, symbol=f"{coin}/USDT:USDT")
        try:
            df = (synthetic(args.days, cfg.timeframe, seed=200 + k) if args.synthetic
                  else fetch_history(cfg, args.days))
        except Exception as e:
            print(f"{coin}: skipped ({type(e).__name__}: {e})")
            continue
        t, curve = backtest(df, FROZEN, cfg.fee_rate, cfg.slippage)
        mid = df.index[len(df) // 2]
        if not t.empty:
            t["coin"] = coin
            t["half"] = np.where(t.entry_time < mid, 1, 2)
            all_trades.append(t)
        dd = (curve / curve.cummax() - 1).min()
        R = t.R if not t.empty else pd.Series(dtype=float)
        rows.append({"coin": coin, "from": df.index[0].date(), **summarize(t),
                     "longR": round(R[t.side == 1].sum(), 1) if len(R) else 0,
                     "shortR": round(R[t.side == -1].sum(), 1) if len(R) else 0,
                     "return%": round(100 * (curve.iloc[-1] / 1000 - 1)),
                     "maxDD%": round(100 * dd),
                     "hold%": round(100 * (df.close.iloc[-1] / df.close.iloc[0] - 1))})
        print(f"{coin}: done")

    table = pd.DataFrame(rows)
    T = pd.concat(all_trades) if all_trades else pd.DataFrame(columns=["R", "half", "side"])
    agg = summarize(T)
    profitable = int((table.PF > 1).sum())
    h1, h2 = T.R[T.half == 1].sum(), T.R[T.half == 2].sum()

    pd.set_option("display.width", 220)
    print("\nRules:", FROZEN, "\n")
    print(table.to_string(index=False))
    print("\n  return% = strategy at 1% risk per trade | hold% = just buying and holding the coin")
    print(f"\nALL COINS: {agg['trades']} trades, PF {agg['PF']}, avg {agg['avgR']}R, total {agg['totalR']}R, "
          f"profitable on {profitable}/{len(table)} coins")
    print(f"  longs {T.R[T.side == 1].sum():.1f}R | shorts {T.R[T.side == -1].sum():.1f}R | "
          f"first half {h1:.1f}R | second half {h2:.1f}R")
    table.to_csv("trend_results.csv", index=False)
    if len(T):
        T.to_csv("trend_trades.csv", index=False)

    print("\nVERDICT (criteria fixed in advance):")
    if agg["trades"] < 100:
        print("  Too few trades to judge.")
    elif agg["PF"] >= 1.3 and profitable >= 0.7 * len(table) and h1 > 0 and h2 > 0:
        print("  PASSES: profitable across most coins and in both halves of the period.")
        print("  Next step: build live execution for it and run on testnet.")
    elif agg["PF"] > 1.0:
        print("  Weakly positive but fails the bar (needs PF >= 1.3, 70% of coins, both halves positive).")
        print("  Not strong enough for real money.")
    else:
        print("  No edge. Don't trade it.")
    print("\nSaved trend_results.csv and trend_trades.csv")


if __name__ == "__main__":
    main()
