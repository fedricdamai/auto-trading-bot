import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Exchange backend: "ccxt" or "hyperliquid"
    exchange_backend: str = os.getenv("EXCHANGE_BACKEND", "hyperliquid")

    # Trading engine. Hyperliquid defaults to the conservative V2 engine.
    # Set TRADER_VERSION=v1 only for deliberate rollback/comparison.
    trader_version: str = os.getenv("TRADER_VERSION", "v2").lower()

    # ccxt settings (used when EXCHANGE_BACKEND=ccxt)
    exchange_id: str = os.getenv("EXCHANGE_ID", "binance")
    api_key: str = os.getenv("API_KEY", "")
    api_secret: str = os.getenv("API_SECRET", "")
    symbol: str = os.getenv("SYMBOL", "BTC/USDT")

    # Hyperliquid settings (used when EXCHANGE_BACKEND=hyperliquid)
    hl_wallet_address: str = os.getenv("HL_WALLET_ADDRESS", "")
    hl_private_key: str = os.getenv("HL_PRIVATE_KEY", "")
    hl_symbol: str = os.getenv("HL_SYMBOL", "BTC")
    hl_mainnet: bool = os.getenv("HL_MAINNET", "false").lower() == "true"

    # Legacy V1 leverage range. V2 uses v2_leverage as a fixed low leverage,
    # bounded by this range so existing Telegram/config tooling still works.
    hl_leverage_min: int = int(os.getenv("HL_LEVERAGE_MIN", "3"))
    hl_leverage_max: int = int(os.getenv("HL_LEVERAGE_MAX", "10"))
    hl_leverage: int = 3

    # Multi-symbol scanning
    hl_multi_symbol: bool = os.getenv("HL_MULTI_SYMBOL", "true").lower() == "true"
    hl_symbols: str = os.getenv("HL_SYMBOLS", "BTC,ETH,SOL,HYPE,BNB")
    hl_scan_top_n: int = int(os.getenv("HL_SCAN_TOP_N", "10"))
    hl_max_positions: int = int(os.getenv("HL_MAX_POSITIONS", "5"))

    # General trading settings. V2 evaluates entries from closed 30m candles;
    # the bot still checks exchange state every CHECK_INTERVAL seconds.
    timeframe: str = os.getenv("TIMEFRAME", "30m")
    lookback_candles: int = int(os.getenv("LOOKBACK_CANDLES", "200"))
    min_touches: int = int(os.getenv("MIN_TOUCHES", "2"))
    level_tolerance_pct: float = float(os.getenv("LEVEL_TOLERANCE_PCT", "0.5"))
    order_size: float = float(os.getenv("ORDER_SIZE", "2000"))
    target_profit_usd: float = float(os.getenv("TARGET_PROFIT_USD", "4.0"))
    target_pnl_pct: float = float(os.getenv("TARGET_PNL_PCT", "2.5"))
    max_loss_pct: float = float(os.getenv("MAX_LOSS_PCT", "1.5"))
    check_interval: int = int(os.getenv("CHECK_INTERVAL", "5"))
    paper_trade: bool = os.getenv("PAPER_TRADE", "true").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # ------------------------------------------------------------------
    # Trader V2: higher-timeframe confirmed-entry strategy
    # ------------------------------------------------------------------

    # Fixed low leverage while V2 is being validated. Leverage does not define
    # the stop distance; structure and ATR do.
    v2_leverage: int = int(os.getenv("V2_LEVERAGE", "3"))

    # Dollar risk sizing. Notional = risk_usd / stop_distance_pct, then capped.
    v2_risk_per_trade_usd: float = float(os.getenv("V2_RISK_PER_TRADE_USD", "4"))
    v2_max_position_notional_usd: float = float(os.getenv("V2_MAX_POSITION_NOTIONAL_USD", "500"))
    v2_min_position_notional_usd: float = float(os.getenv("V2_MIN_POSITION_NOTIONAL_USD", "10"))
    # Additional cap: max isolated margin allocated to one trade as a fraction
    # of account value. 0.10 = at most 10% of account value as position margin.
    v2_max_margin_fraction: float = float(os.getenv("V2_MAX_MARGIN_FRACTION", "0.10"))

    # 4h + 1h regime and 30m/1h/4h structure quality gates.
    v2_min_trend_confidence: float = float(os.getenv("V2_MIN_TREND_CONFIDENCE", "70"))
    v2_min_level_strength: float = float(os.getenv("V2_MIN_LEVEL_STRENGTH", "65"))
    v2_min_level_timeframes: int = int(os.getenv("V2_MIN_LEVEL_TIMEFRAMES", "2"))
    v2_max_level_distance_pct: float = float(os.getenv("V2_MAX_LEVEL_DISTANCE_PCT", "3.0"))

    # 30m rejection confirmation and safer pullback entry.
    v2_confirmation_zone_pct: float = float(os.getenv("V2_CONFIRMATION_ZONE_PCT", "0.35"))
    v2_entry_pullback_atr: float = float(os.getenv("V2_ENTRY_PULLBACK_ATR", "0.15"))

    # Structural stop: beyond the S/R zone by at least an ATR buffer. Minimum
    # and maximum distances prevent noise-tight stops and absurdly-wide stops.
    v2_sl_atr_mult: float = float(os.getenv("V2_SL_ATR_MULT", "0.75"))
    v2_sl_zone_buffer_pct: float = float(os.getenv("V2_SL_ZONE_BUFFER_PCT", "0.35"))
    v2_min_stop_distance_pct: float = float(os.getenv("V2_MIN_STOP_DISTANCE_PCT", "0.60"))
    v2_max_stop_distance_pct: float = float(os.getenv("V2_MAX_STOP_DISTANCE_PCT", "2.50"))

    # Higher hit-rate target: modest fixed R multiple, but never through a
    # nearer opposing S/R zone.
    v2_target_risk_reward: float = float(os.getenv("V2_TARGET_RISK_REWARD", "1.50"))
    v2_tp_zone_buffer_pct: float = float(os.getenv("V2_TP_ZONE_BUFFER_PCT", "0.15"))

    # Prevent immediate revenge/re-entry after any position exit.
    v2_symbol_cooldown_minutes: float = float(os.getenv("V2_SYMBOL_COOLDOWN_MINUTES", "30"))

    # ------------------------------------------------------------------
    # Legacy V1 TP/SL settings (kept for rollback and old tests)
    # ------------------------------------------------------------------
    sl_zone_buffer_pct: float = float(os.getenv("SL_ZONE_BUFFER_PCT", "0.25"))
    tp_zone_buffer_pct: float = float(os.getenv("TP_ZONE_BUFFER_PCT", "0.15"))
    min_risk_reward: float = float(os.getenv("MIN_RISK_REWARD", "1.5"))
    tp_sl_sync_min_change_pct: float = float(os.getenv("TP_SL_SYNC_MIN_CHANGE_PCT", "0.1"))

    # Legacy V1 limit-order settings. V2 ignores ORDER_TTL_HOURS and expires
    # each pending entry at the next 30m candle instead.
    order_ttl_hours: float = float(os.getenv("ORDER_TTL_HOURS", "2"))
    level_refresh_seconds: int = int(os.getenv("LEVEL_REFRESH_SECONDS", "300"))
    doji_check_seconds: int = int(os.getenv("DOJI_CHECK_SECONDS", "300"))
    cooldown_seconds: int = int(os.getenv("COOLDOWN_SECONDS", "60"))

    # Telegram notifications (optional)
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
