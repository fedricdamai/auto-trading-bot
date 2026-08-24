from types import SimpleNamespace

from bot.telegram_notifier_v2 import flatten_exchange_state


class FakeInfo:
    def __init__(self, exchange):
        self.exchange = exchange

    def frontend_open_orders(self, address):
        out = []
        for orders in self.exchange.normal.values():
            out.extend(orders)
        for orders in self.exchange.triggers.values():
            out.extend(orders)
        return out


class FakeExchange:
    def __init__(self):
        self.symbol = "BTC"
        self.address = "0xtest"
        self.info = FakeInfo(self)
        self.positions = {
            "ETH": {
                "coin": "ETH",
                "size": 0.5,
                "side": "long",
                "entry_price": 2500.0,
                "unrealized_pnl": 0.0,
            }
        }
        self.normal = {
            "BTC": [],
            "ETH": [],
            # Simulates an orphan order on a symbol removed from config.
            "HYPE": [{"coin": "HYPE", "oid": 91, "limitPx": 40.0}],
        }
        self.triggers = {
            "BTC": [],
            "ETH": [
                {"coin": "ETH", "oid": 11, "triggerPx": 2600.0, "isTrigger": True},
                {"coin": "ETH", "oid": 12, "triggerPx": 2400.0, "isTrigger": True},
            ],
            "HYPE": [{"coin": "HYPE", "oid": 92, "triggerPx": 45.0, "isTrigger": True}],
        }

    def switch_symbol(self, symbol):
        self.symbol = symbol

    def get_all_positions(self):
        return list(self.positions.values())

    def get_position(self, symbol=None):
        return self.positions.get(symbol or self.symbol)

    def get_open_orders_for_symbol(self, symbol):
        return list(self.normal.get(symbol, []))

    def get_trigger_orders_for_symbol(self, symbol):
        return list(self.triggers.get(symbol, []))

    def cancel_orders_for_symbol(self, symbol):
        count = len(self.normal.get(symbol, [])) + len(self.triggers.get(symbol, []))
        self.normal[symbol] = []
        self.triggers[symbol] = []
        return count

    def cancel_trigger_orders_for_symbol(self, symbol):
        count = len(self.triggers.get(symbol, []))
        self.triggers[symbol] = []
        return count

    def place_market_close(self, quantity, side="long"):
        self.positions.pop(self.symbol, None)
        return {"status": "filled", "amount": quantity}


class FakeTrader:
    def __init__(self):
        self.config = SimpleNamespace(
            paper_trade=False,
            hl_symbol="BTC",
            hl_symbols="BTC,ETH",
        )
        self.exchange = FakeExchange()
        self.primary_symbol = "BTC"
        # Deliberately stale/empty local state. The exchange is the truth.
        self.positions = {}
        self.pending_orders = {}
        self.blocked_symbols = set()
        self.paused = False

    def _get_symbols(self):
        return ["BTC", "ETH"]


def test_closeall_uses_exchange_truth_and_cleans_orphan_symbol_orders():
    trader = FakeTrader()

    result = flatten_exchange_state(trader)

    assert trader.paused is True
    assert result["failures"] == {}
    assert result["closed"] == 1
    assert set(result["clean_symbols"]) == {"BTC", "ETH", "HYPE"}
    assert trader.exchange.positions == {}
    assert trader.exchange.normal["HYPE"] == []
    assert trader.exchange.triggers["HYPE"] == []
    assert trader.exchange.triggers["ETH"] == []


class StubbornEntryExchange(FakeExchange):
    def __init__(self):
        super().__init__()
        self.normal["ETH"] = [{"coin": "ETH", "oid": 77, "limitPx": 2450.0}]

    def cancel_orders_for_symbol(self, symbol):
        if symbol == "ETH":
            # Refuse to clear the non-reduce-only entry. The helper must not
            # market-close ETH while an entry can immediately reopen exposure.
            self.triggers[symbol] = []
            return 0
        return super().cancel_orders_for_symbol(symbol)


class StubbornTrader(FakeTrader):
    def __init__(self):
        super().__init__()
        self.exchange = StubbornEntryExchange()


def test_closeall_refuses_market_close_when_stale_entry_cannot_be_cancelled():
    trader = StubbornTrader()

    result = flatten_exchange_state(trader)

    assert "ETH" in result["failures"]
    assert trader.exchange.get_position("ETH") is not None
    assert "ETH" in trader.blocked_symbols
