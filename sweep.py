"""Test many strategy variants honestly.

Each variant is run on several coins. Data is split in time:
  IN-SAMPLE  (first 70%)  = where you choose settings
  OUT-OF-SAMPLE (last 30%) = the test. The bot never "saw" this period while you chose.

A variant is only worth trading if it is profitable in BOTH, with enough trades.
Picking the best in-sample row and ignoring out-of-sample is how people fool themselves.

  python sweep.py --days 730
  python sweep.py --days 730 --exchange binanceusdm
  python sweep.py --synthetic           # offline test of the script itself
"""
import argparse
import itertools
from dataclasses import replace

import numpy as np
import pandas as pd

from config import Config
from data import fetch_history, synthetic
from backtest import run_backtest

SYMBOLS = ["TAO/USDT:USDT"]
GRID = {                               # trades on 15m, direction from 1h
    "bias_mode": ["structure", "ema", "none"],
    "entry_mode": ["limit", "market"],
    "min_confluence": [0, 1, 2],
    "enable_breakout_setup": [True, False],
}


def r_stats(R):
    if len(R) == 0:
        return 0, np.nan, np.nan, np.nan
    win, loss = R[R > 0].sum(), -R[R <= 0].sum()
    pf = win / loss if loss > 0 else np.inf
    return len(R), round(pf, 2), round(R.mean(), 2), round(100 * (R > 0).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--exchange")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--split", type=float, default=0.7)
    args = ap.parse_args()

    base = Config()
    if args.exchange:
        base.exchange = args.exchange

    # download each symbol/timeframe once
    data = {}
    for tf in [base.timeframe]:
        for k, sym in enumerate(SYMBOLS):
            cfg = replace(base, symbol=sym, timeframe=tf)
            try:
                df = synthetic(args.days, tf, seed=k) if args.synthetic else fetch_history(cfg, args.days)
            except Exception as e:
                print(f"skip {sym} {tf}: {e}")
                continue
            cut = df.index[int(len(df) * args.split)]
            data[(sym, tf)] = (df[df.index < cut], df[df.index >= cut])
            print(f"loaded {sym} {tf}: {len(df)} candles, out-of-sample from {cut.date()}")

    keys = list(GRID)
    rows = []
    for combo in itertools.product(*GRID.values()):
        params = dict(zip(keys, combo))
        R_in, R_out = [], []
        for (sym, tf), (dfi, dfo) in data.items():
            cfg = replace(base, symbol=sym, **params)
            for part, bucket in ((dfi, R_in), (dfo, R_out)):
                t, _ = run_backtest(part, cfg)
                if not t.empty:
                    bucket.append(t.R)
        R_in = pd.concat(R_in) if R_in else pd.Series(dtype=float)
        R_out = pd.concat(R_out) if R_out else pd.Series(dtype=float)
        n_i, pf_i, ar_i, wr_i = r_stats(R_in)
        n_o, pf_o, ar_o, wr_o = r_stats(R_out)
        rows.append({**params, "IS trades": n_i, "IS PF": pf_i, "IS avgR": ar_i,
                     "OOS trades": n_o, "OOS PF": pf_o, "OOS avgR": ar_o, "OOS win%": wr_o})
        print(".", end="", flush=True)

    res = pd.DataFrame(rows).sort_values("IS PF", ascending=False)
    res.to_csv("sweep_results.csv", index=False)
    pd.set_option("display.width", 200)
    print("\n\n", res.to_string(index=False))

    ok = res[(res["IS PF"] > 1.2) & (res["OOS PF"] > 1.2) & (res["IS trades"] >= 60) & (res["OOS trades"] >= 25)]
    print("\nVERDICT:")
    if ok.empty:
        print("  No variant was profitable both in-sample and out-of-sample with enough trades.")
        print("  => No evidence of an edge. Do NOT trade this with real money.")
    else:
        print(f"  {len(ok)} variant(s) passed. Best candidate (by out-of-sample PF):")
        print("  ", ok.sort_values("OOS PF", ascending=False).iloc[0].to_dict())
        print("  Next: run it on testnet for 3-4 weeks before any real money.")
    print("\nSaved sweep_results.csv")


if __name__ == "__main__":
    main()
