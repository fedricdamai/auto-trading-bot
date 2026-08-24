"""Telegram V2 notifications with persistent trade IDs."""

from __future__ import annotations

import html

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
