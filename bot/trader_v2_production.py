"""Final production guard above the tracked V2 trader.

An already-active symbol owns its trade ID and thesis until that lifecycle ends.
Repeated scans must never replace that identity.
"""

from __future__ import annotations

import logging

from bot.trader_v2_tracked import Trader as TrackedTrader

logger = logging.getLogger(__name__)


class Trader(TrackedTrader):
    """Tracked V2 trader with immutable identity per active symbol."""

    def _place_signal(self, signal, current_price: float):
        symbol = signal.symbol

        if symbol in self.positions or symbol in self.pending_orders:
            logger.info(
                f"[{symbol}] New signal ignored: active trade keeps ID "
                f"{self.get_trade_id(symbol) or 'unknown'}"
            )
            return None

        if not self.config.paper_trade:
            try:
                actual = self.exchange.get_position(symbol)
            except Exception as exc:
                # Unknown exchange state is not a safe moment to create a new
                # identity or overwrite the persisted thesis.
                logger.warning(f"[{symbol}] Entry skipped: position read failed: {exc}")
                return None
            if actual and actual.get("size", 0) > 0:
                logger.info(
                    f"[{symbol}] New signal ignored: exchange position already exists; "
                    f"keeping trade ID {self.get_trade_id(symbol) or 'recovery-pending'}"
                )
                return None

        return super()._place_signal(signal, current_price)
