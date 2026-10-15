"""Bar-by-bar backtester. Uses the exact same AMDStrategy.on_bar() as the live bot.

Conservative assumptions:
  - market entries fill at the signal candle's close + slippage
  - limit entries fill only if price actually trades back to the order price;
    if filled and stopped in the same candle, it counts as a loss
  - if stop and target are both inside one candle, the STOP is assumed hit first
  - gaps through the stop fill at the open
  - fees on both sides, forced exit at session close
"""
import numpy as np
import pandas as pd

from strategy import AMDStrategy


def run_backtest(df, cfg):
    strat = AMDStrategy(cfg).prepare(df)
    O, H, L = strat.O, strat.H, strat.L
    hours, dates, times = strat.hours, strat.dates, strat.idx
    slip = cfg.slippage

    equity = cfg.starting_equity
    pos, pending, trades, curve = None, None, [], []
    day, day_start, halted = None, equity, False
    run_backtest.missed = 0

    def open_pos(sig, fill, fee, i):
        risk_px = abs(fill - sig.stop)
        if risk_px <= 0 or equity <= 0:
            return None
        qty = min(equity * cfg.risk_per_trade / risk_px, equity * cfg.max_leverage / fill)
        return dict(entry_time=times[i], setup=sig.setup, order=sig.order, side=sig.side, entry=fill,
                    stop=sig.stop, target=sig.target, qty=qty, score=sig.score, entry_fee=fee,
                    risk_usd=qty * risk_px, reasons=" | ".join(sig.reasons), date=dates[i], opened=i)

    def close_pos(i, px, why):
        nonlocal equity, pos, halted
        s, q, e = pos["side"], pos["qty"], pos["entry"]
        if why == "target":
            exit_fee = cfg.maker_fee                  # take-profit is a resting limit order
        else:
            exit_fee, px = cfg.fee_rate, px * (1 - s * slip)
        pnl = s * (px - e) * q - pos["entry_fee"] * e * q - exit_fee * px * q
        equity += pnl
        trades.append({**{k: v for k, v in pos.items() if k not in ("date", "opened", "entry_fee")},
                       "exit_time": times[i], "exit": px, "exit_reason": why,
                       "pnl": pnl, "R": pnl / pos["risk_usd"], "equity": equity})
        pos = None
        if (equity - day_start) / day_start <= -cfg.max_daily_loss:
            halted = True

    for i in range(len(O)):
        if dates[i] != day:
            day, day_start, halted = dates[i], equity, False

        # ---- resting limit order waiting to fill
        if pending:
            sig, s = pending["sig"], pending["sig"].side
            if i > pending["expires"] or hours[i] >= cfg.session_close or dates[i] != pending["date"] or halted:
                pending = None
                run_backtest.missed += 1
            elif (L[i] <= sig.entry) if s == 1 else (H[i] >= sig.entry):
                fill = min(O[i], sig.entry) if s == 1 else max(O[i], sig.entry)
                pos, pending = open_pos(sig, fill, cfg.maker_fee, i), None
                if pos and ((L[i] <= pos["stop"]) if s == 1 else (H[i] >= pos["stop"])):
                    close_pos(i, pos["stop"], "stop")       # filled and stopped in the same candle
            elif (H[i] >= sig.target) if s == 1 else (L[i] <= sig.target):
                pending = None                              # price left without us
                run_backtest.missed += 1

        # ---- manage open position (from the candle after it opened)
        if pos and pos["opened"] < i:
            s = pos["side"]
            if (O[i] <= pos["stop"]) if s == 1 else (O[i] >= pos["stop"]):
                close_pos(i, O[i], "stop (gap)")
            elif (L[i] <= pos["stop"]) if s == 1 else (H[i] >= pos["stop"]):
                close_pos(i, pos["stop"], "stop")
            elif (H[i] >= pos["target"]) if s == 1 else (L[i] <= pos["target"]):
                close_pos(i, pos["target"], "target")
            elif hours[i] >= cfg.session_close or dates[i] != pos["date"]:
                close_pos(i, O[i], "session close")

        # ---- new signals
        sig = strat.on_bar(i)
        if sig and pos is None and pending is None and not halted:
            if sig.order == "limit":
                pending = dict(sig=sig, expires=i + cfg.limit_wait_bars, date=dates[i])
            else:
                pos = open_pos(sig, sig.entry * (1 + sig.side * slip), cfg.fee_rate, i)
        curve.append(equity)

    return pd.DataFrame(trades), pd.Series(curve, index=times, name="equity")


def stats(trades, curve, cfg):
    if trades.empty:
        return {"trades": 0}
    wins = trades[trades.pnl > 0]
    losses = trades[trades.pnl <= 0]
    dd = (curve / curve.cummax() - 1).min()
    pf = wins.pnl.sum() / abs(losses.pnl.sum()) if len(losses) and losses.pnl.sum() else np.inf
    out = {
        "trades": len(trades),
        "win_rate_%": round(100 * len(wins) / len(trades), 1),
        "profit_factor": round(pf, 2),
        "avg_R": round(trades.R.mean(), 2),
        "total_return_%": round(100 * (curve.iloc[-1] / cfg.starting_equity - 1), 1),
        "max_drawdown_%": round(100 * dd, 1),
        "final_equity": round(curve.iloc[-1], 2),
        "limit orders not filled": getattr(run_backtest, "missed", 0),
    }
    for name, g in trades.groupby("setup"):
        out[f"{name}: trades / win% / avg R"] = (
            f"{len(g)} / {100 * (g.pnl > 0).mean():.0f}% / {g.R.mean():.2f}")
    return out


def save_report(trades, curve, out_prefix="backtest"):
    trades.to_csv(f"{out_prefix}_trades.csv", index=False)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 4))
        curve.plot(ax=ax, color="#2a6f97")
        ax.set_title("Equity curve")
        ax.set_ylabel("USDT")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(f"{out_prefix}_equity.png", dpi=120)
    except ImportError:
        pass
