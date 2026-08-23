import logging
import asyncio
import html
import threading
import time
from collections import deque
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

from bot.levels import compute_tp_sl

logger = logging.getLogger(__name__)

# Keywords that make an INFO message worth forwarding to Telegram
_KEY_PATTERNS = (
    "FILLED", "LIMIT ", "DECISION", "SKIP", "CANCEL", "STOP LOSS",
    "TAKE PROFIT", "Startup", "Synced", "cleanup", "position",
    "Tick:", "ERROR", "WARNING", "Level",
)


class TelegramLogHandler(logging.Handler):
    """Logging handler that forwards important messages to Telegram."""

    def __init__(self, bot_send_fn, level=logging.INFO):
        super().__init__(level)
        self._send = bot_send_fn
        self._buffer: list[str] = []
        self._last_flush = 0.0
        self._flush_interval = 5.0
        self._verbose = False

    @property
    def verbose(self):
        return self._verbose

    @verbose.setter
    def verbose(self, val: bool):
        self._verbose = val

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            if record.levelno >= logging.WARNING:
                self._buffer.append(msg)
                self._try_flush()
                return
            if self._verbose or any(p in msg for p in _KEY_PATTERNS):
                self._buffer.append(msg)
                self._try_flush()
        except Exception:
            pass

    def _try_flush(self):
        now = time.time()
        if not self._buffer:
            return
        if now - self._last_flush < self._flush_interval and len(self._buffer) < 10:
            return
        text = "\n".join(self._buffer[-20:])
        self._buffer.clear()
        self._last_flush = now
        try:
            safe = html.escape(text[:3500])
            self._send(f"<pre>{safe}</pre>")
        except Exception:
            pass


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
        self._log_handler: TelegramLogHandler | None = None

    def set_trader(self, trader):
        self.trader = trader

    def get_log_handler(self) -> TelegramLogHandler:
        if not self._log_handler:
            self._log_handler = TelegramLogHandler(self.send)
            fmt = logging.Formatter("%(asctime)s [%(levelname).1s] %(name)s: %(message)s", datefmt="%H:%M:%S")
            self._log_handler.setFormatter(fmt)
        return self._log_handler

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
            f"/scan - Scan all markets (multi-symbol)\n"
            f"/help - All commands"
        )

    def notify_levels(self, current_price: float, support: list, resistance: list):
        def _fmt(levels):
            lines = []
            for lv in levels[:5]:
                if isinstance(lv, dict):
                    tag = f" [Fib {lv['fib']}]" if lv.get("fib") else ""
                    lines.append(f"  {lv['price']:.2f}{tag}")
                else:
                    lines.append(f"  {lv:.2f}")
            return "\n".join(lines) or "  none"

        self.send(
            f"<b>Levels Update</b>\n"
            f"Price: <code>{current_price:.2f}</code>\n\n"
            f"<b>Support (long zones):</b>\n<code>{_fmt(support)}</code>\n\n"
            f"<b>Resistance (short zones):</b>\n<code>{_fmt(resistance)}</code>"
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

    def notify_decision_report(self, report: dict):
        r = report
        trend = r.get("trend", {})
        breakout = r.get("breakout", {})
        level = r.get("level", {})
        decision = r.get("decision", "SKIP")
        reason = r.get("reason", "")

        trend_dir = trend.get("direction", "?").upper()
        trend_conf = trend.get("confidence", 0)
        tf_lines = []
        for tf, detail in trend.get("tf_details", {}).items():
            if isinstance(detail, dict) and "error" not in detail:
                tf_lines.append(
                    f"  {tf}: EMA {detail.get('ema_cross', '?')} | "
                    f"Price {'>' if detail.get('price_vs_ema21') == 'above' else '<'} EMA21 | "
                    f"Score: {detail.get('score', '?')}"
                )

        bo_conf = breakout.get("confidence", 0)
        vol_ok = "Y" if breakout.get("volume_confirmed") else "N"
        mom_ok = "Y" if breakout.get("momentum_confirmed") else "N"
        trend_ok = "Y" if breakout.get("trend_aligned") else "N"
        retest_ok = "Y" if breakout.get("retest_seen") else "N"
        bo_verdict = "BREAKOUT" if breakout.get("is_breakout") else ("FAKEOUT" if breakout.get("is_fakeout") else "UNCLEAR")

        fib_tag = f" (Fib {level.get('fib_ratio')})" if level.get("fib_ratio") else ""

        symbol = r.get("symbol", "?")
        msg = (
            f"<b>DECISION REPORT — {symbol}</b>\n"
            f"{'=' * 28}\n\n"
            f"<b>1. TRIGGER</b>\n"
            f"Level: <code>{level.get('price', '?')}</code> ({level.get('kind', '?')}{fib_tag})\n"
            f"Strength: <code>{level.get('strength', '?')}</code> | "
            f"Eff: <code>{level.get('effective_strength', '?')}</code>\n"
            f"TFs: <code>{level.get('timeframes', '?')}</code>\n"
            f"Distance: <code>{level.get('distance_pct', '?')}%</code>\n\n"
            f"<b>2. TREND BIAS (1M/5M/15M)</b>\n"
            f"Direction: <code>{trend_dir}</code> ({trend_conf:.0f}%)\n"
        )
        if tf_lines:
            msg += "<code>" + "\n".join(tf_lines) + "</code>\n"
        msg += (
            f"\n<b>3. BREAKOUT CHECK</b>\n"
            f"Verdict: <code>{bo_verdict}</code> ({bo_conf:.0f}%)\n"
            f"  Volume:   <code>[{vol_ok}]</code>\n"
            f"  Momentum: <code>[{mom_ok}]</code>\n"
            f"  Trend:    <code>[{trend_ok}]</code>\n"
            f"  Retest:   <code>[{retest_ok}]</code>\n"
        )
        if breakout.get("details"):
            msg += f"  <code>{breakout['details']}</code>\n"
        lev = r.get("leverage")
        lev_line = f"\nLeverage: <code>{lev}x</code> (dynamic)" if lev else ""
        msg += (
            f"\n<b>4. DECISION: {decision}</b>{lev_line}\n"
            f"{reason}"
        )

        self.send(msg)

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
            "<b>Control:</b>\n"
            "/pause - Stop placing new orders\n"
            "/resume - Resume trading\n"
            "/stop - Stop bot (force to ignore open positions)\n"
            "/closeall - Close all positions & orders\n\n"
            "<b>Monitor:</b>\n"
            "/status - Bot status & all positions\n"
            "/pnl - All positions P&L\n"
            "/levels - S/R levels per symbol\n"
            "/scan - Scan all markets\n\n"
            "<b>Config (live):</b>\n"
            "/set - Show all editable settings\n"
            "/set size 1000 - Order size\n"
            "/set tp 2.5 - Target PnL %\n"
            "/set sl 1.5 - Max loss %\n"
            "/set levmin 3 - Min leverage\n"
            "/set levmax 10 - Max leverage\n"
            "/set maxpos 5 - Max positions\n"
            "/config - Full config view\n"
            "/risk - TP/SL & leverage details\n\n"
            "<b>Logs:</b>\n"
            "/logs on - Stream all logs\n"
            "/logs off - Key events only\n\n"
            "<b>History:</b>\n"
            "/journal - Recent trades\n"
            "/learn - Strategy insights\n\n"
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

        if self.trader.config.paper_trade:
            mode = "PAPER"
        else:
            mode = "LIVE MAINNET" if self.trader.config.hl_mainnet else "LIVE TESTNET"

        total_levels = sum(len(v) for v in self.trader.known_levels.values())
        positions = self.trader.positions
        pending_orders = self.trader.pending_orders
        lev_min = self.trader.config.hl_leverage_min
        lev_max = self.trader.config.hl_leverage_max
        lev = self.trader.config.hl_leverage

        symbols_line = ", ".join(self.trader._get_symbols())
        max_pos = self.trader.max_positions

        pos_text = ""
        if positions:
            pos_text += "\n"
            for sym, pos in positions.items():
                self.trader.exchange.switch_symbol(sym)
                try:
                    sym_price = self.trader.exchange.get_ticker_price()
                except Exception:
                    sym_price = 0
                pos_lev = pos.leverage
                if pos.side == "long":
                    price_pnl = (sym_price - pos.entry_price) / pos.entry_price * 100
                else:
                    price_pnl = (pos.entry_price - sym_price) / pos.entry_price * 100
                margin_pnl = price_pnl * pos_lev
                sign = "+" if margin_pnl >= 0 else ""
                pos_text += (
                    f"\n<b>{sym} {pos.side.upper()}</b>\n"
                    f"  Entry: <code>{pos.entry_price:.2f}</code> ({pos.kind})\n"
                    f"  Now: <code>{sym_price:.2f}</code> | PnL: <code>{sign}{margin_pnl:.2f}%</code>\n"
                    f"  SL: <code>{pos.stop_loss:.2f}</code> | TP: <code>{pos.take_profit:.2f}</code>"
                )

        if pending_orders:
            pos_text += "\n"
            for sym, pend in pending_orders.items():
                self.trader.exchange.switch_symbol(sym)
                try:
                    sym_price = self.trader.exchange.get_ticker_price()
                except Exception:
                    sym_price = 0
                label = "BUY" if pend.side == "long" else "SELL"
                dist = abs(sym_price - pend.price) / sym_price * 100 if sym_price else 0
                pos_text += (
                    f"\n<b>{sym} LIMIT {label}</b>\n"
                    f"  Level: <code>{pend.level_price:.2f}</code> ({pend.kind})\n"
                    f"  Price: <code>{pend.price:.2f}</code> ({dist:.2f}% away)\n"
                    f"  SL: <code>{pend.stop_loss:.2f}</code> | TP: <code>{pend.take_profit:.2f}</code>\n"
                    f"  Str: <code>{pend.effective_strength:.1f}</code>"
                )

        if not positions and not pending_orders:
            pos_text = "\n\nNo open positions — scanning for entries..."

        self.trader.exchange.switch_symbol(self.trader.config.hl_symbol)

        await update.message.reply_text(
            f"<b>Bot Status [{mode}]</b>\n\n"
            f"Symbols: <code>{symbols_line}</code>\n"
            f"Leverage: <code>dynamic {lev_min}x-{lev_max}x</code> (last: {lev}x)\n"
            f"Levels tracked: <code>{total_levels}</code>\n"
            f"Positions: <code>{len(positions)}/{max_pos}</code> | "
            f"Pending: <code>{len(pending_orders)}</code>"
            f"{pos_text}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        positions = self.trader.positions
        pending = self.trader.pending_orders
        if not positions and not pending:
            await update.message.reply_text("No open orders or positions.")
            return

        lines = ["<b>Orders & Positions</b>\n"]

        for sym in sorted(set(list(positions.keys()) + list(pending.keys()))):
            self.trader.exchange.switch_symbol(sym)
            try:
                price = self.trader.exchange.get_ticker_price()
            except Exception:
                price = 0

            sym_levels = self.trader.known_levels.get(sym, {})
            resistances = sorted(
                [l for l in sym_levels.values() if l.kind == "resistance"],
                key=lambda l: l.price,
            )
            supports = sorted(
                [l for l in sym_levels.values() if l.kind == "support"],
                key=lambda l: l.price, reverse=True,
            )

            lines.append(f"\n<b>--- {sym} ---</b>")

            chart = []
            pos = positions.get(sym)
            pend = pending.get(sym)

            if pos:
                pos_lev = pos.leverage
                if pos.side == "long":
                    pnl_pct = (price - pos.entry_price) / pos.entry_price * 100 * pos_lev
                else:
                    pnl_pct = (pos.entry_price - price) / pos.entry_price * 100 * pos_lev
                sign = "+" if pnl_pct >= 0 else ""
                chart.append((pos.take_profit, f"  TP   {pos.take_profit:.2f}  ............"))
                chart.append((pos.entry_price, f"  {pos.side[0].upper()}    {pos.entry_price:.2f}  [{sign}{pnl_pct:.1f}%]"))
                chart.append((pos.stop_loss, f"  SL   {pos.stop_loss:.2f}  ............"))

            if pend:
                label = "BUY" if pend.side == "long" else "SEL"
                chart.append((pend.take_profit, f"  TP   {pend.take_profit:.2f}  ............"))
                chart.append((pend.price, f"  {label}  {pend.price:.2f}  [pending]"))
                chart.append((pend.stop_loss, f"  SL   {pend.stop_loss:.2f}  ............"))

            for r in resistances[:2]:
                chart.append((r.price, f"  R    {r.price:.2f}  str={r.strength}"))
            for s in supports[:2]:
                chart.append((s.price, f"  S    {s.price:.2f}  str={s.strength}"))

            chart.append((price, f"  --&gt; {price:.2f} &lt;--  NOW"))

            chart.sort(key=lambda x: x[0], reverse=True)

            lines.append("<code>")
            for _, line in chart:
                lines.append(line)
            lines.append("</code>")

            if pend:
                lines.append(f"  /cancel {sym} — cancel pending order")

        self.trader.exchange.switch_symbol(self.trader.config.hl_symbol)
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        if not context.args:
            symbols = list(self.trader.pending_orders.keys())
            if not symbols:
                await update.message.reply_text("No pending orders to cancel.")
                return
            await update.message.reply_text(
                f"Usage: /cancel SYMBOL\n\nPending: {', '.join(symbols)}"
            )
            return

        sym = context.args[0].upper()
        if sym not in self.trader.pending_orders:
            await update.message.reply_text(f"No pending order on {sym}.")
            return

        pend = self.trader.pending_orders[sym]
        self.trader.exchange.switch_symbol(sym)
        self.trader._cancel_pending_order(sym)
        self.trader.exchange.switch_symbol(self.trader.config.hl_symbol)

        await update.message.reply_text(
            f"<b>Cancelled {sym}</b>\n"
            f"Was: {pend.side.upper()} @ {pend.price:.2f}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_levels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader or not self.trader.known_levels:
            await update.message.reply_text("No levels detected yet. Wait for the next refresh.")
            return

        lines = [f"<b>S/R Levels (Multi-TF)</b>\n"]

        for sym, sym_levels in self.trader.known_levels.items():
            if not sym_levels:
                continue
            self.trader.exchange.switch_symbol(sym)
            try:
                price = self.trader.exchange.get_ticker_price()
            except Exception:
                price = 0

            sorted_levels = sorted(sym_levels.values(), key=lambda l: l.strength, reverse=True)
            support = [l for l in sorted_levels if l.kind == "support"]
            resistance = [l for l in sorted_levels if l.kind == "resistance"]

            lines.append(f"<b>{sym}</b> — <code>{price:.2f}</code>")
            if resistance:
                for l in resistance[:3]:
                    dist = abs(l.price - price) / price * 100 if price else 0
                    tfs = ",".join(l.timeframes) if l.timeframes else "-"
                    lines.append(f"  R <code>{l.price:.2f}</code> str={l.strength} [{tfs}] ({dist:.1f}%)")
            if support:
                for l in support[:3]:
                    dist = abs(l.price - price) / price * 100 if price else 0
                    tfs = ",".join(l.timeframes) if l.timeframes else "-"
                    lines.append(f"  S <code>{l.price:.2f}</code> str={l.strength} [{tfs}] ({dist:.1f}%)")
            lines.append("")

        self.trader.exchange.switch_symbol(self.trader.config.hl_symbol)
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader or not self.trader.positions:
            await update.message.reply_text("No open positions.")
            return

        import time as _time
        lines = [f"<b>Open Positions PnL</b>\n"]
        total_pnl_usd = 0.0

        for sym, pos in self.trader.positions.items():
            self.trader.exchange.switch_symbol(sym)
            try:
                price = self.trader.exchange.get_ticker_price()
            except Exception:
                lines.append(f"\n<b>{sym}</b> — price unavailable")
                continue

            pos_lev = pos.leverage
            if pos.side == "long":
                price_pnl = (price - pos.entry_price) / pos.entry_price * 100
                pnl_usd = pos.quantity * (price - pos.entry_price) * pos_lev
            else:
                price_pnl = (pos.entry_price - price) / pos.entry_price * 100
                pnl_usd = pos.quantity * (pos.entry_price - price) * pos_lev

            margin_pnl = price_pnl * pos_lev
            total_pnl_usd += pnl_usd
            sign = "+" if margin_pnl >= 0 else ""

            sl_moved = pos.stop_loss != pos.initial_sl
            sl_tag = " (trailed)" if sl_moved else ""
            tp_moved = pos.take_profit != pos.initial_tp
            tp_tag = " (extended)" if tp_moved else ""

            hold_h = (_time.time() - pos.filled_at) / 3600 if pos.filled_at else 0

            lines.append(
                f"\n<b>{sym} {pos.side.upper()}</b>\n"
                f"  Entry: <code>{pos.entry_price:.2f}</code> ({pos.kind})\n"
                f"  Now: <code>{price:.2f}</code>\n"
                f"  PnL: <code>{sign}{margin_pnl:.2f}%</code> (${sign}{pnl_usd:.2f})\n"
                f"  SL: <code>{pos.stop_loss:.2f}</code>{sl_tag} | "
                f"TP: <code>{pos.take_profit:.2f}</code>{tp_tag}\n"
                f"  Hold: <code>{hold_h:.1f}h</code>"
            )

        total_sign = "+" if total_pnl_usd >= 0 else ""
        lines.append(f"\n<b>Total: ${total_sign}{total_pnl_usd:.2f}</b>")

        self.trader.exchange.switch_symbol(self.trader.config.hl_symbol)

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

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
        multi_info = ""
        if c.hl_multi_symbol:
            symbols = c.hl_symbols if c.hl_symbols else f"top {c.hl_scan_top_n} by volume"
            multi_info = (
                f"Multi-symbol: <code>ON</code>\n"
                f"Symbols: <code>{symbols}</code>\n"
                f"Max positions: <code>{c.hl_max_positions}</code>\n"
            )

        await update.message.reply_text(
            f"<b>Config</b>\n\n"
            f"Mode: <code>{mode}</code>\n"
            f"Exchange: <code>{c.exchange_backend}</code>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"{multi_info}"
            f"Timeframe: <code>{c.timeframe}</code>\n"
            f"Order size: <code>{c.order_size}</code>\n"
            f"Leverage: <code>dynamic {c.hl_leverage_min}x-{c.hl_leverage_max}x</code>\n"
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

    async def _cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return
        if not self.trader.scanner:
            await update.message.reply_text("Multi-symbol mode is not enabled. Set HL_MULTI_SYMBOL=true.")
            return

        await update.message.reply_text("Scanning markets...")

        try:
            symbols = None
            if self.trader.config.hl_symbols:
                symbols = [s.strip() for s in self.trader.config.hl_symbols.split(",") if s.strip()]
            opportunities = self.trader.scanner.scan_all(
                symbols=symbols,
                top_n=self.trader.config.hl_scan_top_n,
            )

            if not opportunities:
                await update.message.reply_text("No opportunities found.")
                return

            lines = [f"<b>Market Scan Results ({len(opportunities)} found)</b>\n"]
            for i, o in enumerate(opportunities[:10]):
                active = " [ACTIVE]" if o.symbol == self.trader.config.hl_symbol else ""
                lines.append(
                    f"{i+1}. <b>{o.symbol}</b> — score: <code>{o.score:.1f}</code>{active}\n"
                    f"   {o.level.kind} @ <code>{o.level.price:.2f}</code> (str={o.level.strength})\n"
                    f"   Trend: {o.trend.direction} ({o.trend.confidence:.0f}%) | "
                    f"Dist: {o.distance_pct:.2f}%"
                )

            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"Scan failed: {e}")

    async def _cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        c = self.trader.config
        lev = c.hl_leverage
        lev_min = c.hl_leverage_min
        lev_max = c.hl_leverage_max

        try:
            price = self.trader.exchange.get_ticker_price()
        except Exception:
            price = 0

        lines = [
            f"<b>Risk Settings</b>\n",
            f"Leverage: <code>dynamic {lev_min}x-{lev_max}x</code> (last: {lev}x)",
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
            lev_min = self.trader.config.hl_leverage_min
            lev_max = self.trader.config.hl_leverage_max
            await update.message.reply_text(
                f"Target PnL set to <code>{val}%</code> on margin.\n"
                f"Price move needed: <code>{val / lev_max:.3f}%</code> ({lev_max}x) to <code>{val / lev_min:.3f}%</code> ({lev_min}x)",
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
            lev_max = self.trader.config.hl_leverage_max
            max_safe = (1 / lev_max) * 50
            price_move_max = val / lev_max
            if price_move_max > max_safe:
                await update.message.reply_text(
                    f"Too risky at {lev_max}x! {val}% margin loss = {price_move_max:.2f}% price drop.\n"
                    f"Max safe: <code>{max_safe * lev_max:.1f}%</code> margin loss.",
                    parse_mode=ParseMode.HTML,
                )
                return
            lev_min = self.trader.config.hl_leverage_min
            self.trader.config.max_loss_pct = val
            await update.message.reply_text(
                f"Max loss set to <code>{val}%</code> on margin.\n"
                f"SL price drop: <code>{val / lev_max:.3f}%</code> ({lev_max}x) to <code>{val / lev_min:.3f}%</code> ({lev_min}x)",
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

        if not context.args or len(context.args) < 2:
            c = self.trader.config
            await update.message.reply_text(
                f"<b>Leverage Range</b>\n\n"
                f"Current: <code>{c.hl_leverage_min}x - {c.hl_leverage_max}x</code>\n"
                f"Last used: <code>{c.hl_leverage}x</code>\n\n"
                f"Usage: /setlev min max\n"
                f"Example: /setlev 3 10",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            new_min = int(context.args[0])
            new_max = int(context.args[1])
            if new_min < 1 or new_max > 50 or new_min > new_max:
                await update.message.reply_text("Min must be 1+, max 50-, and min <= max.")
                return

            self.trader.config.hl_leverage_min = new_min
            self.trader.config.hl_leverage_max = new_max

            await update.message.reply_text(
                f"Leverage range set to <code>{new_min}x - {new_max}x</code>\n"
                f"Actual leverage per trade is computed from signal quality.",
                parse_mode=ParseMode.HTML,
            )
        except (ValueError, IndexError):
            await update.message.reply_text("Usage: /setlev 3 10")

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return
        self.trader.paused = True
        n_pos = len(self.trader.positions)
        n_pend = len(self.trader.pending_orders)
        await update.message.reply_text(
            f"<b>Bot PAUSED</b>\n\n"
            f"No new orders will be placed.\n"
            f"Existing positions ({n_pos}) and pending orders ({n_pend}) "
            f"are still managed (TP/SL active).\n\n"
            f"/resume to continue trading.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return
        self.trader.paused = False
        await update.message.reply_text(
            "<b>Bot RESUMED</b>\nScanning for new opportunities.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return
        n_pos = len(self.trader.positions)
        n_pend = len(self.trader.pending_orders)
        if n_pos > 0 or n_pend > 0:
            await update.message.reply_text(
                f"<b>WARNING:</b> {n_pos} positions and {n_pend} pending orders still open.\n"
                f"Use /closeall first, or /stop force to stop anyway.",
                parse_mode=ParseMode.HTML,
            )
            if not (context.args and context.args[0].lower() == "force"):
                return
        self.trader.running = False
        await update.message.reply_text(
            "<b>Bot STOPPING...</b>\nRestart: <code>sudo systemctl restart auto-trading-bot</code>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_closeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        self.trader.paused = True
        closed = 0
        cancelled = 0

        for sym in list(self.trader.pending_orders.keys()):
            self.trader._cancel_pending_order(sym)
            cancelled += 1

        for sym in list(self.trader.positions.keys()):
            self.trader.exchange.switch_symbol(sym)
            try:
                price = self.trader.exchange.get_ticker_price()
                self.trader._close_position(sym, price, "manual_close")
                closed += 1
            except Exception as e:
                await update.message.reply_text(f"Failed to close {sym}: {e}")

        self.trader.exchange.switch_symbol(self.trader.config.hl_symbol)
        await update.message.reply_text(
            f"<b>All positions closed</b>\n"
            f"Closed: {closed} | Cancelled: {cancelled}\n"
            f"Bot is PAUSED. /resume to continue.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if not self.trader:
            await update.message.reply_text("Bot not initialized yet.")
            return

        if not context.args or len(context.args) < 2:
            c = self.trader.config
            await update.message.reply_text(
                f"<b>Live Config (editable)</b>\n\n"
                f"<code>size    </code> {c.order_size}\n"
                f"<code>tp      </code> {c.target_pnl_pct}%\n"
                f"<code>sl      </code> {c.max_loss_pct}%\n"
                f"<code>levmin  </code> {c.hl_leverage_min}x\n"
                f"<code>levmax  </code> {c.hl_leverage_max}x\n"
                f"<code>maxpos  </code> {c.hl_max_positions}\n"
                f"<code>interval</code> {c.check_interval}s\n"
                f"<code>ttl     </code> {c.order_ttl_hours}h\n"
                f"<code>cooldown</code> {c.cooldown_seconds}s\n\n"
                f"Leverage is dynamic ({c.hl_leverage_min}x-{c.hl_leverage_max}x) per signal quality.\n\n"
                f"Usage: /set size 1000\n"
                f"       /set tp 2.5\n"
                f"       /set sl 1.5",
                parse_mode=ParseMode.HTML,
            )
            return

        key = context.args[0].lower()
        try:
            val = float(context.args[1])
        except ValueError:
            await update.message.reply_text("Invalid number.")
            return

        c = self.trader.config
        settings = {
            "size": ("order_size", 10, 50000, "Order size"),
            "tp": ("target_pnl_pct", 0.1, 50, "Target PnL %"),
            "sl": ("max_loss_pct", 0.1, 50, "Max loss %"),
            "levmin": ("hl_leverage_min", 1, 50, "Leverage min"),
            "levmax": ("hl_leverage_max", 1, 50, "Leverage max"),
            "maxpos": ("hl_max_positions", 1, 20, "Max positions"),
            "interval": ("check_interval", 1, 300, "Check interval (s)"),
            "ttl": ("order_ttl_hours", 0.1, 72, "Order TTL (h)"),
            "cooldown": ("cooldown_seconds", 0, 600, "Cooldown (s)"),
        }

        if key not in settings:
            await update.message.reply_text(f"Unknown setting: {key}\nAvailable: {', '.join(settings.keys())}")
            return

        attr, min_v, max_v, label = settings[key]
        if val < min_v or val > max_v:
            await update.message.reply_text(f"{label} must be between {min_v} and {max_v}.")
            return

        if key in ("levmin", "levmax", "maxpos", "interval", "cooldown"):
            val = int(val)

        setattr(c, attr, val)

        if key == "maxpos":
            self.trader.max_positions = int(val)

        await update.message.reply_text(
            f"<b>{label}</b> set to <code>{val}</code>",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        handler = self._log_handler
        if not handler:
            await update.message.reply_text("Log handler not active.")
            return

        if context.args and context.args[0].lower() in ("on", "all", "verbose"):
            handler.verbose = True
            await update.message.reply_text("Verbose logs: ON\nAll log messages will be forwarded.")
        elif context.args and context.args[0].lower() in ("off", "quiet", "key"):
            handler.verbose = False
            await update.message.reply_text("Verbose logs: OFF\nOnly key events and warnings forwarded.")
        else:
            status = "ON (all)" if handler.verbose else "OFF (key events only)"
            await update.message.reply_text(
                f"<b>Log Streaming</b>\n\n"
                f"Verbose: <code>{status}</code>\n\n"
                f"Usage:\n"
                f"/logs on — stream all logs\n"
                f"/logs off — key events only",
                parse_mode=ParseMode.HTML,
            )

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
        app.add_handler(CommandHandler("scan", self._cmd_scan))
        app.add_handler(CommandHandler("orders", self._cmd_orders))
        app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        app.add_handler(CommandHandler("pause", self._cmd_pause))
        app.add_handler(CommandHandler("resume", self._cmd_resume))
        app.add_handler(CommandHandler("stop", self._cmd_stop))
        app.add_handler(CommandHandler("closeall", self._cmd_closeall))
        app.add_handler(CommandHandler("set", self._cmd_set))
        app.add_handler(CommandHandler("logs", self._cmd_logs))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        stop_event = asyncio.Event()
        await stop_event.wait()


TelegramNotifier = TelegramBot
