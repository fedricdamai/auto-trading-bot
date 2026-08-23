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
    hl_leverage: int = int(os.getenv("HL_LEVERAGE", "3"))
    hl_max_leverage: int = int(os.getenv("HL_MAX_LEVERAGE", "5"))
    hl_mainnet: bool = os.getenv("HL_MAINNET", "false").lower() == "true"

    # Multi-symbol scanning
    hl_multi_symbol: bool = os.getenv("HL_MULTI_SYMBOL", "true").lower() == "true"
    hl_symbols: str = os.getenv("HL_SYMBOLS", "BTC,ETH,SOL,HYPE,BNB")
    hl_scan_top_n: int = int(os.getenv("HL_SCAN_TOP_N", "10"))
    hl_max_positions: int = int(os.getenv("HL_MAX_POSITIONS", "5"))

    # Shared trading settings
    timeframe: str = os.getenv("TIMEFRAME", "5m")
    lookback_candles: int = int(os.getenv("LOOKBACK_CANDLES", "200"))
    min_touches: int = int(os.getenv("MIN_TOUCHES", "2"))
    level_tolerance_pct: float = float(os.getenv("LEVEL_TOLERANCE_PCT", "0.5"))
    order_size: float = float(os.getenv("ORDER_SIZE", "50"))
    target_pnl_pct: float = float(os.getenv("TARGET_PNL_PCT", "1.0"))
    max_loss_pct: float = float(os.getenv("MAX_LOSS_PCT", "1.0"))
    check_interval: int = int(os.getenv("CHECK_INTERVAL", "5"))
    paper_trade: bool = os.getenv("PAPER_TRADE", "true").lower() == "true"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Limit order settings
    order_ttl_hours: float = float(os.getenv("ORDER_TTL_HOURS", "24"))
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
