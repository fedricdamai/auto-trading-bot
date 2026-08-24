import time

from bot.trader_v2 import OpenPosition
from bot.trader_v2_guarded import Trader
from tests.test_trader_v2 import make_config, make_signal
from tests.test_trader_v2_safe import GroupedFakeExchange


class ParentCancelDropsChildrenExchange(GroupedFakeExchange):
    """Model a grouped venue transition where cancelling remainder drops children."""

    def cancel_order(self, price, oid):
        is_entry = any(int(o["oid"]) == int(oid) for o in self.normal_orders)
        result = super().cancel_order(price, oid)
        if is_entry:
            self.triggers = []
        return result


def test_partial_fill_rebuilds_children_if_parent_cancel_drops_them():
    ex = ParentCancelDropsChildrenExchange()
    trader = Trader(make_config(), ex)
    signal = make_signal()
    trader._place_signal(signal, ex.price)
    pending = trader.pending_orders["BTC"]

    partial_qty = pending.quantity / 2
    ex.position = {
        "coin": "BTC",
        "size": partial_qty,
        "side": "long",
        "entry_price": pending.price,
        "unrealized_pnl": 0.0,
    }

    trader._check_pending_order("BTC", ex.price, time.time())

    assert ex.normal_orders == []
    assert "BTC" in trader.positions
    assert "BTC" not in trader.pending_orders
    assert trader.positions["BTC"].quantity == partial_qty
    assert len(ex.triggers) == 2
    assert all(o["reduceOnly"] is True for o in ex.triggers)
    assert all(abs(float(o["sz"]) - partial_qty) < 1e-12 for o in ex.triggers)


def test_watchdog_repairs_missing_protection_for_known_live_position():
    ex = GroupedFakeExchange()
    trader = Trader(make_config(), ex)

    ex.position = {
        "coin": "BTC",
        "size": 0.5,
        "side": "long",
        "entry_price": 100.0,
        "unrealized_pnl": 0.0,
    }
    ex.normal_orders = []
    ex.triggers = []

    trader.positions["BTC"] = OpenPosition(
        symbol="BTC",
        entry_price=100.0,
        quantity=0.5,
        side="long",
        kind="support",
        level_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        initial_sl=99.0,
        initial_tp=103.0,
        highest_price=100.0,
        lowest_price=100.0,
        strength=80.0,
        timeframes=["1h"],
        filled_at=time.time(),
        leverage=3,
        last_synced_sl=99.0,
        last_synced_tp=103.0,
        trigger_oids=[],
        last_protection_check=time.time(),
    )

    trader._watchdog_exchange_positions()

    assert len(ex.triggers) == 2
    assert all(o["reduceOnly"] is True for o in ex.triggers)
    assert all(abs(float(o["sz"]) - 0.5) < 1e-12 for o in ex.triggers)


def test_watchdog_replaces_wrong_size_protection():
    ex = GroupedFakeExchange()
    trader = Trader(make_config(), ex)

    ex.position = {
        "coin": "BTC",
        "size": 0.5,
        "side": "long",
        "entry_price": 100.0,
        "unrealized_pnl": 0.0,
    }
    ex.triggers = [
        {
            "coin": "BTC",
            "oid": 40,
            "triggerPx": 103.0,
            "limitPx": 103.0,
            "orderType": "Take Profit Market",
            "side": "A",
            "sz": 1.0,
            "isTrigger": True,
            "reduceOnly": True,
        },
        {
            "coin": "BTC",
            "oid": 41,
            "triggerPx": 99.0,
            "limitPx": 99.0,
            "orderType": "Stop Market",
            "side": "A",
            "sz": 1.0,
            "isTrigger": True,
            "reduceOnly": True,
        },
    ]
    trader.positions["BTC"] = OpenPosition(
        symbol="BTC",
        entry_price=100.0,
        quantity=0.5,
        side="long",
        kind="support",
        level_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        initial_sl=99.0,
        initial_tp=103.0,
        highest_price=100.0,
        lowest_price=100.0,
        strength=80.0,
        timeframes=["1h"],
        filled_at=time.time(),
        leverage=3,
        last_synced_sl=99.0,
        last_synced_tp=103.0,
        trigger_oids=[40, 41],
        last_protection_check=time.time(),
    )

    trader._watchdog_exchange_positions()

    assert len(ex.triggers) == 2
    assert all(o["reduceOnly"] is True for o in ex.triggers)
    assert all(abs(float(o["sz"]) - 0.5) < 1e-12 for o in ex.triggers)
