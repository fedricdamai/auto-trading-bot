import logging
import asyncio
import threading
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

from bot.levels import compute_tp_sl

logger = logging.getLogger(__name__)


class TelegramBot:
    """Telegram bot with commands and notifications."""

    def __init__(self, token: str, chat_id: str, allowed_users: set[int] | None = None):
        self.token = token
        self.chat_id = chat_id
        self.bot = Bot(token=token)
        self.trader = None
        self.allowed_users = allowed_users or set()
        self._app = None
        self._loop = None

    def set_trader(self, trader):
        self.trader = trader

    def _is_authorized(self, update: Update) -> bool:
        user_id = update.effective_user.id
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

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

    def notify_startup(self, symbol: str, timeframe: str, paper: bool, leverage: int = 1, mainnet: bool = False):
        if paper:
            mode = "PAPER"
        else:
            mode = "LIVE MAINNET" if mainnet else "LIVE TESTNET"
        self.send(
            f"<b>Scalp Bot Started [{mode}]</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Timeframe: <code>{timeframe}</code>\n"
            f"Leverage: <code>{leverage}x</code>\n"
            f"Mode: Long + Short at S/R levels\n\n"
            f"Commands:\n"
            f"/status - Bot status & position\n"
            f"/levels - S/R levels (multi-TF)\n"
            f"/pnl - Position P&L\n"
            f"/risk - TP/SL & leverage info\n"
            f"/help - All commands"
        )

    def notify_levels(self, current_price: float, support: list, resistance: list):
        sup = "\n".join(f"  {s:.2f}" for s in support[:5]) or "  none"
        res = "\n".join(f"  {r:.2f}" for r in resistance[:5]) or "  none"
        self.send(
            f"<b>Levels Update</b>\n"
            f"Price: <code>{current_price:.2f}</code>\n\n"
            f"<b>Support (long zones):</b>\n<code>{sup}</code>\n\n"
            f"<b>Resistance (short zones):</b>\n<code>{res}</code>"
        )

    def notify_entry(self, side: str, level: float, level_type: str, price: float,
                     quantity: float, sl: float, tp: float, leverage: int):
        label = "LONG" if side == "long" else "SHORT"
        self.send(
            f"<b>{label} ENTRY at {level_type.upper()}</b>\n"
            f"Level: <code>{level:.2f}</code>\n"
            f"Entry: <code>{price:.2f}</code>\n"
            f"Size: <code>{quantity:.6f}</code>\n"
            f"SL: <code>{sl:.2f}</code> | TP: <code>{tp:.2f}</code>\n"
            f"Leverage: <code>{leverage}x</code>"
        )

    def notify_exit(self, reason: str, level: float, level_type: str, price: float,
                    entry: float = 0, leverage: int = 1, side: str = "long"):
        label = "STOP LOSS" if reason == "stop_loss" else "TAKE PROFIT"
        emoji = "red" if reason == "stop_loss" else "green"
        side_label = side.upper()
        msg = (
            f"<b>{label} HIT ({side_label})</b>\n"
            f"Level: <code>{level:.2f}</code> ({level_type})\n"
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

    def notify_limit_order(self, side: str, level: float, level_type: str,
                           quantity: float, sl: float, tp: float,
                           leverage: int, strength: float):
        label = "LIMIT BUY" if side == "long" else "LIMIT SELL"
        self.send(
            f"<b>{label} at {level_type.upper()}</b>\n"
            f"Level: <code>{level:.2f}</code>\n"
            f"Entry: <code>{level:.2f}</code>\n"
            f"Size: <code>{quantity:.6f}</code>\n"
            f"SL: <code>{sl:.2f}</code> | TP: <code>{tp:.2f}</code>\n"
            f"Leverage: <code>{leverage}x</code>\n"
            f"Confidence: <code>{strength:.1f}</code>"
        )

    def notify_order_cancelled(self, side: str, level: float, level_type: str):
        label = "BUY" if side == "long" else "SELL"
        self.send(
            f"<b>ORDER CANCELLED</b>\n"
            f"Limit {label} at {level_type} <code>{level:.2f}</code>"
        )

    def notify_buy(self, level: float, level_type: str, price: float, quantity: float, sl: float, tp: float):
        self.notify_entry("long", level, level_type, price, quantity, sl, tp, 1)

    def notify_error(self, error: str):
        self.send(f"<b>Error</b>\n<code>{error[:500]}</code>")

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            logger.warning(f"Unauthorized access attempt from user {update.effective_user.id}")
            return
        await update.message.reply_text(
            "<b>Scalp Trading Bot</b>\n\n"
            "<b>Monitor:</b>\n"
            "/status - Bot status & position\n"
            "/levels - Current support & resistance\n"
            "/pnl - Position P&L\n"
            "/config - Current settings\n\n"
            "<b>Risk management:</b>\n"
            "/risk - Show TP/SL & leverage info\n"
            "/settp 1.5 - Set target profit %\n"
            "/setsl 0.5 - Set max loss %\n"
            "/setlev 3 - Set leverage (1-5)\n\n"
            "<b>Learning:</b>\n"
            "/learn - What the bot has learned\n"
            "/journal - Recent trade history\n\n"
            "/help - This message",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await self._cmd_start(update, context)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        try:
            price = self.trader.exchange.get_ticker_price()
        except Exception:
            price = 0

        levels = len(self.trader.known_levels)
        if self.trader.config.paper_trade:
            mode = "PAPER"
        else:
            mode = "LIVE MAINNET" if self.trader.config.hl_mainnet else "LIVE TESTNET"

        pos = self.trader.position
        pending = self.trader.pending_order
        if pos:
            lev = self.trader.config.hl_leverage
            if pos.side == "long":
                price_pnl = (price - pos.entry_price) / pos.entry_price * 100
            else:
                price_pnl = (pos.entry_price - price) / pos.entry_price * 100
            margin_pnl = price_pnl * lev
            sign = "+" if margin_pnl >= 0 else ""

            pos_text = (
                f"\n\n<b>Position: {pos.side.upper()}</b>\n"
                f"Entry: <code>{pos.entry_price:.2f}</code> ({pos.kind})\n"
                f"Now: <code>{price:.2f}</code>\n"
                f"Margin PnL: <code>{sign}{margin_pnl:.2f}%</code>\n"
                f"SL: <code>{pos.stop_loss:.2f}</code> | TP: <code>{pos.take_profit:.2f}</code>"
            )
        elif pending:
            label = "LIMIT BUY" if pending.side == "long" else "LIMIT SELL"
            dist = abs(price - pending.price) / price * 100
            pos_text = (
                f"\n\n<b>Pending: {label}</b>\n"
                f"Level: <code>{pending.level_price:.2f}</code> ({pending.kind})\n"
                f"Price: <code>{pending.price:.2f}</code> ({dist:.2f}% away)\n"
                f"Size: <code>{pending.quantity:.6f}</code>\n"
                f"SL: <code>{pending.stop_loss:.2f}</code> | TP: <code>{pending.take_profit:.2f}</code>\n"
                f"Confidence: <code>{pending.effective_strength:.1f}</code>"
            )
        else:
            pos_text = "\n\nNo open position — scanning for entry..."

        await update.message.reply_text(
            f"<b>Bot Status [{mode}]</b>\n\n"
            f"Price: <code>{price:.2f}</code>\n"
            f"Levels tracked: <code>{levels}</code>\n"
            f"Pending orders: <code>{1 if pending else 0}</code>\n"
            f"Open positions: <code>{1 if pos else 0}</code>"
            f"{pos_text}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_levels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader or not self.trader.known_levels:
            await update.message.reply_text("No levels detected yet. Wait for the next refresh.")
            return

        try:
            price = self.trader.exchange.get_ticker_price()
        except Exception:
            price = 0

        sorted_levels = sorted(self.trader.known_levels.values(), key=lambda l: l.strength, reverse=True)
        support = [l for l in sorted_levels if l.kind == "support"]
        resistance = [l for l in sorted_levels if l.kind == "resistance"]

        lines = [f"<b>S/R Levels (Multi-TF)</b>\nPrice: <code>{price:.2f}</code>\n"]

        if resistance:
            lines.append("<b>Resistance (short zones):</b>")
            for l in resistance[:5]:
                dist = abs(l.price - price) / price * 100
                tfs = ",".join(l.timeframes) if l.timeframes else "-"
                lines.append(f"  <code>{l.price:.2f}</code> | str={l.strength} t={l.touches} [{tfs}] ({dist:.1f}%)")

        if support:
            lines.append("\n<b>Support (long zones):</b>")
            for l in support[:5]:
                dist = abs(l.price - price) / price * 100
                tfs = ",".join(l.timeframes) if l.timeframes else "-"
                lines.append(f"  <code>{l.price:.2f}</code> | str={l.strength} t={l.touches} [{tfs}] ({dist:.1f}%)")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader or not self.trader.position:
            await update.message.reply_text("No open position.")
            return

        try:
            price = self.trader.exchange.get_ticker_price()
        except Exception:
            await update.message.reply_text("Could not fetch current price.")
            return

        pos = self.trader.position
        lev = self.trader.config.hl_leverage

        if pos.side == "long":
            price_pnl = (price - pos.entry_price) / pos.entry_price * 100
            pnl_usd = pos.quantity * (price - pos.entry_price) * lev
        else:
            price_pnl = (pos.entry_price - price) / pos.entry_price * 100
            pnl_usd = pos.quantity * (pos.entry_price - price) * lev

        margin_pnl = price_pnl * lev
        sign = "+" if margin_pnl >= 0 else ""

        sl_moved = pos.stop_loss != pos.initial_sl
        sl_tag = " (trailed)" if sl_moved else ""
        tp_moved = pos.take_profit != pos.initial_tp
        tp_tag = " (extended)" if tp_moved else ""

        hold_h = ((__import__("time").time() - pos.filled_at) / 3600) if pos.filled_at else 0

        await update.message.reply_text(
            f"<b>{pos.side.upper()} Position ({lev}x)</b>\n\n"
            f"Entry: <code>{pos.entry_price:.2f}</code> ({pos.kind})\n"
            f"Now: <code>{price:.2f}</code>\n"
            f"Margin PnL: <code>{sign}{margin_pnl:.2f}%</code> ({sign}{pnl_usd:.2f})\n"
            f"SL: <code>{pos.stop_loss:.2f}</code>{sl_tag}\n"
            f"TP: <code>{pos.take_profit:.2f}</code>{tp_tag}\n"
            f"Hold time: <code>{hold_h:.1f}h</code>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return
        c = self.trader.config
        symbol = c.hl_symbol if c.exchange_backend == "hyperliquid" else c.symbol
        if c.paper_trade:
            mode = "PAPER"
        else:
            mode = "LIVE MAINNET" if c.hl_mainnet else "LIVE TESTNET"
        await update.message.reply_text(
            f"<b>Config</b>\n\n"
            f"Mode: <code>{mode}</code>\n"
            f"Exchange: <code>{c.exchange_backend}</code>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Timeframe: <code>{c.timeframe}</code>\n"
            f"Order size: <code>{c.order_size}</code>\n"
            f"Leverage: <code>{c.hl_leverage}x</code> (max {c.hl_max_leverage}x)\n"
            f"Target PnL: <code>{c.target_pnl_pct}%</code>/trade\n"
            f"Max Loss: <code>{c.max_loss_pct}%</code>/trade\n"
            f"Tick interval: <code>{c.check_interval}s</code>\n"
            f"Level refresh: <code>{c.level_refresh_seconds}s</code>\n"
            f"Order TTL: <code>{c.order_ttl_hours}h</code>\n"
            f"Cooldown: <code>{c.cooldown_seconds}s</code>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_learn(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        text = self.trader.learner.get_insights_text()
        await update.message.reply_text(
            f"<b>Strategy Learner</b>\n\n<pre>{text}</pre>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_journal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        trades = self.trader.journal.load_recent(10)
        if not trades:
            await update.message.reply_text("No trades recorded yet.")
            return

        lines = [f"<b>Last {len(trades)} Trades</b>\n"]
        for i, t in enumerate(reversed(trades), 1):
            sign = "+" if t.margin_pnl_pct >= 0 else ""
            icon = "W" if t.margin_pnl_pct > 0 else "L"
            tfs = ",".join(t.timeframes) if t.timeframes else "-"
            lines.append(
                f"{i}. [{icon}] {t.side.upper()} {t.kind} @ <code>{t.entry_price:.2f}</code>\n"
                f"   Exit: <code>{t.exit_price:.2f}</code> ({t.exit_reason})\n"
                f"   Margin: <code>{sign}{t.margin_pnl_pct:.2f}%</code> (${sign}{t.pnl_usd:.2f})\n"
                f"   Str: {t.level_strength} | TF: [{tfs}] | {t.hold_duration_h:.1f}h"
            )

        stats = self.trader.journal.stats()
        sign = "+" if stats["total_pnl_usd"] >= 0 else ""
        lines.append(
            f"\n<b>Overall: {stats['win_rate']:.0f}% win rate | "
            f"${sign}{stats['total_pnl_usd']:.2f}</b>"
        )
        if stats.get("long_total", 0) > 0 or stats.get("short_total", 0) > 0:
            lines.append(
                f"Long: {stats.get('long_win_rate', 0):.0f}% ({stats.get('long_total', 0)}) | "
                f"Short: {stats.get('short_win_rate', 0):.0f}% ({stats.get('short_total', 0)})"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        c = self.trader.config
        lev = c.hl_leverage

        try:
            price = self.trader.exchange.get_ticker_price()
        except Exception:
            price = 0

        lines = [
            f"<b>Risk Settings</b>\n",
            f"Leverage: <code>{lev}x</code> (max {c.hl_max_leverage}x)",
            f"Target PnL: <code>{c.target_pnl_pct}%</code> per trade (on margin)",
            f"Max Loss: <code>{c.max_loss_pct}%</code> per trade (on margin)",
        ]

        if price > 0:
            tpsl_long = compute_tp_sl(price, lev, c.target_pnl_pct, c.max_loss_pct, side="long")
            tpsl_short = compute_tp_sl(price, lev, c.target_pnl_pct, c.max_loss_pct, side="short")
            lines.append(f"\n<b>Long example @ <code>{price:.2f}</code>:</b>")
            lines.append(f"  TP: <code>{tpsl_long['tp_price']:.2f}</code> | SL: <code>{tpsl_long['sl_price']:.2f}</code>")
            lines.append(f"  Liq: <code>~{tpsl_long['liq_price']:.2f}</code>")
            lines.append(f"\n<b>Short example @ <code>{price:.2f}</code>:</b>")
            lines.append(f"  TP: <code>{tpsl_short['tp_price']:.2f}</code> | SL: <code>{tpsl_short['sl_price']:.2f}</code>")
            lines.append(f"  Liq: <code>~{tpsl_short['liq_price']:.2f}</code>")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_settp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /settp 1.5\nSets target profit % on margin per trade.")
            return

        try:
            val = float(context.args[0])
            if val <= 0 or val > 50:
                await update.message.reply_text("Target must be between 0.1 and 50.")
                return
            self.trader.config.target_pnl_pct = val
            lev = self.trader.config.hl_leverage
            price_move = val / lev
            await update.message.reply_text(
                f"Target PnL set to <code>{val}%</code> on margin.\n"
                f"At {lev}x leverage, need <code>{price_move:.3f}%</code> price move.",
                parse_mode=ParseMode.HTML,
            )
        except ValueError:
            await update.message.reply_text("Invalid number. Usage: /settp 1.5")

    async def _cmd_setsl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /setsl 1.0\nSets max loss % on margin per trade.")
            return

        try:
            val = float(context.args[0])
            if val <= 0 or val > 50:
                await update.message.reply_text("Max loss must be between 0.1 and 50.")
                return
            lev = self.trader.config.hl_leverage
            max_safe = (1 / lev) * 50
            price_move = val / lev
            if price_move > max_safe:
                await update.message.reply_text(
                    f"Too risky! At {lev}x, {val}% margin loss = {price_move:.2f}% price drop.\n"
                    f"Max safe: <code>{max_safe * lev:.1f}%</code> margin loss.",
                    parse_mode=ParseMode.HTML,
                )
                return
            self.trader.config.max_loss_pct = val
            await update.message.reply_text(
                f"Max loss set to <code>{val}%</code> on margin.\n"
                f"At {lev}x leverage, SL at <code>{price_move:.3f}%</code> price drop.",
                parse_mode=ParseMode.HTML,
            )
        except ValueError:
            await update.message.reply_text("Invalid number. Usage: /setsl 1.0")

    async def _cmd_setlev(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /setlev 3\nSets leverage (1-5).")
            return

        try:
            val = int(context.args[0])
            max_lev = self.trader.config.hl_max_leverage
            if val < 1 or val > max_lev:
                await update.message.reply_text(f"Leverage must be between 1 and {max_lev}.")
                return

            self.trader.config.hl_leverage = val

            if not self.trader.config.paper_trade and hasattr(self.trader.exchange, '_set_leverage'):
                try:
                    self.trader.exchange.config.hl_leverage = val
                    self.trader.exchange._set_leverage()
                except Exception as e:
                    logger.error(f"Failed to update exchange leverage: {e}")

            tp_move = self.trader.config.target_pnl_pct / val
            sl_move = self.trader.config.max_loss_pct / val
            liq_dist = (1 / val) * 100

            await update.message.reply_text(
                f"Leverage set to <code>{val}x</code>\n\n"
                f"TP price move: <code>{tp_move:.3f}%</code>\n"
                f"SL price move: <code>{sl_move:.3f}%</code>\n"
                f"Liquidation at: <code>~{liq_dist:.1f}%</code> drop",
                parse_mode=ParseMode.HTML,
            )
        except ValueError:
            await update.message.reply_text("Invalid number. Usage: /setlev 3")

    def start_command_listener(self):
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
        app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        app.add_handler(CommandHandler("config", self._cmd_config))
        app.add_handler(CommandHandler("risk", self._cmd_risk))
        app.add_handler(CommandHandler("settp", self._cmd_settp))
        app.add_handler(CommandHandler("setsl", self._cmd_setsl))
        app.add_handler(CommandHandler("setlev", self._cmd_setlev))
        app.add_handler(CommandHandler("learn", self._cmd_learn))
        app.add_handler(CommandHandler("journal", self._cmd_journal))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        stop_event = asyncio.Event()
        await stop_event.wait()


TelegramNotifier = TelegramBot
