"""Untouched-data test: one FROZEN set of rules on coins that never influenced any setting.

Do not change FROZEN after seeing these results and re-run. That turns this into
more curve-fitting and the test stops meaning anything.

  python coins.py --days 730
  python coins.py --days 730 --exchange binanceusdm
"""
import argparse
from dataclasses import replace

import numpy as np
import pandas as pd

from config import Config
from data import fetch_history, synthetic
from backtest import run_backtest

FROZEN = dict(bias_mode="structure", entry_mode="market", min_confluence=0, enable_breakout_setup=False)
FRESH = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "ADA", "NEAR"]
REFERENCE = ["TAO"]   # already used for tuning, shown for comparison only


def summarize(R):
    if len(R) == 0:
        return dict(trades=0, PF=np.nan, avgR=np.nan, win=np.nan, totalR=0.0)
    loss = -R[R <= 0].sum()
    return dict(trades=len(R), PF=round(R[R > 0].sum() / loss, 2) if loss > 0 else np.inf,
                avgR=round(R.mean(), 2), win=round(100 * (R > 0).mean()), totalR=round(R.sum(), 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--exchange")
    ap.add_argument("--synthetic", action="store_true")
    args = ap.parse_args()

    base = replace(Config(), **FROZEN)
    if args.exchange:
        base.exchange = args.exchange

    rows, fresh_R = [], []
    for k, coin in enumerate(FRESH + REFERENCE):
        cfg = replace(base, symbol=f"{coin}/USDT:USDT")
        try:
            df = synthetic(args.days, cfg.timeframe, seed=100 + k) if args.synthetic else fetch_history(cfg, args.days)
        except Exception as e:
            print(f"{coin}: skipped ({type(e).__name__}: {e})")
            continue
        trades, _ = run_backtest(df, cfg)
        R = trades.R if not trades.empty else pd.Series(dtype=float)
        if coin in FRESH:
            fresh_R.append(R)
        rows.append({"coin": coin + (" (tuned on)" if coin in REFERENCE else ""), "from": df.index[0].date(),
                     **summarize(R)})
        print(f"{coin}: done")

    table = pd.DataFrame(rows)
    allR = pd.concat(fresh_R) if fresh_R else pd.Series(dtype=float)
    agg = summarize(allR)
    fresh_rows = table[~table.coin.str.contains("tuned")]
    profitable = int((fresh_rows.PF > 1).sum())

    pd.set_option("display.width", 200)
    print("\nRules:", FROZEN, "| 15m trades, 1h bias\n")
    print(table.to_string(index=False))
    print(f"\nALL FRESH COINS: {agg['trades']} trades, PF {agg['PF']}, avg {agg['avgR']}R, "
          f"total {agg['totalR']}R, profitable on {profitable}/{len(fresh_rows)} coins")
    table.to_csv("coins_results.csv", index=False)

    print("\nVERDICT:")
    if agg["trades"] < 150:
        print("  Too few trades to judge.")
    elif agg["PF"] >= 1.2 and profitable >= 0.7 * len(fresh_rows):
        print("  Profitable on most coins it was never tuned on. Worth paper trading on testnet.")
    elif agg["PF"] > 1.0:
        print("  Slightly positive but weak. Fees and slippage live will likely erase it. Not worth real money.")
    else:
        print("  Loses on coins it was never tuned on. These rules have no edge. Don't trade them.")
    print("\nSaved coins_results.csv")


if __name__ == "__main__":
    main()
