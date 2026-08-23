#!/usr/bin/env python3
"""Auto Trading Bot - Buys at support and resistance levels.

Supports Hyperliquid (perps, 1x leverage) and ccxt exchanges.
Control and monitor via Telegram commands.
"""

import logging
import sys

from bot.config import Config
from bot.trader import Trader
from bot.telegram_notifier import TelegramBot


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
    if config.exchange_backend == "hyperliquid":
        logger.info(f"  Leverage:    {config.hl_leverage}x")
        if config.hl_multi_symbol:
            if config.hl_symbols:
                logger.info(f"  Multi-sym:   ON ({config.hl_symbols})")
            else:
                logger.info(f"  Multi-sym:   ON (top {config.hl_scan_top_n} by volume)")
        else:
            logger.info(f"  Multi-sym:   OFF (single: {config.hl_symbol})")
    logger.info(f"  Order size:  {config.order_size} (quote)")
    logger.info(f"  Mode:        {mode}")
    logger.info(f"  Telegram:    {'ON' if config.telegram_enabled else 'OFF'}")
    logger.info("=" * 60)

    exchange = build_exchange(config)

    tg_bot = None
    if config.telegram_enabled:
        tg_bot = TelegramBot(config.tg_bot_token, config.tg_chat_id, config.telegram_whitelist)
        logger.info("Telegram bot enabled")

    trader = Trader(config, exchange, tg_bot)

    if tg_bot:
        tg_bot.set_trader(trader)
        tg_bot.start_command_listener()
        logger.info("Telegram commands active: /status /levels /orders /pnl /config /help")

    trader.run_loop()


if __name__ == "__main__":
    main()
