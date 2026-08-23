import logging
import asyncio
import threading
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram bot with commands and notifications."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.bot = Bot(token=token)
        self.trader = None  # set after trader is created
        self._app = None
        self._loop = None

    def set_trader(self, trader):
        self.trader = trader

    # ── Sending messages ──

    def send(self, message: str):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._async_send(message))
            else:
                loop.run_until_complete(self._async_send(message))
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._async_send(message))

    async def _async_send(self, message: str):
        async with Bot(self.token) as bot:
            await bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
            )

    # ── Notification helpers ──

    def notify_startup(self, symbol: str, timeframe: str, paper: bool):
        mode = "PAPER" if paper else "LIVE"
        self.send(
            f"<b>Bot Started [{mode}]</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Timeframe: <code>{timeframe}</code>\n\n"
            f"Commands:\n"
            f"/status - Bot status & open positions\n"
            f"/levels - Current S/R levels\n"
            f"/orders - Pending limit orders\n"
            f"/pnl - Position P&L\n"
            f"/help - All commands"
        )

    def notify_levels(self, current_price: float, support: list, resistance: list):
        sup = "\n".join(f"  {s:.2f}" for s in support[:5]) or "  none"
        res = "\n".join(f"  {r:.2f}" for r in resistance[:5]) or "  none"
        self.send(
            f"<b>Levels Update</b>\n"
            f"Price: <code>{current_price:.2f}</code>\n\n"
            f"<b>Support:</b>\n<code>{sup}</code>\n\n"
            f"<b>Resistance:</b>\n<code>{res}</code>"
        )

    def notify_buy(self, level: float, level_type: str, price: float, quantity: float, sl: float, tp: float):
        self.send(
            f"<b>LIMIT BUY at {level_type.upper()}</b>\n"
            f"Level: <code>{level:.2f}</code>\n"
            f"Entry: <code>{price:.2f}</code>\n"
            f"Size: <code>{quantity:.6f}</code>\n"
            f"SL: <code>{sl:.2f}</code> | TP: <code>{tp:.2f}</code>"
        )

    def notify_exit(self, reason: str, level: float, level_type: str, price: float):
        label = "STOP LOSS" if reason == "stop_loss" else "TAKE PROFIT"
        self.send(
            f"<b>{label} HIT</b>\n"
            f"Level: <code>{level:.2f}</code> ({level_type})\n"
            f"Exit price: <code>{price:.2f}</code>"
        )

    def notify_error(self, error: str):
        self.send(f"<b>Error</b>\n<code>{error[:500]}</code>")

    # ── Command handlers ──

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "<b>Auto Trading Bot</b>\n\n"
            "Commands:\n"
            "/status - Bot status & positions\n"
            "/levels - Current support & resistance\n"
            "/orders - Pending limit orders\n"
            "/pnl - Position P&L\n"
            "/config - Current settings\n"
            "/help - This message",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._cmd_start(update, context)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        try:
            price = self.trader.exchange.get_ticker_price()
        except Exception:
            price = 0

        pending = len(self.trader.pending_orders)
        positions = len(self.trader.open_positions)
        levels = len(self.trader.known_levels)

        mode = "PAPER" if self.trader.config.paper_trade else "LIVE"

        await update.message.reply_text(
            f"<b>Bot Status [{mode}]</b>\n\n"
            f"Price: <code>{price:.2f}</code>\n"
            f"Levels tracked: <code>{levels}</code>\n"
            f"Pending orders: <code>{pending}</code>\n"
            f"Open positions: <code>{positions}</code>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_levels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.trader or not self.trader.known_levels:
            await update.message.reply_text("No levels detected yet. Wait for the next cycle.")
            return

        try:
            price = self.trader.exchange.get_ticker_price()
        except Exception:
            price = 0

        sorted_levels = sorted(self.trader.known_levels.values(), key=lambda l: l.strength, reverse=True)
        support = [l for l in sorted_levels if l.kind == "support"]
        resistance = [l for l in sorted_levels if l.kind == "resistance"]

        lines = [f"<b>S/R Levels</b>\nPrice: <code>{price:.2f}</code>\n"]

        if resistance:
            lines.append("<b>Resistance:</b>")
            for l in resistance[:5]:
                dist = abs(l.price - price) / price * 100
                lines.append(f"  <code>{l.price:.2f}</code> | str={l.strength} t={l.touches} ({dist:.1f}% away)")

        if support:
            lines.append("\n<b>Support:</b>")
            for l in support[:5]:
                dist = abs(l.price - price) / price * 100
                lines.append(f"  <code>{l.price:.2f}</code> | str={l.strength} t={l.touches} ({dist:.1f}% away)")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.trader or not self.trader.pending_orders:
            await update.message.reply_text("No pending orders.")
            return

        lines = ["<b>Pending Limit Orders</b>\n"]
        for i, o in enumerate(self.trader.pending_orders, 1):
            age_h = ((__import__("time").time() - o.placed_at) / 3600)
            lines.append(
                f"{i}. <code>{o.entry_price:.2f}</code> ({o.kind})\n"
                f"   SL: <code>{o.stop_loss:.2f}</code> | TP: <code>{o.take_profit:.2f}</code>\n"
                f"   Strength: {o.strength} | Age: {age_h:.1f}h"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.trader or not self.trader.open_positions:
            await update.message.reply_text("No open positions.")
            return

        try:
            price = self.trader.exchange.get_ticker_price()
        except Exception:
            await update.message.reply_text("Could not fetch current price.")
            return

        lines = ["<b>Open Positions</b>\n"]
        total_pnl = 0
        for i, pos in enumerate(self.trader.open_positions, 1):
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
            pnl_usd = pos.quantity * (price - pos.entry_price)
            total_pnl += pnl_usd
            sign = "+" if pnl_pct >= 0 else ""
            lines.append(
                f"{i}. Entry: <code>{pos.entry_price:.2f}</code> ({pos.kind})\n"
                f"   Now: <code>{price:.2f}</code> | PnL: <code>{sign}{pnl_pct:.2f}%</code> ({sign}{pnl_usd:.2f})\n"
                f"   SL: <code>{pos.stop_loss:.2f}</code> | TP: <code>{pos.take_profit:.2f}</code>"
            )

        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"\n<b>Total PnL: <code>{sign}{total_pnl:.2f}</code></b>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return
        c = self.trader.config
        symbol = c.hl_symbol if c.exchange_backend == "hyperliquid" else c.symbol
        mode = "PAPER" if c.paper_trade else "LIVE"
        await update.message.reply_text(
            f"<b>Config</b>\n\n"
            f"Mode: <code>{mode}</code>\n"
            f"Exchange: <code>{c.exchange_backend}</code>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Timeframe: <code>{c.timeframe}</code>\n"
            f"Order size: <code>{c.order_size}</code>\n"
            f"Max orders: <code>{c.max_open_orders}</code>\n"
            f"Order TTL: <code>{c.order_ttl_hours}h</code>\n"
            f"Leverage: <code>{c.hl_leverage}x</code>\n"
            f"Check interval: <code>{c.check_interval}s</code>",
            parse_mode=ParseMode.HTML,
        )

    # ── Run the command listener ──

    def start_command_listener(self):
        """Start the Telegram command listener in a background thread."""
        thread = threading.Thread(target=self._run_polling, daemon=True)
        thread.start()
        logger.info("Telegram command listener started")

    def _run_polling(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._polling_loop())

    async def _polling_loop(self):
        app = Application.builder().token(self.token).build()

        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("levels", self._cmd_levels))
        app.add_handler(CommandHandler("orders", self._cmd_orders))
        app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        app.add_handler(CommandHandler("config", self._cmd_config))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        # Keep running until the process exits
        stop_event = asyncio.Event()
        await stop_event.wait()


# Keep backward compat alias
TelegramNotifier = TelegramBot
