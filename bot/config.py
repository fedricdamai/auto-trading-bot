import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Exchange backend: "ccxt" or "hyperliquid"
    exchange_backend: str = os.getenv("EXCHANGE_BACKEND", "hyperliquid")

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

    # Dynamic leverage range (actual leverage per trade is computed from signal quality)
    hl_leverage_min: int = int(os.getenv("HL_LEVERAGE_MIN", "3"))
    hl_leverage_max: int = int(os.getenv("HL_LEVERAGE_MAX", "10"))
    hl_leverage: int = 10  # current active leverage, set dynamically per trade

    # Multi-symbol scanning
    hl_multi_symbol: bool = os.getenv("HL_MULTI_SYMBOL", "true").lower() == "true"
    hl_symbols: str = os.getenv("HL_SYMBOLS", "BTC,ETH,SOL,HYPE,BNB")
    hl_scan_top_n: int = int(os.getenv("HL_SCAN_TOP_N", "10"))
    hl_max_positions: int = int(os.getenv("HL_MAX_POSITIONS", "5"))

    # Trading settings (timeframe is fixed at 5m for scalping; multi-TF analysis uses 1m/5m/15m/1h/4h/1d internally)
    timeframe: str = "5m"
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

    # TP/SL positioning around S/R zones
    # SL sits this % beyond the level that justifies the trade (zone buffer)
    sl_zone_buffer_pct: float = float(os.getenv("SL_ZONE_BUFFER_PCT", "0.25"))
    # TP sits this % in front of the next opposing level
    tp_zone_buffer_pct: float = float(os.getenv("TP_ZONE_BUFFER_PCT", "0.15"))
    # Minimum reward-to-risk before a nearer level is skipped for a further one
    min_risk_reward: float = float(os.getenv("MIN_RISK_REWARD", "1.5"))
    # Only re-place exchange TP/SL triggers when a price moved at least this %
    tp_sl_sync_min_change_pct: float = float(os.getenv("TP_SL_SYNC_MIN_CHANGE_PCT", "0.1"))

    # Limit order settings
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
