from bot.hyperliquid_exchange import HyperliquidExchange


class TestTriggerOrderDetection:
    """frontend_open_orders reports TP/SL orders with orderType values like
    "Stop Market" / "Take Profit Market" — the filter must catch them, or
    cancels become silent no-ops and every update stacks duplicate orders."""

    def test_stop_market_is_trigger(self):
        assert HyperliquidExchange._is_trigger_order({"orderType": "Stop Market"})

    def test_stop_limit_is_trigger(self):
        assert HyperliquidExchange._is_trigger_order({"orderType": "Stop Limit"})

    def test_take_profit_market_is_trigger(self):
        assert HyperliquidExchange._is_trigger_order({"orderType": "Take Profit Market"})

    def test_take_profit_limit_is_trigger(self):
        assert HyperliquidExchange._is_trigger_order({"orderType": "Take Profit Limit"})

    def test_is_trigger_flag_is_trusted(self):
        assert HyperliquidExchange._is_trigger_order({"orderType": "Limit", "isTrigger": True})

    def test_plain_limit_is_not_trigger(self):
        assert not HyperliquidExchange._is_trigger_order({"orderType": "Limit", "isTrigger": False})

    def test_empty_order_is_not_trigger(self):
        assert not HyperliquidExchange._is_trigger_order({})
