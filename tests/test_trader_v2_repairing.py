import time

from bot.trader_v2 import OpenPosition
from bot.trader_v2_repairing import Trader
from tests.test_trader_v2 import make_config, make_signal
from tests.test_trader_v2_safe import GroupedFakeExchange


class FlakyProtectionExchange(GroupedFakeExchange):
    """Grouped exchange whose post-fill TP/SL placement can fail temporarily."""

    def __init__(self):
        super().__init__()
        self.fail_protection = True
        self.market_close_calls = 0

    def cancel_order(self, price, oid):
        # Model the venue behavior seen in testnet: cancelling the filled/partial
        # parent can make its attached children disappear.
        is_entry = any(int(o["oid"]) == int(oid) for o in self.normal_orders)
        result = super().cancel_order(price, oid)
        if is_entry:
            self.triggers = []
        return result

    def place_tp_sl_orders(self, quantity, side, tp_price, sl_price):
        if self.fail_protection:
            self.triggers = []
            return {"tp_error": "temporary protection failure"}
        return super().place_tp_sl_orders(quantity, side, tp_price, sl_price)

    def place_market_close(self, quantity, side="long"):
        self.market_close_calls += 1
        return super().place_market_close(quantity, side=side)


def make_repairing_trader(ex, tmp_path):
    Trader.THESIS_STATE_FILE = tmp_path / "v2_trade_thesis.json"
    return Trader(make_config(), ex)


def test_fill_protection_failure_never_market_closes_and_retries_exact_levels(tmp_path):
    ex = FlakyProtectionExchange()
    trader = make_repairing_trader(ex, tmp_path)
    signal = make_signal()

    trader._place_signal(signal, ex.price)
    pending = trader.pending_orders["BTC"]
    exact_tp = pending.take_profit
    exact_sl = pending.stop_loss

    ex.position = {
        "coin": "BTC",
        "size": pending.quantity,
        "side": "long",
        "entry_price": pending.price,
        "unrealized_pnl": 0.0,
    }

    # First fill transition loses grouped children and TP/SL re-placement fails.
    trader._check_pending_order("BTC", ex.price, time.time())

    assert ex.market_close_calls == 0
    assert ex.position is not None
    assert "BTC" in trader.pending_orders
    assert "BTC" in trader.blocked_symbols
    assert "BTC" in trader.protection_repair_needed
    assert ex.triggers == []

    # Next watchdog tick retries using the SAME TP/SL stored on the signal.
    ex.fail_protection = False
    trader._watchdog_exchange_positions()

    assert ex.market_close_calls == 0
    assert ex.position is not None
    assert "BTC" not in trader.pending_orders
    assert "BTC" in trader.positions
    assert "BTC" not in trader.protection_repair_needed
    assert len(ex.triggers) == 2
    assert all(o["reduceOnly"] is True for o in ex.triggers)

    trigger_prices = sorted(float(o["triggerPx"]) for o in ex.triggers)
    assert trigger_prices == sorted([exact_sl, exact_tp])
    assert all(
        abs(float(o["sz"]) - float(ex.position["size"])) < 1e-12
        for o in ex.triggers
    )


def test_known_live_position_stays_open_until_protection_repair_succeeds(tmp_path):
    ex = FlakyProtectionExchange()
    trader = make_repairing_trader(ex, tmp_path)

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

    assert ex.market_close_calls == 0
    assert ex.position is not None
    assert "BTC" in trader.positions
    assert "BTC" in trader.protection_repair_needed
    assert ex.triggers == []

    ex.fail_protection = False
    trader._watchdog_exchange_positions()

    assert ex.market_close_calls == 0
    assert ex.position is not None
    assert "BTC" in trader.positions
    assert "BTC" not in trader.protection_repair_needed
    assert len(ex.triggers) == 2
    assert all(o["reduceOnly"] is True for o in ex.triggers)
    assert sorted(float(o["triggerPx"]) for o in ex.triggers) == [99.0, 103.0]


def test_exact_signal_tp_sl_persist_and_restore_after_restart(tmp_path):
    state_file = tmp_path / "v2_trade_thesis.json"
    Trader.THESIS_STATE_FILE = state_file

    ex = GroupedFakeExchange()
    first = Trader(make_config(), ex)
    signal = make_signal()
    first._place_signal(signal, ex.price)
    pending = first.pending_orders["BTC"]

    assert state_file.exists()

    # Simulate a process restart after the entry filled and grouped children
    # disappeared. The new process has no in-memory pending/position objects.
    ex.normal_orders = []
    ex.triggers = []
    ex.position = {
        "coin": "BTC",
        "size": pending.quantity,
        "side": "long",
        "entry_price": pending.price,
        "unrealized_pnl": 0.0,
    }

    restarted = Trader(make_config(), ex)
    restarted._recover_orphan_position("BTC", ex.position)

    assert "BTC" in restarted.positions
    assert restarted.positions["BTC"].take_profit == signal.take_profit
    assert restarted.positions["BTC"].stop_loss == signal.stop_loss
    assert len(ex.triggers) == 2
    assert all(o["reduceOnly"] is True for o in ex.triggers)
    assert sorted(float(o["triggerPx"]) for o in ex.triggers) == sorted(
        [signal.stop_loss, signal.take_profit]
    )
