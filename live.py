"""Live / paper trader.

SAFETY:
  - Runs on the exchange TESTNET unless cfg.live = True AND the environment
    variable I_ACCEPT_REAL_MONEY_RISK=YES is set. Both are required.
  - Checks every 15 seconds: any open position without a stop-loss gets one
    immediately; if that fails, the position is closed.
  - Daily loss limit, max trades/day, max leverage, flat at session close.
  - Don't trade the same symbol by hand on the same account: the bot will treat
    an unknown position as unprotected and close it.
  - API keys come from environment variables only. Never give the key
    withdrawal permission.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

from data import make_exchange, to_frame, TF_MIN
from strategy import AMDStrategy

STATE_FILE = "bot_state.json"
FAST_POLL_SECONDS = 15
log = logging.getLogger("amd_bot")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class LiveTrader:
    def __init__(self, cfg, exchange=None):
        self.cfg = cfg
        real = cfg.live and os.getenv("I_ACCEPT_REAL_MONEY_RISK") == "YES"
        if cfg.live and not real:
            raise SystemExit("cfg.live=True but I_ACCEPT_REAL_MONEY_RISK=YES is not set. Refusing to trade real money.")
        self.real = real
        self.ex = exchange or make_exchange(cfg, auth=True, sandbox=not real)
        self.ex.load_markets()
        self.state = load_state()
        self.tf_ms = TF_MIN[cfg.timeframe] * 60_000
        log.warning("MODE: %s | %s %s (bias %s %s) on %s", "REAL MONEY" if real else "TESTNET",
                    cfg.symbol, cfg.timeframe, cfg.bias_timeframe, cfg.bias_mode, cfg.exchange)

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    # ------------------------------------------------------------ exchange helpers
    def equity(self):
        return float(self.ex.fetch_balance()["total"].get("USDT", 0) or 0)

    def position(self):
        for p in self.ex.fetch_positions([self.cfg.symbol]):
            if abs(float(p.get("contracts") or 0)) > 0:
                return p
        return None

    def cancel_all(self):
        try:
            self.ex.cancel_all_orders(self.cfg.symbol)
        except Exception as e:
            log.error("cancel_all_orders failed: %s", e)

    def close_all(self, reason):
        self.cancel_all()
        p = self.position()
        if p:
            side = "sell" if p["side"] == "long" else "buy"
            self.ex.create_order(self.cfg.symbol, "market", side, abs(float(p["contracts"])),
                                 params={"reduceOnly": True})
            log.warning("Closed position (%s)", reason)
        self.state.pop("trade", None)
        self.state.pop("pending", None)
        self.state["protected"] = False
        self.save()

    def protect(self):
        """Put a stop-loss (and take-profit) on the current position."""
        ex, sym, t = self.ex, self.cfg.symbol, self.state.get("trade")
        p = self.position()
        if not p:
            return
        if not t:
            self.close_all("position with unknown stop")
            return
        amount = abs(float(p["contracts"]))
        close_side = "sell" if t["side"] == 1 else "buy"
        try:
            ex.create_order(sym, "market", close_side, amount, None,
                            {"stopLossPrice": float(ex.price_to_precision(sym, t["stop"])), "reduceOnly": True})
        except Exception as e:
            log.error("STOP-LOSS FAILED (%s). Closing position immediately.", e)
            self.close_all("no stop-loss")
            return
        try:   # resting limit order = maker fee
            ex.create_order(sym, "limit", close_side, amount, float(ex.price_to_precision(sym, t["target"])),
                            {"reduceOnly": True})
        except Exception as e:
            log.error("Take-profit failed (%s). Stop is in place; exits at stop or session close.", e)
        self.state["protected"] = True
        self.save()
        log.warning("Protected %s %s: stop %.4f target %.4f", "LONG" if t["side"] == 1 else "SHORT",
                    amount, t["stop"], t["target"])

    # ------------------------------------------------------------ every 15 seconds
    def fast_step(self):
        st, sym = self.state, self.cfg.symbol
        pos = self.position()
        pend = st.get("pending")

        if pend:
            open_ids = {o["id"] for o in self.ex.fetch_open_orders(sym)}
            still_open = pend["id"] in open_ids
            if pos:                                   # (partly) filled
                if still_open:
                    self.ex.cancel_order(pend["id"], sym)
                st["trade"] = {k: pend[k] for k in ("side", "stop", "target")}
                st.pop("pending")
                st["protected"] = False
                self.save()
                log.warning("Limit entry filled at ~%s", pos.get("entryPrice"))
            elif not still_open or self.ex.milliseconds() > pend["expires_ms"]:
                if still_open:
                    self.ex.cancel_order(pend["id"], sym)
                st.pop("pending")
                self.save()
                log.info("Limit entry not filled, cancelled.")

        if pos and not st.get("protected"):
            self.protect()
        elif not pos and st.get("protected"):         # stop or target hit
            self.cancel_all()                          # remove the leftover order
            st["protected"] = False
            st.pop("trade", None)
            self.save()
            log.warning("Position closed by stop/target.")

        if datetime.now(timezone.utc).hour >= self.cfg.session_close:
            if pos:
                self.close_all("session close")
            elif st.get("pending"):
                self.ex.cancel_order(st["pending"]["id"], sym)
                st.pop("pending")
                self.save()

    # ------------------------------------------------------------ every new candle
    def candles(self):
        df = to_frame(self.ex.fetch_ohlcv(self.cfg.symbol, self.cfg.timeframe, limit=self.cfg.candles_to_load))
        if df.index[-1].value // 1_000_000 + self.tf_ms > self.ex.milliseconds():   # still forming
            df = df.iloc[:-1]
        return df

    def candle_step(self):
        cfg, st = self.cfg, self.state
        today = datetime.now(timezone.utc).date().isoformat()
        eq = self.equity()
        if st.get("day") != today:
            st.update(day=today, day_start_equity=eq, halted=False)
            self.save()
        if (eq - st["day_start_equity"]) / max(st["day_start_equity"], 1e-9) <= -cfg.max_daily_loss:
            if not st.get("halted"):
                log.warning("Daily loss limit hit (%.2f -> %.2f). Halted until tomorrow.", st["day_start_equity"], eq)
                st["halted"] = True
                self.save()
            return

        df = self.candles()
        strat = AMDStrategy(cfg).prepare(df)
        sig = None
        for i in range(len(df)):           # replay so daily state matches the backtest
            sig = strat.on_bar(i)
        last_ts = str(df.index[-1])
        busy = self.position() or st.get("pending")
        if not sig or busy or st.get("halted") or st.get("last_signal") == last_ts:
            return
        st["last_signal"] = last_ts
        self.save()
        self.execute(sig, eq)

    def execute(self, sig, eq):
        cfg, ex, sym, st = self.cfg, self.ex, self.cfg.symbol, self.state
        side = "buy" if sig.side == 1 else "sell"
        price = ex.fetch_ticker(sym)["last"]
        if sig.side * (price - sig.stop) <= 0 or sig.side * (sig.target - price) <= 0:
            log.info("Price already past stop/target, skipping.")
            return
        entry = sig.entry if sig.order == "limit" else price
        qty = min(eq * cfg.risk_per_trade / abs(entry - sig.stop), eq * cfg.max_leverage / entry)
        amount = float(ex.amount_to_precision(sym, qty))
        if amount <= 0:
            log.info("Position size rounds to zero, skipping.")
            return
        try:
            ex.set_leverage(int(cfg.max_leverage), sym)
        except Exception:
            pass
        log.warning("SIGNAL %s %s %s qty=%s entry=%.4f stop=%.4f target=%.4f RR=%.2f score=%d\n  %s",
                    sig.setup, sig.order.upper(), side.upper(), amount, entry, sig.stop, sig.target,
                    sig.rr, sig.score, "\n  ".join(sig.reasons))
        trade = {"side": sig.side, "stop": sig.stop, "target": sig.target}
        if sig.order == "limit":
            o = ex.create_order(sym, "limit", side, amount, float(ex.price_to_precision(sym, entry)))
            st["pending"] = {**trade, "id": o["id"],
                             "expires_ms": ex.milliseconds() + cfg.limit_wait_bars * self.tf_ms}
            self.save()
        else:
            ex.create_order(sym, "market", side, amount)
            st["trade"], st["protected"] = trade, False
            self.save()
            self.protect()

    # ------------------------------------------------------------ loop
    def run(self):
        last_candle = None
        while True:
            try:
                self.fast_step()
                now = self.ex.milliseconds()
                candle = now // self.tf_ms
                if candle != last_candle and now % self.tf_ms > 5_000:   # 5s after each close
                    last_candle = candle
                    self.candle_step()
                    self.fast_step()       # protect a market fill right away
            except Exception as e:
                log.exception("Cycle error: %s", e)
            time.sleep(FAST_POLL_SECONDS)
