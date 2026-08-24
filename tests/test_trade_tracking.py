import json
import re
import time

from bot.trade_lifecycle import TradeLifecycleLog, generate_trade_id
from bot.trader_v2_tracked import Trader
from tests.test_trader_v2 import make_config, make_signal
from tests.test_trader_v2_safe import GroupedFakeExchange


TRADE_ID_RE = re.compile(r"^[A-Z0-9]+-[LS]-\d{8}-\d{6}-[A-F0-9]{4}$")


def make_tracked_trader(ex, tmp_path):
    Trader.THESIS_STATE_FILE = tmp_path / "v2_trade_thesis.json"
    Trader.BOT_LOG_FILE = tmp_path / "bot.log"
    Trader.LIFECYCLE_FILE = tmp_path / "trade_lifecycle.jsonl"
    trader = Trader(make_config(), ex)
    trader.journal.path = tmp_path / "trade_journal.jsonl"
    return trader


def read_events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_trade_id_format_is_readable_and_unique():
    first = generate_trade_id("BTC", "long", now=1_777_000_000)
    second = generate_trade_id("BTC", "long", now=1_777_000_000)

    assert TRADE_ID_RE.match(first)
    assert TRADE_ID_RE.match(second)
    assert first != second
    assert first.startswith("BTC-L-")


def test_trade_id_persists_from_trigger_through_fill(tmp_path):
    ex = GroupedFakeExchange()
    trader = make_tracked_trader(ex, tmp_path)
    signal = make_signal()

    trader._place_signal(signal, ex.price)

    thesis = trader._trade_theses["BTC"]
    trade_id = thesis["trade_id"]
    pending = trader.pending_orders["BTC"]

    assert TRADE_ID_RE.match(trade_id)
    assert getattr(pending, "trade_id") == trade_id
    assert trader.get_trade_id("BTC") == trade_id

    events = read_events(Trader.LIFECYCLE_FILE)
    assert [e["event"] for e in events] == ["TRIGGER_CREATED", "ORDER_PENDING"]
    assert all(e["trade_id"] == trade_id for e in events)

    ex.position = {
        "coin": "BTC",
        "size": pending.quantity,
        "side": "long",
        "entry_price": pending.price,
        "unrealized_pnl": 0.0,
    }
    trader._check_pending_order("BTC", ex.price, time.time())

    pos = trader.positions["BTC"]
    assert getattr(pos, "trade_id") == trade_id
    assert trader.get_trade_id("BTC") == trade_id

    events = read_events(Trader.LIFECYCLE_FILE)
    names = [e["event"] for e in events]
    assert "FILLED" in names
    assert "PROTECTION_ACTIVE" in names
    assert all(e["trade_id"] == trade_id for e in events)


def test_final_exit_records_result_under_same_trade_id(tmp_path):
    ex = GroupedFakeExchange()
    trader = make_tracked_trader(ex, tmp_path)
    signal = make_signal()
    trader._place_signal(signal, ex.price)
    pending = trader.pending_orders["BTC"]
    trade_id = trader.get_trade_id("BTC")

    ex.position = {
        "coin": "BTC",
        "size": pending.quantity,
        "side": "long",
        "entry_price": pending.price,
        "unrealized_pnl": 0.0,
    }
    trader._check_pending_order("BTC", ex.price, time.time())

    # Model an exchange-side take-profit fill.
    ex.position = None
    exit_price = signal.take_profit
    trader._finalize_external_exit("BTC", exit_price, "take_profit")

    events = read_events(Trader.LIFECYCLE_FILE)
    exits = [e for e in events if e["event"] == "EXIT"]
    assert len(exits) == 1
    result = exits[0]
    assert result["trade_id"] == trade_id
    assert result["details"]["outcome"] == "WIN"
    assert result["details"]["reason"] == "take_profit"
    assert result["details"]["pnl_usd"] > 0
    assert "BTC" not in trader._trade_theses


def test_lifecycle_log_can_rebuild_one_trade_history(tmp_path):
    path = tmp_path / "events.jsonl"
    log = TradeLifecycleLog(path)
    trade_id = "SOL-S-20260824-184205-A3F7"

    log.event(trade_id, "TRIGGER_CREATED", "SOL", "short", level=96.25)
    log.event(trade_id, "FILLED", "SOL", "short", entry=96.25)
    log.event(trade_id, "EXIT", "SOL", "short", outcome="WIN")

    rows = log.load_trade(trade_id)
    assert [row["event"] for row in rows] == [
        "TRIGGER_CREATED",
        "FILLED",
        "EXIT",
    ]
