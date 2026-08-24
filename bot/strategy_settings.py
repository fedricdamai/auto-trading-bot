"""Trading strategy configuration.

Keep strategy and risk parameters in source control so every deployed revision has
an explicit, reviewable trading model. Environment variables are reserved for
credentials and runtime/deployment switches.

Change these values through a reviewed code change, then run the test suite
before deployment.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategySettings:
    # Universe and execution cadence
    primary_symbol: str = "BTC"
    symbols: tuple[str, ...] = ("BTC", "ETH", "SOL", "HYPE", "BNB")
    max_positions: int = 5

    # Exchange state is managed every 5 seconds. Historical trade setups are
    # re-evaluated every 60 seconds. This is intentionally independent from the
    # 30m/1h/4h candle timeframes used by the strategy itself.
    check_interval_seconds: int = 5
    signal_scan_interval_seconds: int = 60

    # Telegram heartbeat is intentionally much slower than the market scan so
    # "no trade" periods still prove the bot is alive without spamming chat.
    heartbeat_interval_seconds: int = 600

    # Higher-timeframe strategy
    decision_timeframe: str = "30m"
    lookback_candles: int = 200
    min_touches: int = 2
    level_tolerance_pct: float = 0.50

    # Conservative fixed leverage while the strategy is validated.
    leverage: int = 3
    leverage_min: int = 3
    leverage_max: int = 10

    # Risk sizing
    risk_per_trade_usd: float = 4.0
    max_position_notional_usd: float = 500.0
    min_position_notional_usd: float = 10.0
    max_margin_fraction: float = 0.10
    order_size_hard_cap_usd: float = 2000.0

    # 4h + 1h regime and S/R quality gates
    min_trend_confidence: float = 70.0
    min_level_strength: float = 65.0
    min_level_timeframes: int = 2
    max_level_distance_pct: float = 3.0

    # 30m rejection confirmation and pullback entry
    confirmation_zone_pct: float = 0.35
    entry_pullback_atr: float = 0.15

    # Structural stop
    sl_atr_mult: float = 0.75
    sl_zone_buffer_pct: float = 0.35
    min_stop_distance_pct: float = 0.60
    max_stop_distance_pct: float = 2.50

    # Profit target
    target_risk_reward: float = 1.50
    tp_zone_buffer_pct: float = 0.15

    # Re-entry control
    symbol_cooldown_minutes: float = 30.0

    # Legacy V1 defaults retained only for rollback compatibility.
    legacy_target_profit_usd: float = 4.0
    legacy_target_pnl_pct: float = 2.5
    legacy_max_loss_pct: float = 1.5
    legacy_sl_zone_buffer_pct: float = 0.25
    legacy_tp_zone_buffer_pct: float = 0.15
    legacy_min_risk_reward: float = 1.5
    legacy_tp_sl_sync_min_change_pct: float = 0.1
    legacy_order_ttl_hours: float = 2.0
    legacy_level_refresh_seconds: int = 300
    legacy_doji_check_seconds: int = 300
    legacy_cooldown_seconds: int = 60


STRATEGY = StrategySettings()
