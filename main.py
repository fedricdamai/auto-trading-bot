#!/usr/bin/env python3
"""Auto Trading Bot - Buys at support and resistance levels.

Supports Hyperliquid (perps, 1x leverage) and ccxt exchanges.
Optional Telegram notifications for trade alerts.
"""

import logging
import sys

from bot.config import Config
from bot.trader import Trader
from bot.telegram_notifier import TelegramNotifier


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("bot.log"),
        ],
    )


def build_exchange(config: Config):
    if config.exchange_backend == "hyperliquid":
        from bot.hyperliquid_exchange import HyperliquidExchange
        return HyperliquidExchange(config)
    else:
        from bot.exchange import Exchange
        return Exchange(config)


def main():
    config = Config()
    setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    backend = config.exchange_backend.upper()
    symbol = config.hl_symbol if config.exchange_backend == "hyperliquid" else config.symbol
    mode = "PAPER" if config.paper_trade else "LIVE"

    logger.info("=" * 60)
    logger.info("  Auto Trading Bot - Support & Resistance Levels")
    logger.info("=" * 60)
    logger.info(f"  Backend:     {backend}")
    logger.info(f"  Symbol:      {symbol}")
    logger.info(f"  Timeframe:   {config.timeframe}")
    logger.info(f"  Leverage:    {config.hl_leverage}x" if config.exchange_backend == "hyperliquid" else "")
    logger.info(f"  Order size:  {config.order_size} (quote)")
    logger.info(f"  Mode:        {mode}")
    logger.info(f"  Stop loss:   {config.stop_loss_pct}%")
    logger.info(f"  Take profit: {config.take_profit_pct}%")
    logger.info(f"  Telegram:    {'ON' if config.telegram_enabled else 'OFF'}")
    logger.info("=" * 60)

    exchange = build_exchange(config)

    notifier = None
    if config.telegram_enabled:
        notifier = TelegramNotifier(config.tg_bot_token, config.tg_chat_id)
        logger.info("Telegram notifications enabled")

    trader = Trader(config, exchange, notifier)
    trader.run_loop()


if __name__ == "__main__":
    main()
