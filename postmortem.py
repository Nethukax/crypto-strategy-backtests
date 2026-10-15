"""Loss post-mortem for the AMD strategy.

For every trade it records what the bot EXPECTED (setup, reasons, stop, target)
and what HAPPENED (best and worst point during the trade, how fast it lost,
what price did after the exit). Losses are grouped into causes, winners and
losers are compared feature by feature, and candidate fixes are checked on
ALL trades, not just the losers. A fix that rescues losers usually hurts winners.

This is analysis only. It never changes the strategy. Any idea it suggests
has to pass sweep.py -> coins.py -> testnet like everything else.

  python postmortem.py                    # fresh coins, 2 years (uses cached data)
  python postmortem.py --coins TAO
  python postmortem.py --synthetic
"""
import argparse
from dataclasses import replace

import numpy as np
import pandas as pd

from backtest import run_backtest
from coins import FRESH, FROZEN
from concepts import htf_bias
from config import Config
from data import fetch_history, synthetic

CAUSES = {
    "almost_won": "Got 75%+ of the way to target, then reversed to the stop",
    "right_idea_tight_stop": "Stopped out, then price hit the target later that session",
    "instant_fail": "Went straight against the trade (stopped within 2 candles, never +0.3R)",
    "out_of_time": "Session close hit while the trade was still losing",
    "gap": "Price jumped over the stop",
    "wrong_direction": "Stopped out and never reached the target afterwards",
}


def session_end(df, ei, cfg):
    """Index of the bar where the trade's session closes (the backtest exits at its open)."""
    day, hours, dates = df.index[ei].date(), df.index.hour, df.index.date
    for j in range(ei + 1, len(df)):
        if dates[j] != day or hours[j] >= cfg.session_close:
            return j
    return len(df) - 1


def simulate(H, L, O, side, entry, stop, target, start, end):
    """Replay a trade bar by bar (stop checked first, like the backtester). Returns R."""
    risk = abs(entry - stop)
    for j in range(start, end):
        if (L[j] <= stop) if side == 1 else (H[j] >= stop):
            return -1.0
        if (H[j] >= target) if side == 1 else (L[j] <= target):
            return side * (target - entry) / risk
    return side * (O[end] - entry) / risk


def analyze_coin(coin, df, cfg):
    trades, _ = run_backtest(df, cfg)
    if trades.empty:
        return pd.DataFrame()
    H, L, O = (df[x].to_numpy() for x in ("high", "low", "open"))
    bias = htf_bias(df, cfg)
    rows = []
    for t in trades.itertuples():
        ei, xi = df.index.get_loc(t.entry_time), df.index.get_loc(t.exit_time)
        s, risk = t.side, abs(t.entry - t.stop)
        se = session_end(df, ei, cfg)
        path = slice(ei + 1, xi + 1)
        if xi > ei:
            mfe = (H[path].max() - t.entry if s == 1 else t.entry - L[path].min()) / risk
            mae = (t.entry - L[path].min() if s == 1 else H[path].max() - t.entry) / risk
        else:
            mfe, mae = 0.0, 1.0
        target_r = s * (t.target - t.entry) / risk
        after = slice(xi + 1, se + 1)
        hit_after = bool(xi < se and ((H[after] >= t.target).any() if s == 1 else (L[after] <= t.target).any()))

        if t.R > 0:
            cause = "win"
        elif t.exit_reason == "stop (gap)":
            cause = "gap"
        elif t.exit_reason == "session close":
            cause = "out_of_time"
        elif mfe >= 0.75 * target_r:
            cause = "almost_won"
        elif hit_after:
            cause = "right_idea_tight_stop"
        elif xi - ei <= 2 and mfe < 0.3:
            cause = "instant_fail"
        else:
            cause = "wrong_direction"

        cf = {f"R_stop_x{m}": simulate(H, L, O, s, t.entry, t.entry - s * m * risk, t.target, ei + 1, se)
              for m in (1, 1.5, 2)}
        cf["R_target_x0.5"] = simulate(H, L, O, s, t.entry, t.stop, t.entry + s * 0.5 * target_r * risk, ei + 1, se)
        reasons = t.reasons.lower()
        rows.append(dict(
            coin=coin, entry_time=t.entry_time, side="LONG" if s == 1 else "SHORT", setup=t.setup,
            entry=t.entry, stop=t.stop, target=t.target, planned_R=round(target_r, 2), R=t.R,
            exit_reason=t.exit_reason, bars=xi - ei, MFE_R=round(mfe, 2), MAE_R=round(mae, 2),
            target_hit_after_exit=hit_after, bias_flipped=bool(bias[xi] != s), cause=cause,
            hour=t.entry_time.hour, score=t.score,
            f_volume="volume spike" in reasons, f_poc="poc" in reasons,
            f_pool="liquidity pool" in reasons, f_engulf="engulfing" in reasons,
            reasons=t.reasons, **cf))
    return pd.DataFrame(rows)


def narrative(r, df_coin):
    """Plain-English 'expected vs happened' for one trade."""
    fmt = lambda x: f"{x:,.2f}" if abs(x) >= 1 else f"{x:.5g}"
    exp = (f"EXPECTED: {r.side} at {fmt(r.entry)}, stop {fmt(r.stop)}, target {fmt(r.target)} (+{r.planned_R}R).\n"
           f"      Why: {r.reasons}")
    hap = (f"HAPPENED: best point {r.MFE_R:+.2f}R, worst {-r.MAE_R:+.2f}R, exit after {r.bars} candle(s) "
           f"({r.exit_reason}) = {r.R:+.2f}R.")
    if r.target_hit_after_exit:
        hap += " Price reached the target later that session."
    if r.bias_flipped:
        hap += " The 1h direction had flipped by the exit."
    return f"  {r.entry_time:%Y-%m-%d %H:%M} {r.coin} [{CAUSES.get(r.cause, r.cause)}]\n      {exp}\n      {hap}"


def compare(trades, col, label, overall):
    """Avg R by group, flagged only if the difference is bigger than random noise would give."""
    lines = []
    for val, g in trades.groupby(col, observed=True):
        if len(g) < 15:
            continue
        se = g.R.std(ddof=1) / np.sqrt(len(g))
        diff = g.R.mean() - overall
        flag = "  <- maybe real, test on new data" if abs(diff) > 2 * se else ""
        lines.append(f"    {label + '=' + str(val):<18} {len(g):>4} trades  win {100 * (g.R > 0).mean():>3.0f}%  "
                     f"avg {g.R.mean():+.2f}R{flag}")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--coins", nargs="*", default=FRESH)
    ap.add_argument("--exchange")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--examples", type=int, default=6)
    args = ap.parse_args()

    cfg = replace(Config(), **FROZEN)
    if args.exchange:
        cfg.exchange = args.exchange
    frames, data = [], {}
    for k, coin in enumerate(args.coins):
        c = replace(cfg, symbol=f"{coin}/USDT:USDT")
        try:
            df = (synthetic(args.days, c.timeframe, seed=100 + k) if args.synthetic
                  else fetch_history(c, args.days, max_age_hours=24 * 60))
        except Exception as e:
            print(f"{coin}: skipped ({type(e).__name__}: {e})")
            continue
        data[coin] = df
        frames.append(analyze_coin(coin, df, c))
        print(f"{coin}: analyzed")
    T = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    if T.empty:
        print("No trades.")
        return
    T.to_csv("postmortem_trades.csv", index=False)
    losses = T[T.R <= 0]
    overall = T.R.mean()

    print(f"\n{len(T)} trades | {len(T) - len(losses)} wins, {len(losses)} losses | "
          f"win rate {100 * (T.R > 0).mean():.0f}% | avg {overall:+.2f}R | total {T.R.sum():+.1f}R")

    print("\nWHY THE LOSSES HAPPENED")
    for cause, n in losses.cause.value_counts().items():
        print(f"  {100 * n / len(losses):4.0f}%  ({n:>3})  {CAUSES[cause]}")
    print(f"\n  Average loser: best point {losses.MFE_R.mean():+.2f}R before losing | "
          f"average winner: worst point {-T[T.R > 0].MAE_R.mean():+.2f}R before winning")
    print(f"  1h direction had flipped by the exit in {100 * losses.bias_flipped.mean():.0f}% of losses "
          f"vs {100 * T[T.R > 0].bias_flipped.mean():.0f}% of wins")

    print("\nWOULD A FIX HAVE HELPED? (replayed on ALL trades, before fees)")
    base = T["R_stop_x1"].mean()
    tests = [("Current rules (replayed)", "R_stop_x1"), ("Stop 1.5x wider", "R_stop_x1.5"),
             ("Stop 2x wider", "R_stop_x2"), ("Target halfway", "R_target_x0.5")]
    for name, col in tests:
        d = T[col].mean() - base
        print(f"  {name:<26} avg {T[col].mean():+.3f}R   win {100 * (T[col] > 0).mean():3.0f}%"
              + ("" if col == "R_stop_x1" else f"   ({d:+.3f}R vs current)"))
    print("  Position size shrinks with a wider stop, so $ risked per trade stays the same and the R numbers")
    print("  compare directly. Real fees take roughly 0.1-0.2R more per trade (a bit less with wider stops).")

    print("\nWINNERS vs LOSERS: does any feature matter?")
    print(f"  (overall avg {overall:+.2f}R; with ~20 groups, about 1 gets flagged by pure chance)")
    T["hour_band"] = pd.cut(T.hour, [0, 9, 12, 24], right=False, labels=["7-9", "9-12", "12-21"])
    T["planned_band"] = pd.cut(T.planned_R, [0, 2, 3, 99], right=False, labels=["<2R", "2-3R", "3R+"])
    for col, label in [("side", "side"), ("hour_band", "UTC hour"), ("planned_band", "target"),
                       ("score", "score"), ("f_volume", "volume"), ("f_poc", "POC"),
                       ("f_pool", "liq pool"), ("f_engulf", "engulf")]:
        for line in compare(T, col, label, overall):
            print(line)

    print("\nEXAMPLE LOSSES: what it expected vs what happened")
    picks = losses.drop_duplicates("cause").head(args.examples)
    for r in picks.itertuples():
        print(narrative(r, data[r.coin]))
    print("\nSaved postmortem_trades.csv (every trade with all of the above)")


if __name__ == "__main__":
    main()
