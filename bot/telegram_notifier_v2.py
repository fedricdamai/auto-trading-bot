"""Telegram controls for Trader V2.

The legacy Telegram /closeall command trusts the trader's in-memory dictionaries.
That is unsafe after restarts, failed syncs, or earlier versions that left orphan
orders on Hyperliquid. V2 treats the exchange as the source of truth.

V2 also sends a periodic health digest so long periods without a qualifying
setup do not look like a dead bot.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from bot.telegram_notifier import TelegramBot as BaseTelegramBot

logger = logging.getLogger(__name__)


def _account_order_symbols(exchange) -> set[str]:
    """Discover symbols with live Hyperliquid orders, even if config changed.

    Older bot versions may have left orders on a symbol that is no longer in
    HL_SYMBOLS. The V2 emergency close must still find and clean those orders.
    """
    try:
        if hasattr(exchange, "info") and hasattr(exchange, "address"):
            orders = exchange.info.frontend_open_orders(exchange.address)
            return {o.get("coin") for o in orders if o.get("coin")}
    except Exception as exc:
        logger.warning("Could not discover account-wide open-order symbols: %s", exc)
    return set()


def _managed_symbols(trader, exchange_positions: list[dict], exchange=None) -> list[str]:
    """Return every symbol the bot may need to flatten.

    Include configured symbols, locally-known state, exchange positions, and
    account-wide live orders so /closeall works even when local memory is stale.
    """
    symbols = set(trader._get_symbols())
    symbols.update(getattr(trader, "positions", {}).keys())
    symbols.update(getattr(trader, "pending_orders", {}).keys())
    symbols.update(
        p.get("coin") for p in exchange_positions
        if p.get("coin")
    )
    if exchange is not None:
        symbols.update(_account_order_symbols(exchange))
    return sorted(symbols)


def _cancel_and_snapshot(exchange, symbol: str, attempts: int = 4) -> tuple[int, list[dict], list[dict]]:
    """Cancel all orders for a symbol and verify what remains.

    cancel_orders_for_symbol historically returned the number of attempted
    cancellations even when some cancellations failed. Never trust that
    return value as proof. Always read exchange state again.
    """
    exchange.switch_symbol(symbol)
    first_normal = exchange.get_open_orders_for_symbol(symbol)
    first_triggers = exchange.get_trigger_orders_for_symbol(symbol)
    initial_count = len(first_normal) + len(first_triggers)

    normal = first_normal
    triggers = first_triggers
    for _ in range(attempts):
        if not normal and not triggers:
            break
        try:
            exchange.cancel_orders_for_symbol(symbol)
        except Exception as exc:
            logger.warning("[%s] broad cancel failed during /closeall: %s", symbol, exc)
        time.sleep(0.25)
        normal = exchange.get_open_orders_for_symbol(symbol)
        triggers = exchange.get_trigger_orders_for_symbol(symbol)

    return initial_count, normal, triggers


def flatten_exchange_state(trader) -> dict:
    """Pause the bot and flatten actual Hyperliquid state for managed symbols.

    This intentionally does not rely on trader.positions or pending_orders as
    the source of truth. It queries Hyperliquid, cancels stale entries and
    triggers, closes the actual exchange size, then verifies both position and
    order state before reporting success.
    """
    trader.paused = True

    result = {
        "closed": 0,
        "cancelled": 0,
        "clean_symbols": [],
        "failures": {},
    }

    if trader.config.paper_trade:
        result["closed"] = len(getattr(trader, "positions", {}))
        result["cancelled"] = len(getattr(trader, "pending_orders", {}))
        trader.positions.clear()
        trader.pending_orders.clear()
        result["clean_symbols"] = list(trader._get_symbols())
        return result

    exchange = trader.exchange
    try:
        exchange_positions = exchange.get_all_positions()
    except Exception as exc:
        result["failures"]["account"] = f"could not read positions: {exc}"
        return result

    symbols = _managed_symbols(trader, exchange_positions, exchange)
    primary = getattr(trader, "primary_symbol", trader.config.hl_symbol)

    for symbol in symbols:
        try:
            exchange.switch_symbol(symbol)

            initial_orders, normal, triggers = _cancel_and_snapshot(exchange, symbol)
            result["cancelled"] += initial_orders

            # Non-trigger entries are the dangerous case. Closing a position
            # while a stale non-reduce-only entry remains could reopen risk.
            if normal:
                raise RuntimeError(
                    f"{len(normal)} non-trigger order(s) still live after cancellation; "
                    "refusing market close until they are gone"
                )

            # Trigger orders are reduce-only, but still try an explicit trigger
            # cleanup before the market close. They cannot safely be assumed
            # cancelled until the exchange read confirms it.
            if triggers:
                try:
                    exchange.cancel_trigger_orders_for_symbol(symbol)
                except Exception as exc:
                    logger.warning("[%s] trigger cleanup failed: %s", symbol, exc)
                time.sleep(0.25)

            actual = exchange.get_position(symbol)
            if actual and actual.get("size", 0) > 0:
                exchange.place_market_close(actual["size"], side=actual["side"])
                for _ in range(8):
                    time.sleep(0.25)
                    if not exchange.get_position(symbol):
                        break
                if exchange.get_position(symbol):
                    raise RuntimeError("market close could not be verified")
                result["closed"] += 1

            # A trigger may survive or appear during a close race. Clean the
            # symbol again after the position is flat and verify zero orders.
            _, remaining_normal, remaining_triggers = _cancel_and_snapshot(exchange, symbol)
            if remaining_normal or remaining_triggers:
                raise RuntimeError(
                    f"final cleanup failed: entries={len(remaining_normal)} "
                    f"triggers={len(remaining_triggers)}"
                )
            if exchange.get_position(symbol):
                raise RuntimeError("position reappeared during final verification")

            trader.pending_orders.pop(symbol, None)
            trader.positions.pop(symbol, None)
            result["clean_symbols"].append(symbol)
            logger.warning("[%s] /closeall verified FLAT and order-clean", symbol)

        except Exception as exc:
            result["failures"][symbol] = str(exc)
            logger.exception("[%s] /closeall could not verify clean state", symbol)
            if hasattr(trader, "blocked_symbols"):
                trader.blocked_symbols.add(symbol)

    try:
        exchange.switch_symbol(primary)
    except Exception:
        pass
    return result


class TelegramBot(BaseTelegramBot):
    """V2 Telegram bot whose emergency controls use exchange truth."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_send_error: str | None = None

    def send(self, message: str):
        """Best-effort Telegram delivery that can never stop the trading loop."""
        try:
            super().send(message)
            self._last_send_error = None
            return True
        except Exception as exc:
            # Do not log here. The root logger itself forwards to Telegram, so
            # logging a Telegram send failure from this method can recurse.
            self._last_send_error = f"{type(exc).__name__}: {exc}"
            return False

    def notify_startup(
        self,
        symbol: str,
        timeframe: str,
        paper: bool,
        leverage: int = 1,
        mainnet: bool = False,
        scan_seconds: int = 60,
        heartbeat_seconds: int = 600,
    ):
        if paper:
            mode = "PAPER"
        else:
            mode = "LIVE MAINNET" if mainnet else "LIVE TESTNET"
        symbols = "BTC, ETH, SOL, HYPE, BNB"
        self.send(
            f"<b>Trading Bot Online [{mode}]</b>\n"
            f"Engine: <code>V2</code>\n"
            f"Universe: <code>{symbols}</code>\n"
            f"Strategy: <code>4h + 1h regime / 30m-4h structure</code>\n"
            f"Leverage: <code>{leverage}x isolated</code>\n"
            f"HTF scan: <code>every {scan_seconds}s</code>\n"
            f"Heartbeat: <code>every {max(1, heartbeat_seconds // 60)}m</code>\n\n"
            "I will message even when no setup qualifies so you can verify the bot is alive."
        )

    def notify_heartbeat(self, snapshot: dict):
        state = "PAUSED" if snapshot.get("paused") else "RUNNING"
        rows = snapshot.get("rows", [])
        lines = []
        for row in rows:
            symbol = html.escape(str(row.get("symbol", "?")))
            regime = html.escape(str(row.get("regime", "unknown")))
            status = html.escape(str(row.get("status", "no setup")))
            lines.append(f"<code>{symbol}</code> {regime} | {status}")

        body = "\n".join(lines) or "No symbol data yet."
        ok = self.send(
            f"<b>Bot Heartbeat [{html.escape(str(snapshot.get('mode', '?')))}]</b>\n"
            f"Status: <code>{state}</code> | "
            f"Positions: <code>{snapshot.get('positions', 0)}</code> | "
            f"Pending: <code>{snapshot.get('pending', 0)}</code> | "
            f"Blocked: <code>{snapshot.get('blocked', 0)}</code>\n"
            f"Scanning HTF history every <code>{snapshot.get('scan_interval_seconds', '?')}s</code>\n\n"
            f"{body}"
        )
        return bool(ok)

    async def _cmd_closeall(self, update, context):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        await update.message.reply_text(
            "<b>CLOSE ALL requested</b>\n"
            "Bot paused. Reading Hyperliquid directly, cancelling orders, and flattening positions...",
            parse_mode="HTML",
        )

        result = await asyncio.to_thread(flatten_exchange_state, self.trader)
        failures = result["failures"]
        clean = result["clean_symbols"]

        if failures:
            failure_lines = "\n".join(
                f"• <code>{html.escape(str(sym))}</code>: {html.escape(str(err))}"
                for sym, err in failures.items()
            )
            await update.message.reply_text(
                "<b>CLOSE ALL incomplete</b>\n"
                f"Positions closed: <code>{result['closed']}</code>\n"
                f"Orders found/cancelled: <code>{result['cancelled']}</code>\n"
                f"Verified clean: <code>{', '.join(clean) or 'none'}</code>\n\n"
                f"<b>Needs manual review:</b>\n{failure_lines}\n\n"
                "Bot remains <b>PAUSED</b>. Do not /resume until Hyperliquid is clean.",
                parse_mode="HTML",
            )
            return

        await update.message.reply_text(
            "<b>Exchange verified clean</b>\n"
            f"Positions closed: <code>{result['closed']}</code>\n"
            f"Orders found/cancelled: <code>{result['cancelled']}</code>\n"
            f"Symbols verified: <code>{', '.join(clean) or 'none'}</code>\n\n"
            "Bot is <b>PAUSED</b>. Use /resume only when you intentionally want new trades.",
            parse_mode="HTML",
        )

    async def _cmd_stop(self, update, context):
        """Do not trust only local dictionaries before stopping V2."""
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        force = bool(context.args and context.args[0].lower() == "force")
        if not force and not self.trader.config.paper_trade:
            exchange_positions = self.trader.exchange.get_all_positions()
            symbols = _managed_symbols(self.trader, exchange_positions, self.trader.exchange)
            dirty = []
            for sym in symbols:
                self.trader.exchange.switch_symbol(sym)
                pos = self.trader.exchange.get_position(sym)
                normal = self.trader.exchange.get_open_orders_for_symbol(sym)
                triggers = self.trader.exchange.get_trigger_orders_for_symbol(sym)
                if pos or normal or triggers:
                    dirty.append(
                        f"{sym}: pos={'yes' if pos else 'no'}, "
                        f"entries={len(normal)}, triggers={len(triggers)}"
                    )
            self.trader.exchange.switch_symbol(
                getattr(self.trader, "primary_symbol", self.trader.config.hl_symbol)
            )
            if dirty:
                await update.message.reply_text(
                    "<b>STOP blocked: exchange is not clean.</b>\n"
                    + "\n".join(f"<code>{html.escape(line)}</code>" for line in dirty)
                    + "\n\nUse /closeall first, or /stop force if you intentionally want orders/positions left live.",
                    parse_mode="HTML",
                )
                return

        self.trader.running = False
        await update.message.reply_text(
            "<b>Bot STOPPING...</b>\nRestart: <code>sudo systemctl restart auto-trading-bot</code>",
            parse_mode="HTML",
        )
