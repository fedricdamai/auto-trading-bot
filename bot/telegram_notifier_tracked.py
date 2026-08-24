"""Telegram V2 notifications and lifecycle lookup with persistent trade IDs."""

from __future__ import annotations

import asyncio
import html

from telegram.ext import Application, CommandHandler

from bot.telegram_notifier_v2 import TelegramBot as V2TelegramBot


class TelegramBot(V2TelegramBot):
    """Add the current trade ID to order, fill, cancel and exit messages."""

    def _trade_id_for_level_type(self, level_type: str) -> str:
        if not self.trader:
            return ""
        symbol = str(level_type or "").strip().split(" ", 1)[0]
        if not symbol:
            return ""
        getter = getattr(self.trader, "get_trade_id", None)
        if not getter:
            return ""
        try:
            return str(getter(symbol) or "")
        except Exception:
            return ""

    @staticmethod
    def _id_line(trade_id: str) -> str:
        if not trade_id:
            return ""
        return f"Trade ID: <code>{html.escape(trade_id)}</code>\n"

    def notify_limit_order(self, side: str, level: float, level_type: str,
                           quantity: float, sl: float, tp: float,
                           leverage: int, strength: float):
        label = "LIMIT BUY" if side == "long" else "LIMIT SELL"
        trade_id = self._trade_id_for_level_type(level_type)
        self.send(
            f"<b>{label} at {html.escape(level_type.upper())}</b>\n"
            f"{self._id_line(trade_id)}"
            f"Level: <code>{level:.2f}</code>\n"
            f"Entry: <code>{level:.2f}</code>\n"
            f"Size: <code>{quantity:.6f}</code>\n"
            f"SL: <code>{sl:.2f}</code> | TP: <code>{tp:.2f}</code>\n"
            f"Leverage: <code>{leverage}x</code>\n"
            f"Confidence: <code>{strength:.1f}</code>"
        )

    def notify_entry(self, side: str, level: float, level_type: str, price: float,
                     quantity: float, sl: float, tp: float, leverage: int):
        label = "LONG" if side == "long" else "SHORT"
        trade_id = self._trade_id_for_level_type(level_type)
        self.send(
            f"<b>{label} ENTRY at {html.escape(level_type.upper())}</b>\n"
            f"{self._id_line(trade_id)}"
            f"Level: <code>{level:.2f}</code>\n"
            f"Entry: <code>{price:.2f}</code>\n"
            f"Size: <code>{quantity:.6f}</code>\n"
            f"SL: <code>{sl:.2f}</code> | TP: <code>{tp:.2f}</code>\n"
            f"Leverage: <code>{leverage}x</code>"
        )

    def notify_exit(self, reason: str, level: float, level_type: str, price: float,
                    entry: float = 0, leverage: int = 1, side: str = "long"):
        label = "STOP LOSS" if reason == "stop_loss" else (
            "TAKE PROFIT" if reason == "take_profit" else "POSITION EXIT"
        )
        trade_id = self._trade_id_for_level_type(level_type)
        msg = (
            f"<b>{label} ({side.upper()})</b>\n"
            f"{self._id_line(trade_id)}"
            f"Level: <code>{level:.2f}</code> ({html.escape(level_type)})\n"
            f"Exit price: <code>{price:.2f}</code>"
        )
        if entry > 0:
            if side == "long":
                price_pnl = (price - entry) / entry * 100
            else:
                price_pnl = (entry - price) / entry * 100
            margin_pnl = price_pnl * leverage
            sign = "+" if margin_pnl >= 0 else ""
            msg += f"\nMargin PnL: <code>{sign}{margin_pnl:.2f}%</code> ({leverage}x)"
        self.send(msg)

    def notify_order_cancelled(self, side: str, level: float, level_type: str):
        label = "BUY" if side == "long" else "SELL"
        trade_id = self._trade_id_for_level_type(level_type)
        self.send(
            f"<b>ORDER CANCELLED</b>\n"
            f"{self._id_line(trade_id)}"
            f"Limit {label} at {html.escape(level_type)} <code>{level:.2f}</code>"
        )

    # ------------------------------------------------------------------
    # /trade audit command
    # ------------------------------------------------------------------

    @staticmethod
    def _format_event(row: dict) -> str:
        event = str(row.get("event", "?"))
        details = row.get("details", {}) or {}
        timestamp = str(row.get("timestamp_utc", ""))
        clock = timestamp[11:19] if len(timestamp) >= 19 else ""

        if event == "TRIGGER_CREATED":
            return (
                f"{clock} <b>TRIGGER</b> | {html.escape(str(details.get('kind', '?')))} "
                f"<code>{details.get('level_price', '?')}</code> | "
                f"str {details.get('strength', '?')} | "
                f"trend {html.escape(str(details.get('trend_direction', '?')))}"
            )
        if event == "ORDER_PENDING":
            return (
                f"{clock} <b>ORDER</b> | entry <code>{details.get('entry', '?')}</code> | "
                f"qty <code>{details.get('quantity', '?')}</code> | oid <code>{details.get('exchange_oid', '?')}</code>"
            )
        if event == "FILLED":
            return (
                f"{clock} <b>FILLED</b> | <code>{details.get('actual_entry', '?')}</code> | "
                f"qty <code>{details.get('actual_quantity', '?')}</code>"
            )
        if event in ("PROTECTION_ACTIVE", "PROTECTION_REPAIRED"):
            label = "TP/SL" if event == "PROTECTION_ACTIVE" else "TP/SL REPAIRED"
            return (
                f"{clock} <b>{label}</b> | TP <code>{details.get('tp', '?')}</code> | "
                f"SL <code>{details.get('sl', '?')}</code>"
            )
        if event in ("PROTECTION_REPAIR_PENDING", "FILL_PROTECTION_PENDING"):
            return (
                f"{clock} <b>PROTECTION PENDING</b> | "
                f"{html.escape(str(details.get('reason', 'retrying')))}"
            )
        if event == "ORDER_CANCELLED":
            return f"{clock} <b>CANCELLED</b> | {html.escape(str(details.get('reason', '?')))}"
        if event == "EXIT":
            pnl = details.get("pnl_usd", "?")
            return (
                f"{clock} <b>{html.escape(str(details.get('outcome', 'EXIT')))}</b> | "
                f"{html.escape(str(details.get('reason', '?')))} | "
                f"exit <code>{details.get('exit', '?')}</code> | PnL <code>${pnl}</code>"
            )
        return f"{clock} {html.escape(event)}"

    async def _cmd_trade(self, update, context):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        if not context.args:
            active = []
            for symbol in sorted(set(self.trader.positions) | set(self.trader.pending_orders)):
                trade_id = self.trader.get_trade_id(symbol)
                if trade_id:
                    active.append(f"<code>{symbol}</code>: <code>{html.escape(trade_id)}</code>")
            body = "\n".join(active) if active else "No active tracked trades."
            await update.message.reply_text(
                "<b>Trade Audit</b>\n"
                "Use <code>/trade TRADE_ID</code> or <code>/trade SYMBOL</code>.\n\n"
                f"{body}",
                parse_mode="HTML",
            )
            return

        query = str(context.args[0]).strip()
        symbol_query = query.upper()
        if symbol_query in set(self.trader._get_symbols()):
            trade_id = self.trader.get_trade_id(symbol_query)
        else:
            trade_id = query

        if not trade_id:
            await update.message.reply_text("No tracked trade found for that symbol.")
            return

        rows = self.trader.lifecycle.load_trade(trade_id)
        if not rows:
            await update.message.reply_text(
                f"No lifecycle events found for <code>{html.escape(trade_id)}</code>.",
                parse_mode="HTML",
            )
            return

        first = rows[0]
        symbol = html.escape(str(first.get("symbol", "?")))
        side = html.escape(str(first.get("side", "?")).upper())
        lines = [
            f"<b>Trade Audit: {symbol} {side}</b>",
            f"ID: <code>{html.escape(trade_id)}</code>",
            "",
        ]
        for row in rows[-12:]:
            lines.append(self._format_event(row))

        await update.message.reply_text("\n".join(lines)[:3900], parse_mode="HTML")

    async def _polling_loop(self):
        """Register the standard controls plus the V2 /trade audit command."""
        app = Application.builder().token(self.token).build()

        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("levels", self._cmd_levels))
        app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        app.add_handler(CommandHandler("config", self._cmd_config))
        app.add_handler(CommandHandler("risk", self._cmd_risk))
        app.add_handler(CommandHandler("settp", self._cmd_settp))
        app.add_handler(CommandHandler("setsl", self._cmd_setsl))
        app.add_handler(CommandHandler("setlev", self._cmd_setlev))
        app.add_handler(CommandHandler("learn", self._cmd_learn))
        app.add_handler(CommandHandler("journal", self._cmd_journal))
        app.add_handler(CommandHandler("scan", self._cmd_scan))
        app.add_handler(CommandHandler("orders", self._cmd_orders))
        app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        app.add_handler(CommandHandler("pause", self._cmd_pause))
        app.add_handler(CommandHandler("resume", self._cmd_resume))
        app.add_handler(CommandHandler("stop", self._cmd_stop))
        app.add_handler(CommandHandler("closeall", self._cmd_closeall))
        app.add_handler(CommandHandler("set", self._cmd_set))
        app.add_handler(CommandHandler("logs", self._cmd_logs))
        app.add_handler(CommandHandler("trade", self._cmd_trade))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        stop_event = asyncio.Event()
        await stop_event.wait()
