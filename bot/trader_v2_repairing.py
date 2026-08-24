"""Repair-only execution policy for Trader V2.

This is the production wrapper above ``trader_v2_guarded``.

User-required invariant:
- NEVER market-close a position merely because TP/SL placement or verification
  failed.
- The signal's already-calculated TP/SL remain authoritative after fill.
- A protection failure blocks NEW entries for that symbol but keeps the live
  position and local/pending thesis intact.
- The exchange-authoritative watchdog retries protection on every runtime tick
  until exactly one reduce-only TP and one reduce-only SL exist for the actual
  position size.

Manual close commands and normal TP/SL exits are unaffected. This policy only
removes automatic emergency flattening caused by protection-repair failures.
"""

from __future__ import annotations

import logging
import time

from bot.trader_v2_guarded import Trader as GuardedTrader

logger = logging.getLogger(__name__)


class Trader(GuardedTrader):
    """Guarded V2 trader that only repairs protection and never auto-flattens."""

    def __init__(self, config, exchange, notifier=None):
        super().__init__(config, exchange, notifier)
        self.protection_repair_needed: dict[str, str] = {}
        self.protection_repair_last_attempt: dict[str, float] = {}

    def _emergency_flatten_unprotected(self, symbol: str, actual: dict, reason: str):
        """Legacy hook overridden intentionally: NEVER market-close here.

        Parent execution code calls this hook when a protection invariant cannot
        be established immediately. Older behavior flattened the position. The
        production policy is now to preserve the live position, preserve its
        original signal TP/SL state, block new exposure for this symbol, and let
        the watchdog retry on the next tick.
        """
        self.protection_repair_needed[symbol] = reason
        self.protection_repair_last_attempt[symbol] = time.time()

        # Blocking only prevents NEW entries. It does not stop the watchdog from
        # seeing and repairing the already-live exchange position.
        if symbol not in self.blocked_symbols:
            self._block_symbol(symbol, f"protection repair pending: {reason}")

        logger.critical(
            f"[{symbol}] PROTECTION REPAIR PENDING: {reason}. "
            f"Keeping {actual.get('side')} position open qty={actual.get('size')} "
            "and retrying TP/SL on subsequent ticks. NO automatic market close."
        )

    def _repair_known_position(self, symbol: str, actual: dict, pos) -> bool:
        ok = super()._repair_known_position(symbol, actual, pos)
        if ok:
            if symbol in self.protection_repair_needed:
                logger.info(
                    f"[{symbol}] PROTECTION REPAIRED using original signal levels: "
                    f"TP={pos.take_profit:.4f} SL={pos.stop_loss:.4f} "
                    f"qty={actual['size']}"
                )
            self.protection_repair_needed.pop(symbol, None)
            self.protection_repair_last_attempt.pop(symbol, None)
        else:
            self.protection_repair_last_attempt[symbol] = time.time()
        return ok

    def _on_order_filled(self, symbol, order, fill_price: float, fill_qty: float):
        """Use the exact TP/SL already calculated and sent to Telegram.

        ``order.take_profit`` and ``order.stop_loss`` are the same numbers passed
        to the Telegram notification when the limit family was created. The
        guarded parent uses those exact values for post-fill protection. If the
        exchange does not accept or expose them immediately, the overridden
        failure hook above keeps this pending thesis alive so the next tick can
        retry rather than closing the position.
        """
        return super()._on_order_filled(symbol, order, fill_price, fill_qty)

    def _recover_orphan_position(self, symbol: str, actual: dict) -> bool:
        ok = super()._recover_orphan_position(symbol, actual)
        if ok:
            self.protection_repair_needed.pop(symbol, None)
            self.protection_repair_last_attempt.pop(symbol, None)
        else:
            self.protection_repair_last_attempt[symbol] = time.time()
        return ok
