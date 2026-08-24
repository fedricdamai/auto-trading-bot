import time

from bot.strategy_v2 import Regime
from bot.trader_v2_continuous import Trader
from tests.test_trader_v2 import FakeExchange, make_config, make_signal


class FakeNotifier:
    def __init__(self):
        self.heartbeats = []

    def notify_heartbeat(self, snapshot):
        self.heartbeats.append(snapshot)
        return True


def test_signal_scan_runs_between_30m_boundaries():
    ex = FakeExchange()
    config = make_config()
    config.v2_signal_scan_interval_seconds = 60
    trader = Trader(config, ex)

    # Avoid startup work and make the base Trader believe the current 30m
    # boundary has already been processed.
    now = time.time()
    trader._synced = True
    trader._last_signal_bucket = int(now // (30 * 60))
    trader._last_continuous_scan_at = now - 61

    calls = []
    trader._revalidate_pending_signals = lambda ts: calls.append(("revalidate", ts))
    trader._on_new_30m_candle = lambda ts: calls.append(("scan", ts))

    trader.run_once()

    assert [name for name, _ in calls] == ["revalidate", "scan"]


def test_signal_scan_does_not_run_every_five_second_tick():
    ex = FakeExchange()
    config = make_config()
    config.v2_signal_scan_interval_seconds = 60
    trader = Trader(config, ex)

    now = time.time()
    trader._synced = True
    trader._last_signal_bucket = int(now // (30 * 60))
    trader._last_continuous_scan_at = now

    calls = []
    trader._revalidate_pending_signals = lambda ts: calls.append("revalidate")
    trader._on_new_30m_candle = lambda ts: calls.append("scan")

    trader.run_once()

    assert calls == []


def test_pending_limit_is_cancelled_when_fresh_scan_invalidates(monkeypatch):
    ex = FakeExchange()
    config = make_config()
    trader = Trader(config, ex)
    signal = make_signal()

    trader._place_signal(signal, ex.price)
    assert "BTC" in trader.pending_orders
    assert len(ex.normal_orders) == 1

    def no_longer_valid(*args, **kwargs):
        return None, [], Regime("neutral", 0.0, {})

    monkeypatch.setattr("bot.trader_v2_continuous.build_signal", no_longer_valid)
    trader._revalidate_pending_signals(time.time())

    assert "BTC" not in trader.pending_orders
    assert ex.normal_orders == []


def test_pending_limit_survives_when_same_historical_thesis_is_still_valid(monkeypatch):
    ex = FakeExchange()
    config = make_config()
    trader = Trader(config, ex)
    signal = make_signal()

    trader._place_signal(signal, ex.price)
    assert "BTC" in trader.pending_orders

    def same_setup(*args, **kwargs):
        return signal, [], signal.regime

    monkeypatch.setattr("bot.trader_v2_continuous.build_signal", same_setup)
    trader._revalidate_pending_signals(time.time())

    assert "BTC" in trader.pending_orders
    assert len(ex.normal_orders) == 1


def test_heartbeat_is_sent_after_a_completed_scan():
    ex = FakeExchange()
    config = make_config()
    config.v2_heartbeat_interval_seconds = 600
    notifier = FakeNotifier()
    trader = Trader(config, ex, notifier=notifier)

    now = time.time()
    trader._last_scan_completed_at = now
    trader.last_trend_bias["BTC"] = Regime("bullish", 100.0, {})

    trader._maybe_send_heartbeat(now)

    assert len(notifier.heartbeats) == 1
    snapshot = notifier.heartbeats[0]
    assert snapshot["mode"] == "LIVE TESTNET"
    assert snapshot["positions"] == 0
    assert snapshot["pending"] == 0
    assert snapshot["rows"][0]["symbol"] == "BTC"
    assert "bullish 100%" in snapshot["rows"][0]["regime"]


def test_heartbeat_is_throttled_between_intervals():
    ex = FakeExchange()
    config = make_config()
    config.v2_heartbeat_interval_seconds = 600
    notifier = FakeNotifier()
    trader = Trader(config, ex, notifier=notifier)

    now = time.time()
    trader._last_scan_completed_at = now
    trader.last_trend_bias["BTC"] = Regime("neutral", 0.0, {})

    trader._maybe_send_heartbeat(now)
    trader._maybe_send_heartbeat(now + 60)

    assert len(notifier.heartbeats) == 1


def test_heartbeat_explains_idle_directional_symbol():
    ex = FakeExchange()
    config = make_config()
    trader = Trader(config, ex)
    trader.last_trend_bias["BTC"] = Regime("bullish", 100.0, {})
    trader.known_levels["BTC"] = {}

    reason = trader._describe_idle_symbol("BTC")

    assert reason == "no qualifying support"
