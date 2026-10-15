"""Portfolio test: the trend rules from trend.py on ALL coins in ONE account.

Same frozen rules, same costs. Each new trade risks 1% of the account's CURRENT
value (including open profit/loss). Positions on different coins can be open at
the same time, so losses can stack when crypto falls together. That's the point
of this test.

PASS/FAIL, decided before running:
  FAIL  if the worst drawdown is -40% or deeper   (too painful to actually stick with)
  PASS  if drawdown is shallower than -30% AND yearly return / |drawdown| >= 0.5
  else  BORDERLINE

  python portfolio.py                 # uses the 4h data trend.py already downloaded
  python portfolio.py --synthetic
"""
import argparse
from dataclasses import replace

import numpy as np
import pandas as pd

from config import Config
from data import fetch_history, synthetic, TF_MIN
from trend import FROZEN, COINS


def prepare(df, p):
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    n = p["breakout_bars"]
    return dict(o=o, h=h, l=l, c=c, idx={t: i for i, t in enumerate(df.index)},
                atr=pd.Series(tr).ewm(alpha=1 / p["atr_bars"], adjust=False).mean().to_numpy(),
                upper=df["high"].rolling(n).max().shift(1).to_numpy(),
                lower=df["low"].rolling(n).min().shift(1).to_numpy())


def run_portfolio(data, p, fee, slip, start=1000.0):
    times = sorted(set().union(*(d.index for d in data.values())))
    A = {coin: prepare(df, p) for coin, df in data.items()}
    fund = p["funding_per_8h"] * TF_MIN[p["timeframe"]] / 480
    k, n = p["stop_atr"], p["breakout_bars"]
    cash, pos, last = start, {}, {}
    trades, curve, exposure, n_open = [], [], [], []

    def mtm():
        return cash + sum(q["side"] * (last[cn] - q["entry"]) * q["qty"] for cn, q in pos.items())

    for t in times:
        bars = {cn: A[cn]["idx"][t] for cn in A if t in A[cn]["idx"]}
        # 1) manage open positions
        for cn, i in bars.items():
            a = A[cn]
            last[cn] = a["c"][i]
            q = pos.get(cn)
            if not q:
                continue
            s = q["side"]
            q["funding"] += fund * q["qty"] * a["c"][i]
            if (a["l"][i] <= q["stop"]) if s == 1 else (a["h"][i] >= q["stop"]):
                px = (min(a["o"][i], q["stop"]) if s == 1 else max(a["o"][i], q["stop"])) * (1 - s * slip)
                pnl = s * (px - q["entry"]) * q["qty"] - fee * (q["entry"] + px) * q["qty"] - q["funding"]
                cash += pnl
                trades.append(dict(coin=cn, side=s, entry_time=q["time"], exit_time=t, pnl=pnl,
                                   R=pnl / q["risk_usd"]))
                del pos[cn]
            elif s == 1:
                q["best"] = max(q["best"], a["c"][i])
                q["stop"] = max(q["stop"], q["best"] - k * a["atr"][i])
            else:
                q["best"] = min(q["best"], a["c"][i])
                q["stop"] = min(q["stop"], q["best"] + k * a["atr"][i])
        # 2) new entries, sized on current account value
        for cn, i in bars.items():
            a = A[cn]
            if cn in pos or i < n or np.isnan(a["upper"][i]):
                continue
            c = a["c"][i]
            side = 1 if c > a["upper"][i] else (-1 if p["allow_short"] and c < a["lower"][i] else 0)
            if not side:
                continue
            eq = mtm()
            if eq <= 0:
                continue
            entry = c * (1 + side * slip)
            stop = entry - side * k * a["atr"][i]
            qty = min(eq * p["risk"] / abs(entry - stop), eq * p["max_leverage"] / entry)
            pos[cn] = dict(side=side, entry=entry, stop=stop, best=c, qty=qty, time=t,
                           risk_usd=qty * abs(entry - stop), funding=0.0)
        eq = mtm()
        curve.append(eq)
        exposure.append(sum(q["qty"] * last[cn] for cn, q in pos.items()) / eq if eq > 0 else np.nan)
        n_open.append(len(pos))

    idx = pd.DatetimeIndex(times)
    return (pd.DataFrame(trades), pd.Series(curve, index=idx),
            pd.Series(exposure, index=idx), pd.Series(n_open, index=idx))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--exchange")
    ap.add_argument("--synthetic", action="store_true")
    args = ap.parse_args()

    base = replace(Config(), timeframe=FROZEN["timeframe"])
    if args.exchange:
        base.exchange = args.exchange
    data = {}
    for k, coin in enumerate(COINS):
        cfg = replace(base, symbol=f"{coin}/USDT:USDT")
        try:
            data[coin] = (synthetic(args.days, cfg.timeframe, seed=200 + k) if args.synthetic
                          else fetch_history(cfg, args.days, max_age_hours=24 * 30))
        except Exception as e:
            print(f"{coin}: skipped ({type(e).__name__}: {e})")
    print(f"Coins: {', '.join(data)}")

    trades, eq, expo, nopen = run_portfolio(data, FROZEN, base.fee_rate, base.slippage)
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    dd = eq / eq.cummax() - 1
    max_dd = dd.min()
    trough = dd.idxmin()
    peak = eq[:trough].idxmax()
    rec = eq[trough:][eq[trough:] >= eq[peak]]
    recovered = rec.index[0] if len(rec) else None
    under = (dd < 0).astype(int)
    longest = (under.groupby((under == 0).cumsum()).sum().max()) * TF_MIN[FROZEN["timeframe"]] / 1440
    monthly = eq.resample("ME").last().pct_change().dropna()
    yearly = eq.resample("YE").last()
    yearly = pd.concat([pd.Series([eq.iloc[0]], index=[eq.index[0]]), yearly]).pct_change().dropna()

    # buy & hold benchmarks over the same period
    closes = pd.DataFrame({cn: df["close"] for cn, df in data.items()}).ffill()
    first = closes.iloc[0].dropna()
    ew = (closes[first.index] / first).mean(axis=1)
    bh_dd = lambda s: (s / s.cummax() - 1).min()

    print(f"\nPORTFOLIO ({len(data)} coins, {FROZEN['risk']:.0%} risk per trade, {years:.1f} years, {len(trades)} trades)")
    print(f"  Total return        {total:+.0%}   ($1000 -> ${eq.iloc[-1]:,.0f})")
    print(f"  Per year (CAGR)     {cagr:+.1%}")
    print(f"  Worst drawdown      {max_dd:.1%}   peak {peak.date()} -> bottom {trough.date()}, "
          f"{'recovered ' + str(recovered.date()) if recovered is not None else 'NOT recovered yet'}")
    print(f"  Longest time below a previous high: {longest:.0f} days")
    print(f"  Worst month {monthly.min():+.1%}  | best month {monthly.max():+.1%}  | "
          f"losing months {(monthly < 0).mean():.0%}")
    print(f"  Open positions: avg {nopen.mean():.1f}, max {nopen.max()}  | "
          f"position size vs account: avg {expo.mean():.1f}x, max {expo.max():.1f}x")
    print("  By year: " + "  ".join(f"{d.year} {r:+.0%}" for d, r in yearly.items()))
    print(f"\nBUY & HOLD, same period:")
    print(f"  Equal-weight basket    {ew.iloc[-1] - 1:+.0%}  (worst drawdown {bh_dd(ew):.0%})  "
          f"[coins with data from the start: {', '.join(first.index)}]")
    if "BTC" in closes:
        b = closes["BTC"].dropna()
        print(f"  BTC only               {b.iloc[-1] / b.iloc[0] - 1:+.0%}  (worst drawdown {bh_dd(b):.0%})")

    ratio = cagr / abs(max_dd) if max_dd < 0 else np.inf
    print(f"\nVERDICT (criteria fixed in advance): CAGR / |drawdown| = {ratio:.2f}")
    if max_dd <= -0.40:
        print("  FAIL: drawdown of 40% or more. Not worth forward testing.")
    elif max_dd > -0.30 and ratio >= 0.5:
        print("  PASS: worth a forward test on testnet.")
    else:
        print("  BORDERLINE: survivable drawdown but weak return for the pain.")

    eq.to_csv("portfolio_equity.csv")
    trades.to_csv("portfolio_trades.csv", index=False)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
        a1.plot(eq.index, eq / eq.iloc[0], label="Trend portfolio", color="#2a6f97")
        a1.plot(ew.index, ew, label="Buy & hold (equal weight)", color="#999999", lw=1)
        a1.set_yscale("log")
        a1.set_ylabel("Growth of $1 (log)")
        a1.legend()
        a1.grid(alpha=0.3)
        a2.fill_between(dd.index, dd * 100, 0, color="#c0392b", alpha=0.5)
        a2.set_ylabel("Drawdown %")
        a2.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig("portfolio.png", dpi=120)
        print("\nSaved portfolio.png, portfolio_equity.csv, portfolio_trades.csv")
    except ImportError:
        print("\nSaved portfolio_equity.csv, portfolio_trades.csv")


if __name__ == "__main__":
    main()
