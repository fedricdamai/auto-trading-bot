from bot.trader_v2_production import Trader
from tests.test_trader_v2 import make_config, make_signal
from tests.test_trader_v2_safe import GroupedFakeExchange


def test_active_pending_trade_keeps_same_identity(tmp_path):
    Trader.THESIS_STATE_FILE = tmp_path / "v2_trade_thesis.json"
    Trader.BOT_LOG_FILE = tmp_path / "bot.log"
    Trader.LIFECYCLE_FILE = tmp_path / "trade_lifecycle.jsonl"

    ex = GroupedFakeExchange()
    trader = Trader(make_config(), ex)

    first = make_signal()
    trader._place_signal(first, ex.price)
    first_id = trader.get_trade_id("BTC")
    assert first_id

    second = make_signal()
    second.signal_id = "BTC:long:different-scan"
    second.entry_price = first.entry_price - 0.1
    trader._place_signal(second, ex.price)

    assert trader.get_trade_id("BTC") == first_id
    assert trader._trade_theses["BTC"]["trade_id"] == first_id
    assert len(ex.normal_orders) == 1
    assert len(ex.triggers) == 2
