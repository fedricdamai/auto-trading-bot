import time

from bot.config import Config
from bot.strategy_v2 import Regime, StrategySignal
from bot.trader_v2 import PendingOrder, Trader


class FakeExchange:
    def __init__(self):
        self.symbol = "BTC"
        self.price = 102.0
        self.position = None
        self.normal_orders = []
        self.triggers = []
        self.next_oid = 1
        self.cancel_turns_into_fill = False
        self.config = None

    def switch_symbol(self, symbol):
        self.symbol = symbol

    def get_ticker_price(self):
        return self.price

    def get_account_state(self):
        return {"marginSummary": {"accountValue": "10000"}}

    def get_position(self, symbol=None):
        return self.position

    def get_all_positions(self):
        return [self.position] if self.position else []

    def get_open_orders_for_symbol(self, symbol):
        return list(self.normal_orders)

    def get_trigger_orders_for_symbol(self, symbol):
        return list(self.triggers)

    def _set_leverage(self):
        return None

    def place_limit_buy(self, price, amount_quote, tp_price=0, sl_price=0):
        assert tp_price == 0 and sl_price == 0
        qty = amount_quote / price
        oid = self.next_oid
        self.next_oid += 1
        self.normal_orders.append({
            "coin": self.symbol, "oid": oid, "limitPx": price, "sz": qty, "side": "B"
        })
        return {"oid": oid, "status": "open", "amount": qty, "price": price}

    def place_limit_sell(self, price, amount_quote, tp_price=0, sl_price=0):
        assert tp_price == 0 and sl_price == 0
        qty = amount_quote / price
        oid = self.next_oid
        self.next_oid += 1
        self.normal_orders.append({
            "coin": self.symbol, "oid": oid, "limitPx": price, "sz": qty, "side": "A"
        })
        return {"oid": oid, "status": "open", "amount": qty, "price": price}

    def cancel_order(self, price, oid):
        if self.cancel_turns_into_fill:
            match = next((o for o in self.normal_orders if int(o["oid"]) == int(oid)), None)
            self.normal_orders = [o for o in self.normal_orders if int(o["oid"]) != int(oid)]
            if match:
                side = "long" if match["side"] == "B" else "short"
                self.position = {
                    "coin": self.symbol,
                    "size": float(match["sz"]),
                    "side": side,
                    "entry_price": float(match["limitPx"]),
                    "unrealized_pnl": 0.0,
                }
            return {"status": "filled_during_cancel"}
        self.normal_orders = [o for o in self.normal_orders if int(o["oid"]) != int(oid)]
        return {"status": "cancelled"}

    def cancel_orders_for_symbol(self, symbol):
        count = len(self.normal_orders) + len(self.triggers)
        self.normal_orders = []
        self.triggers = []
        return count

    def cancel_trigger_orders_for_symbol(self, symbol):
        count = len(self.triggers)
        self.triggers = []
        return count

    def place_tp_sl_orders(self, quantity, side, tp_price, sl_price):
        is_buy = side == "short"
        self.triggers = [
            {
                "coin": self.symbol,
                "oid": self.next_oid,
                "triggerPx": tp_price,
                "limitPx": tp_price,
                "orderType": "Take Profit Market",
                "side": "B" if is_buy else "A",
                "sz": quantity,
                "isTrigger": True,
            },
            {
                "coin": self.symbol,
                "oid": self.next_oid + 1,
                "triggerPx": sl_price,
                "limitPx": sl_price,
                "orderType": "Stop Market",
                "side": "B" if is_buy else "A",
                "sz": quantity,
                "isTrigger": True,
            },
        ]
        self.next_oid += 2
        return {"tp": {"ok": True}, "sl": {"ok": True}}

    def place_market_close(self, quantity, side="long"):
        self.position = None
        return {"status": "filled", "amount": quantity, "price": self.price}


def make_config():
    c = Config()
    c.paper_trade = False
    c.hl_symbol = "BTC"
    c.hl_symbols = "BTC"
    c.hl_multi_symbol = True
    c.hl_max_positions = 1
    c.order_size = 500
    c.v2_leverage = 3
    c.v2_risk_per_trade_usd = 4
    c.v2_max_position_notional_usd = 500
    c.v2_max_margin_fraction = 0.10
    return c


def make_signal(now=None):
    now = now or time.time()
    return StrategySignal(
        signal_id="BTC:long:test",
        symbol="BTC",
        side="long",
        kind="support",
        level_price=100.0,
        entry_price=100.5,
        stop_loss=99.0,
        take_profit=102.75,
        strength=80.0,
        timeframes=["1h", "4h"],
        atr=1.0,
        risk_reward=1.5,
        regime=Regime("bullish", 90.0, {}),
        confirmation_candle="2026-08-24T10:00:00",
        created_at=now,
        valid_until=now + 1800,
    )


def test_pending_entry_has_no_prefill_triggers():
    ex = FakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()

    trader._place_signal(signal, ex.price)

    assert "BTC" in trader.pending_orders
    assert len(ex.normal_orders) == 1
    assert ex.triggers == []


def test_fill_creates_exactly_one_tp_and_one_sl():
    ex = FakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()
    trader._place_signal(signal, ex.price)
    pending = trader.pending_orders["BTC"]

    ex.normal_orders = []
    ex.position = {
        "coin": "BTC",
        "size": pending.quantity,
        "side": "long",
        "entry_price": pending.price,
        "unrealized_pnl": 0.0,
    }
    trader._check_pending_order("BTC", ex.price, time.time())

    assert "BTC" not in trader.pending_orders
    assert "BTC" in trader.positions
    assert len(ex.triggers) == 2
    assert {o["orderType"] for o in ex.triggers} == {"Take Profit Market", "Stop Market"}


def test_expired_entry_is_cancelled_and_removed():
    ex = FakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal(now=time.time() - 3600)
    signal.valid_until = time.time() - 1
    trader._place_signal(signal, ex.price)

    assert len(ex.normal_orders) == 1
    trader._check_pending_order("BTC", ex.price, time.time())

    assert ex.normal_orders == []
    assert ex.triggers == []
    assert "BTC" not in trader.pending_orders


def test_cancel_fill_race_becomes_protected_position():
    ex = FakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()
    trader._place_signal(signal, ex.price)
    ex.cancel_turns_into_fill = True

    result = trader._cancel_pending_order("BTC", reason="test_race")

    assert result is False
    assert "BTC" not in trader.pending_orders
    assert "BTC" in trader.positions
    assert ex.position is not None
    assert len(ex.triggers) == 2


def test_position_size_uses_stop_risk_not_tp_profit():
    ex = FakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()
    # 1.5% stop distance at $4 risk gives about $268 notional.
    notional = trader._compute_order_notional(signal, leverage=3)
    assert 260 <= notional <= 275
