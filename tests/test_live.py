"""Live order handling against a fake exchange. These guard the real-money path."""
import pytest

import live
from config import Config
from strategy import Signal


class FakeExchange:
    def __init__(self, fail_stop=False):
        self.orders, self.pos, self.n, self.fail_stop, self.log = {}, None, 0, fail_stop, []

    def load_markets(self): pass
    def milliseconds(self): return 1_000_000
    def fetch_balance(self): return {"total": {"USDT": 1000}}
    def fetch_positions(self, syms): return [self.pos] if self.pos else []
    def fetch_open_orders(self, sym): return list(self.orders.values())
    def fetch_ticker(self, sym): return {"last": 300.0}
    def amount_to_precision(self, sym, q): return f"{q:.2f}"
    def price_to_precision(self, sym, p): return f"{p:.2f}"
    def set_leverage(self, *a): pass

    def cancel_order(self, oid, sym):
        self.orders.pop(oid, None)

    def cancel_all_orders(self, sym):
        self.orders.clear()

    def create_order(self, sym, typ, side, amount, price=None, params=None):
        params = params or {}
        if "stopLossPrice" in params and self.fail_stop:
            raise RuntimeError("exchange rejected stop")
        self.n += 1
        oid = str(self.n)
        self.log.append((typ, side, float(amount), price, params))
        if typ == "market" and not params:
            self.pos = {"side": "long" if side == "buy" else "short", "contracts": amount, "entryPrice": 300}
        elif typ == "market" and params == {"reduceOnly": True}:
            self.pos = None
        else:
            self.orders[oid] = {"id": oid}
        return {"id": oid}


@pytest.fixture
def trader(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # bot_state.json goes in a temp folder
    def make(**kw):
        ex = FakeExchange(**kw)
        return live.LiveTrader(Config(), ex), ex
    return make


LIMIT_LONG = Signal(0, None, 1, 295.0, 290.0, 310.0, "AMD sweep", 2, ["test"], "limit")
MARKET_LONG = Signal(0, None, 1, 300.0, 290.0, 320.0, "AMD sweep", 2, ["test"], "market")


def test_refuses_real_money_without_env_var(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("I_ACCEPT_REAL_MONEY_RISK", raising=False)
    with pytest.raises(SystemExit):
        live.LiveTrader(Config(live=True), FakeExchange())


def test_limit_fill_gets_stop_and_target(trader):
    t, ex = trader()
    t.execute(LIMIT_LONG, 1000)
    assert "pending" in t.state
    ex.orders.clear()
    ex.pos = {"side": "long", "contracts": 2.0, "entryPrice": 295}   # order filled
    t.fast_step()
    assert t.state["protected"]
    kinds = [(typ, p.get("stopLossPrice"), p.get("reduceOnly")) for typ, _, _, _, p in ex.log[1:]]
    assert ("market", 290.0, True) in kinds                           # stop-loss
    assert any(typ == "limit" and ro for typ, _, ro in kinds)         # take-profit


def test_rejected_stop_loss_closes_position(trader):
    t, ex = trader(fail_stop=True)
    t.execute(MARKET_LONG, 1000)
    assert ex.pos is None


def test_unknown_position_is_closed(trader):
    t, ex = trader()
    t.state.clear()
    ex.pos = {"side": "long", "contracts": 1, "entryPrice": 300}
    t.fast_step()
    assert ex.pos is None


def test_unfilled_limit_expires(trader):
    t, ex = trader()
    t.execute(LIMIT_LONG, 1000)
    t.state["pending"]["expires_ms"] = 0
    t.fast_step()
    assert "pending" not in t.state and not ex.orders


def test_position_risks_one_percent(trader):
    t, ex = trader()
    t.execute(LIMIT_LONG, 1000)
    qty = ex.log[0][2]
    assert qty * (295 - 290) == pytest.approx(10.0)   # 1% of $1000
