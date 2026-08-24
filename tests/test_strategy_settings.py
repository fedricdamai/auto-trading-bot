import importlib

import bot.config as config_module
from bot.strategy_settings import STRATEGY


def test_strategy_parameters_are_source_controlled_not_env(monkeypatch):
    monkeypatch.setenv("V2_LEVERAGE", "50")
    monkeypatch.setenv("V2_RISK_PER_TRADE_USD", "999")
    monkeypatch.setenv("V2_MIN_TREND_CONFIDENCE", "1")
    monkeypatch.setenv("TIMEFRAME", "1m")
    monkeypatch.setenv("HL_SYMBOLS", "DOGE")

    reloaded = importlib.reload(config_module)
    cfg = reloaded.Config()

    assert cfg.v2_leverage == STRATEGY.leverage
    assert cfg.v2_risk_per_trade_usd == STRATEGY.risk_per_trade_usd
    assert cfg.v2_min_trend_confidence == STRATEGY.min_trend_confidence
    assert cfg.timeframe == STRATEGY.decision_timeframe
    assert cfg.hl_symbols == ",".join(STRATEGY.symbols)


def test_runtime_network_switch_remains_env_configurable(monkeypatch):
    monkeypatch.setenv("HL_MAINNET", "false")
    reloaded = importlib.reload(config_module)
    assert reloaded.Config().hl_mainnet is False
