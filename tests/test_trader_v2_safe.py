import time

from bot.trader_v2_safe import Trader
from tests.test_trader_v2 import FakeExchange, make_config, make_signal


class GroupedFakeExchange(FakeExchange):
    def _place_grouped(self, price, amount_quote, tp_price, sl_price, side):
        qty = amount_quote / price
        entry_oid = self.next_oid
        tp_oid = self.next_oid + 1
        sl_oid = self.next_oid + 2
        self.next_oid += 3

        entry_side = "B" if side == "long" else "A"
        close_side = "A" if side == "long" else "B"
        self.normal_orders.append({
            "coin": self.symbol,
            "oid": entry_oid,
            "limitPx": price,
            "sz": qty,
            "side": entry_side,
            "reduceOnly": False,
            "orderType": "Limit",
            "isTrigger": False,
        })
        self.triggers.extend([
            {
                "coin": self.symbol,
                "oid": tp_oid,
                "triggerPx": tp_price,
                "limitPx": tp_price,
                "orderType": "Take Profit Market",
                "side": close_side,
                "sz": qty,
                "isTrigger": True,
                "reduceOnly": True,
            },
            {
                "coin": self.symbol,
                "oid": sl_oid,
                "triggerPx": sl_price,
                "limitPx": sl_price,
                "orderType": "Stop Market",
                "side": close_side,
                "sz": qty,
                "isTrigger": True,
                "reduceOnly": True,
            },
        ])

        raw = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"resting": {"oid": entry_oid}},
                        "waitingForTrigger",
                        "waitingForTrigger",
                    ]
                },
            },
        }
        return {
            "oid": entry_oid,
            "status": "open",
            "amount": qty,
            "price": price,
            "raw": raw,
        }

    def place_limit_buy(self, price, amount_quote, tp_price=0, sl_price=0):
        assert tp_price > 0 and sl_price > 0
        return self._place_grouped(price, amount_quote, tp_price, sl_price, "long")

    def place_limit_sell(self, price, amount_quote, tp_price=0, sl_price=0):
        assert tp_price > 0 and sl_price > 0
        return self._place_grouped(price, amount_quote, tp_price, sl_price, "short")

    def cancel_order(self, price, oid):
        self.normal_orders = [o for o in self.normal_orders if int(o["oid"]) != int(oid)]
        self.triggers = [o for o in self.triggers if int(o["oid"]) != int(oid)]
        return {"status": "cancelled"}

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
                "reduceOnly": True,
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
                "reduceOnly": True,
            },
        ]
        self.next_oid += 2
        return {"tp": {"ok": True}, "sl": {"ok": True}}


def test_grouped_pending_has_one_entry_and_reduce_only_tp_sl():
    ex = GroupedFakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()

    trader._place_signal(signal, ex.price)

    assert "BTC" in trader.pending_orders
    assert len(ex.normal_orders) == 1
    assert ex.normal_orders[0]["reduceOnly"] is False
    assert len(ex.triggers) == 2
    assert all(o["reduceOnly"] is True for o in ex.triggers)
    assert {o["orderType"] for o in ex.triggers} == {
        "Take Profit Market",
        "Stop Market",
    }


def test_second_submission_for_same_symbol_is_suppressed():
    ex = GroupedFakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()

    trader._place_signal(signal, ex.price)
    trader._place_signal(signal, ex.price)

    assert len(ex.normal_orders) == 1
    assert len(ex.triggers) == 2
    assert len(trader.pending_orders) == 1


def test_existing_exchange_family_is_cleaned_not_stacked():
    ex = GroupedFakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()

    ex.normal_orders = [
        {"coin": "BTC", "oid": 900, "limitPx": 100.5, "sz": 1, "side": "B"},
        {"coin": "BTC", "oid": 901, "limitPx": 100.5, "sz": 1, "side": "B"},
    ]

    trader._place_signal(signal, ex.price)

    # The trader cleans the ambiguous old family and waits for a later scan.
    assert ex.normal_orders == []
    assert ex.triggers == []
    assert "BTC" not in trader.pending_orders


def test_partial_fill_is_immediately_capped_and_protected():
    ex = GroupedFakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()
    trader._place_signal(signal, ex.price)
    pending = trader.pending_orders["BTC"]

    # Simulate a partial fill while the original opening order is still resting.
    partial_qty = pending.quantity / 2
    ex.position = {
        "coin": "BTC",
        "size": partial_qty,
        "side": "long",
        "entry_price": pending.price,
        "unrealized_pnl": 0.0,
    }

    trader._check_pending_order("BTC", ex.price, time.time())

    # Remaining opening quantity was cancelled, so this symbol cannot continue
    # accumulating size after the first fill.
    assert ex.normal_orders == []
    assert "BTC" not in trader.pending_orders
    assert "BTC" in trader.positions
    assert trader.positions["BTC"].quantity == partial_qty
    assert len(ex.triggers) == 2
    assert all(o["reduceOnly"] is True for o in ex.triggers)


def test_live_position_blocks_another_opener():
    ex = GroupedFakeExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()

    ex.position = {
        "coin": "BTC",
        "size": 0.5,
        "side": "long",
        "entry_price": 100.0,
        "unrealized_pnl": 0.0,
    }

    trader._place_signal(signal, ex.price)

    assert ex.normal_orders == []
    assert ex.triggers == []
    assert "BTC" not in trader.pending_orders
