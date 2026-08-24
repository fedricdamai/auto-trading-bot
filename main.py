#!/usr/bin/env python3
"""Auto Trading Bot.

Hyperliquid defaults to Trader V2:
4h regime -> 1h structure -> 30m confirmation, with strict order cleanup and
risk-sized positions. Set TRADER_VERSION=v1 for deliberate rollback.
"""

import logging
import sys

from bot.config import Config


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
    from bot.exchange import Exchange
    return Exchange(config)


def build_trader(config: Config, exchange, notifier=None):
    # V2 currently targets the Hyperliquid order lifecycle. Keep V1 available
    # for rollback and for the generic ccxt adapter.
    if config.exchange_backend == "hyperliquid" and config.trader_version == "v2":
        from bot.trader_v2 import Trader
        return Trader(config, exchange, notifier)

    from bot.trader import Trader
    return Trader(config, exchange, notifier)


def build_telegram_bot(config: Config):
    """Use exchange-sourced emergency controls with Hyperliquid Trader V2."""
    if config.exchange_backend == "hyperliquid" and config.trader_version == "v2":
        from bot.telegram_notifier_v2 import TelegramBot
    else:
        from bot.telegram_notifier import TelegramBot
    return TelegramBot(config.tg_bot_token, config.tg_chat_id, config.telegram_whitelist)


def main():
    config = Config()
    setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    backend = config.exchange_backend.upper()
    symbol = config.hl_symbol if config.exchange_backend == "hyperliquid" else config.symbol
    mode = "PAPER" if config.paper_trade else "LIVE"
    engine = config.trader_version.upper() if config.exchange_backend == "hyperliquid" else "V1"

    logger.info("=" * 60)
    logger.info("  Auto Trading Bot")
    logger.info("=" * 60)
    logger.info(f"  Backend:     {backend}")
    logger.info(f"  Engine:      {engine}")
    logger.info(f"  Symbol:      {symbol}")
    logger.info(f"  Timeframe:   {config.timeframe}")
    if config.exchange_backend == "hyperliquid":
        if config.trader_version == "v2":
            logger.info("  Strategy:    4h regime -> 1h structure -> 30m confirmation")
            logger.info(f"  Leverage:    fixed {config.v2_leverage}x isolated")
            logger.info(f"  Risk/trade:  ${config.v2_risk_per_trade_usd:.2f}")
            logger.info(f"  Max notional:${config.v2_max_position_notional_usd:.2f}")
        else:
            logger.info(f"  Leverage:    dynamic {config.hl_leverage_min}x-{config.hl_leverage_max}x")
        if config.hl_multi_symbol:
            logger.info(f"  Multi-sym:   ON ({config.hl_symbols})")
        else:
            logger.info(f"  Multi-sym:   OFF (single: {config.hl_symbol})")
    logger.info(f"  Mode:        {mode}")
    logger.info(f"  Telegram:    {'ON' if config.telegram_enabled else 'OFF'}")
    logger.info("=" * 60)

    exchange = build_exchange(config)

    tg_bot = None
    if config.telegram_enabled:
        tg_bot = build_telegram_bot(config)
        logger.info("Telegram bot enabled")

    trader = build_trader(config, exchange, tg_bot)

    if tg_bot:
        tg_bot.set_trader(trader)
        tg_log_handler = tg_bot.get_log_handler()
        logging.getLogger().addHandler(tg_log_handler)
        tg_bot.start_command_listener()
        logger.info("Telegram commands active — /logs to control log streaming")

    trader.run_loop()


if __name__ == "__main__":
    main()
