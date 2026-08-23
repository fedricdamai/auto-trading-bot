import logging
import asyncio
from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends trade alerts and status updates to a Telegram chat."""

    def __init__(self, token: str, chat_id: str):
        self.bot = Bot(token=token)
        self.chat_id = chat_id

    def send(self, message: str):
        """Send a message synchronously (blocking)."""
        try:
            asyncio.get_event_loop().run_until_complete(
                self.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                )
            )
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                self.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                )
            )

    def notify_startup(self, symbol: str, timeframe: str, paper: bool):
        mode = "PAPER" if paper else "LIVE"
        self.send(
            f"<b>Bot Started [{mode}]</b>\n"
            f"Symbol: <code>{symbol}</code>\n"
            f"Timeframe: <code>{timeframe}</code>"
        )

    def notify_levels(self, current_price: float, support: list, resistance: list):
        sup = ", ".join(f"{s:.2f}" for s in support[:5]) or "none"
        res = ", ".join(f"{r:.2f}" for r in resistance[:5]) or "none"
        self.send(
            f"<b>Levels Detected</b>\n"
            f"Price: <code>{current_price:.2f}</code>\n"
            f"Support: <code>{sup}</code>\n"
            f"Resistance: <code>{res}</code>"
        )

    def notify_buy(self, level: float, level_type: str, price: float, quantity: float, sl: float, tp: float):
        self.send(
            f"<b>BUY at {level_type.upper()}</b>\n"
            f"Level: <code>{level:.2f}</code>\n"
            f"Entry: <code>{price:.2f}</code>\n"
            f"Size: <code>{quantity:.6f}</code>\n"
            f"SL: <code>{sl:.2f}</code> | TP: <code>{tp:.2f}</code>"
        )

    def notify_exit(self, reason: str, level: float, level_type: str, price: float):
        emoji = "SL" if reason == "stop_loss" else "TP"
        self.send(
            f"<b>{emoji} HIT</b>\n"
            f"Level: <code>{level:.2f}</code> ({level_type})\n"
            f"Exit price: <code>{price:.2f}</code>"
        )

    def notify_error(self, error: str):
        self.send(f"<b>Error</b>\n<code>{error[:500]}</code>")
