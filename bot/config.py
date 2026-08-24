import os
from dataclasses import dataclass
from dotenv import load_dotenv

from bot.strategy_settings import STRATEGY

load_dotenv()


@dataclass
class Config:
    """Application configuration.

    Environment variables are intentionally limited to secrets and deployment
    switches. Trading logic and risk parameters live in strategy_settings.py so
    they are versioned, reviewable, and tested with the code that uses them.
    """

    # Runtime / deployment switches
    exchange_backend: str = os.getenv("EXCHANGE_BACKEND", "hyperliquid")
    trader_version: str = os.getenv("TRADER_VERSION", "v2").lower()
    paper_trade: bool = os.getenv("PAPER_TRADE", "true").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # ccxt credentials / adapter settings
    exchange_id: str = os.getenv("EXCHANGE_ID", "binance")
    api_key: str = os.getenv("API_KEY", "")
    api_secret: str = os.getenv("API_SECRET", "")
    symbol: str = os.getenv("SYMBOL", "BTC/USDT")

    # Hyperliquid credentials / network only
    hl_wallet_address: str = os.getenv("HL_WALLET_ADDRESS", "")
    hl_private_key: str = os.getenv("HL_PRIVATE_KEY", "")
    hl_mainnet: bool = os.getenv("HL_MAINNET", "false").lower() == "true"

    # Trading universe comes from version-controlled strategy settings.
    hl_symbol: str = STRATEGY.primary_symbol
    hl_multi_symbol: bool = True
    hl_symbols: str = ",".join(STRATEGY.symbols)
    hl_scan_top_n: int = 10
    hl_max_positions: int = STRATEGY.max_positions

    # General trading settings from source-controlled strategy configuration.
    timeframe: str = STRATEGY.decision_timeframe
    lookback_candles: int = STRATEGY.lookback_candles
    min_touches: int = STRATEGY.min_touches
    level_tolerance_pct: float = STRATEGY.level_tolerance_pct
    order_size: float = STRATEGY.order_size_hard_cap_usd
    check_interval: int = STRATEGY.check_interval_seconds

    # V2 strategy / risk settings
    hl_leverage_min: int = STRATEGY.leverage_min
    hl_leverage_max: int = STRATEGY.leverage_max
    hl_leverage: int = STRATEGY.leverage
    v2_leverage: int = STRATEGY.leverage

    v2_risk_per_trade_usd: float = STRATEGY.risk_per_trade_usd
    v2_max_position_notional_usd: float = STRATEGY.max_position_notional_usd
    v2_min_position_notional_usd: float = STRATEGY.min_position_notional_usd
    v2_max_margin_fraction: float = STRATEGY.max_margin_fraction

    v2_min_trend_confidence: float = STRATEGY.min_trend_confidence
    v2_min_level_strength: float = STRATEGY.min_level_strength
    v2_min_level_timeframes: int = STRATEGY.min_level_timeframes
    v2_max_level_distance_pct: float = STRATEGY.max_level_distance_pct

    v2_confirmation_zone_pct: float = STRATEGY.confirmation_zone_pct
    v2_entry_pullback_atr: float = STRATEGY.entry_pullback_atr

    v2_sl_atr_mult: float = STRATEGY.sl_atr_mult
    v2_sl_zone_buffer_pct: float = STRATEGY.sl_zone_buffer_pct
    v2_min_stop_distance_pct: float = STRATEGY.min_stop_distance_pct
    v2_max_stop_distance_pct: float = STRATEGY.max_stop_distance_pct

    v2_target_risk_reward: float = STRATEGY.target_risk_reward
    v2_tp_zone_buffer_pct: float = STRATEGY.tp_zone_buffer_pct
    v2_symbol_cooldown_minutes: float = STRATEGY.symbol_cooldown_minutes

    # Legacy V1 compatibility. These are also code settings, not .env knobs.
    target_profit_usd: float = STRATEGY.legacy_target_profit_usd
    target_pnl_pct: float = STRATEGY.legacy_target_pnl_pct
    max_loss_pct: float = STRATEGY.legacy_max_loss_pct
    sl_zone_buffer_pct: float = STRATEGY.legacy_sl_zone_buffer_pct
    tp_zone_buffer_pct: float = STRATEGY.legacy_tp_zone_buffer_pct
    min_risk_reward: float = STRATEGY.legacy_min_risk_reward
    tp_sl_sync_min_change_pct: float = STRATEGY.legacy_tp_sl_sync_min_change_pct
    order_ttl_hours: float = STRATEGY.legacy_order_ttl_hours
    level_refresh_seconds: int = STRATEGY.legacy_level_refresh_seconds
    doji_check_seconds: int = STRATEGY.legacy_doji_check_seconds
    cooldown_seconds: int = STRATEGY.legacy_cooldown_seconds

    # Telegram credentials
    tg_bot_token: str = os.getenv("TG_BOT_TOKEN", "")
    tg_chat_id: str = os.getenv("TG_CHAT_ID", "")
    tg_allowed_users: str = os.getenv("TG_ALLOWED_USERS", "")

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_bot_token and self.tg_chat_id)

    @property
    def telegram_whitelist(self) -> set[int]:
        if not self.tg_allowed_users:
            return {int(self.tg_chat_id)} if self.tg_chat_id else set()
        return {int(uid.strip()) for uid in self.tg_allowed_users.split(",") if uid.strip()}
