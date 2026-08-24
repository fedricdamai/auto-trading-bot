#!/usr/bin/env python3
"""Auto Trading Bot.

Hyperliquid defaults to Trader V2:
reaction-based 30m/1h support and resistance, trend as context, structural
TP/SL, strict one-family-per-symbol execution, repair-only protection, and
persistent trade lifecycle IDs. Set TRADER_VERSION=v1 for deliberate rollback.
"""

import logging
import sys

from bot.config import Config


class _RuntimeNoiseFilter(logging.Filter):
    """Keep infrastructure chatter out of the interactive console/Telegram.

    Full detail still goes to bot.log. Strategy decisions, fills,
    cancellations, warnings and meaningful errors remain visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.name.startswith("httpx") or record.name.startswith("httpcore"):
            return False
        if message.startswith("V2 Tick:"):
            return False

        if record.name == "bot.hyperliquid_exchange" and (
            message.startswith("Switched symbol:")
            or " szDecimals=" in message
            or message.startswith("Leverage set to ")
        ):
            return False

        if record.name.startswith("telegram.ext.Updater") and "polling" in message.lower():
            return False

        return True


def setup_logging(level: str):
    console = logging.StreamHandler(sys.stdout)
    console.addFilter(_RuntimeNoiseFilter())

    file_handler = logging.FileHandler("bot.log")

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[console, file_handler],
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_exchange(config: Config):
    if config.exchange_backend == "hyperliquid":
        from bot.hyperliquid_exchange import HyperliquidExchange
        return HyperliquidExchange(config)
    from bot.exchange import Exchange
    return Exchange(config)


def build_trader(config: Config, exchange, notifier=None):
    # Production V2 keeps one immutable identity for each active symbol until
    # that trade lifecycle ends.
    if config.exchange_backend == "hyperliquid" and config.trader_version == "v2":
        from bot.trader_v2_production import Trader
        return Trader(config, exchange, notifier)

    from bot.trader import Trader
    return Trader(config, exchange, notifier)


def build_telegram_bot(config: Config):
    """Use exchange-sourced controls and trade-ID notifications for V2."""
    if config.exchange_backend == "hyperliquid" and config.trader_version == "v2":
        from bot.telegram_notifier_tracked import TelegramBot
    else:
        from bot.telegram_notifier import TelegramBot
    return TelegramBot(config.tg_bot_token, config.tg_chat_id, config.telegram_whitelist)


def main():
    config = Config()
    setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    if config.exchange_backend == "hyperliquid" and config.trader_version == "v2":
        from bot.runtime_lock import acquire_single_instance_lock
        try:
            acquire_single_instance_lock()
        except RuntimeError as exc:
            logger.critical(f"Trader V2 startup refused: {exc}")
            raise SystemExit(2) from exc

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
            logger.info("  Strategy:    reaction S/R first, HTF trend as context")
            logger.info("  Orders:      one grouped entry + reduce-only TP/SL per symbol")
            logger.info("  Protection:  repair-only watchdog, never auto-flatten")
            logger.info("  Tracking:    persistent trade ID + lifecycle event journal")
            logger.info(f"  Leverage:    fixed {config.v2_leverage}x isolated")
            logger.info(f"  Risk/trade:  ${config.v2_risk_per_trade_usd:.2f}")
            logger.info(f"  Max notional:${config.v2_max_position_notional_usd:.2f}")
            logger.info(
                f"  Decisions:   re-evaluate S/R history every "
                f"{config.v2_signal_scan_interval_seconds}s"
            )
            logger.info(
                f"  Heartbeat:   Telegram every "
                f"{config.v2_heartbeat_interval_seconds // 60}m"
            )
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
        tg_log_handler.addFilter(_RuntimeNoiseFilter())
        logging.getLogger().addHandler(tg_log_handler)
        tg_bot.start_command_listener()
        logger.info("Telegram commands active: /logs to control log streaming")

        if config.exchange_backend == "hyperliquid" and config.trader_version == "v2":
            tg_bot.notify_startup(
                symbol,
                config.timeframe,
                config.paper_trade,
                leverage=config.v2_leverage,
                mainnet=config.hl_mainnet,
                scan_seconds=config.v2_signal_scan_interval_seconds,
                heartbeat_seconds=config.v2_heartbeat_interval_seconds,
            )
        else:
            tg_bot.notify_startup(
                symbol,
                config.timeframe,
                config.paper_trade,
                leverage=config.hl_leverage,
                mainnet=config.hl_mainnet,
            )

    trader.run_loop()


if __name__ == "__main__":
    main()
