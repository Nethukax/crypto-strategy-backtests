"""Command line entry point.

  python run.py backtest --days 365          # real exchange history
  python run.py backtest --synthetic         # offline smoke test with fake data
  python run.py signals --days 30            # list recent setups with their reasons
  python run.py live                         # testnet by default (see live.py)
"""
import argparse
import logging

from config import Config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backtest", "signals", "live"])
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--symbol")
    ap.add_argument("--exchange")
    ap.add_argument("--timeframe")
    args = ap.parse_args()

    cfg = Config()
    if args.symbol:
        cfg.symbol = args.symbol
    if args.exchange:
        cfg.exchange = args.exchange
    if args.timeframe:
        cfg.timeframe = args.timeframe

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")])

    if args.mode == "live":
        from live import LiveTrader
        LiveTrader(cfg).run()
        return

    from data import fetch_history, synthetic
    df = synthetic(args.days, cfg.timeframe) if args.synthetic else fetch_history(cfg, args.days)
    print(f"Loaded {len(df)} candles {df.index[0]} -> {df.index[-1]}")

    if args.mode == "signals":
        from strategy import AMDStrategy
        s = AMDStrategy(cfg).prepare(df)
        for i in range(len(df)):
            sig = s.on_bar(i)
            if sig:
                print(f"\n{sig.time}  {sig.setup}  {'LONG' if sig.side == 1 else 'SHORT'}  "
                      f"entry {sig.entry:.2f} stop {sig.stop:.2f} target {sig.target:.2f} RR {sig.rr:.2f}")
                for r in sig.reasons:
                    print("   -", r)
        return

    from backtest import run_backtest, stats, save_report
    trades, curve = run_backtest(df, cfg)
    for k, v in stats(trades, curve, cfg).items():
        print(f"{k:>34}: {v}")
    save_report(trades, curve)
    print("\nSaved backtest_trades.csv and backtest_equity.png")


if __name__ == "__main__":
    main()
