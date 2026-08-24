"""Final execution guard for Trader V2.

This layer keeps the reaction-based strategy and grouped entry family from
``trader_v2_safe`` but makes live exchange positions authoritative on every
runtime tick.

Invariants:
- One opening family per symbol remains enforced by the parent safe trader.
- A real position may never remain without exactly one reduce-only TP and SL.
- Partial fills cancel the remaining opener immediately.
- Existing valid grouped children are preserved after fill.
- If grouped children disappear during the fill transition, protection is
  rebuilt against the actual exchange position size.
- If local state is lost, the watchdog reconstructs or closes the position
  rather than leaving naked exposure.
"""

from __future__ import annotations

import logging
import time

from bot.strategy_v2 import build_recovery_plan
from bot.trader_v2 import OpenPosition, PendingOrder
from bot.trader_v2_safe import Trader as SafeTrader

logger = logging.getLogger(__name__)


class Trader(SafeTrader):
    """Safe V2 trader with an exchange-authoritative protection watchdog."""

    WATCHDOG_RETRY_SECONDS = 3.0

    def _trigger_size_matches(self, triggers: list[dict], quantity: float) -> bool:
        if len(triggers) != 2:
            return False
        for order in triggers:
            try:
                sz = abs(float(order.get("sz", 0) or 0))
            except (TypeError, ValueError):
                return False
            if quantity <= 0 or abs(sz - quantity) / max(quantity, 1e-12) > 0.01:
                return False
        return True

    def _valid_actual_protection(
        self,
        triggers: list[dict],
        side: str,
        tp: float,
        sl: float,
        quantity: float,
    ) -> bool:
        return (
            self._valid_protection_pair(triggers, side, tp, sl)
            and self._trigger_size_matches(triggers, quantity)
        )

    def _cancel_remaining_openers(self, symbol: str) -> bool:
        """Cancel only non-trigger opening orders after any position appears."""
        deadline = time.time() + self.WATCHDOG_RETRY_SECONDS
        while time.time() < deadline:
            entries, _ = self._family_state(symbol)
            if not entries:
                return True
            for entry in entries:
                oid = entry.get("oid")
                if oid is None:
                    continue
                try:
                    self.exchange.cancel_order(
                        float(entry.get("limitPx", 0) or 0), int(oid)
                    )
                except Exception as exc:
                    logger.warning(
                        f"[{symbol}] remaining opener cancel oid={oid} failed: {exc}"
                    )
            time.sleep(0.20)
        entries, _ = self._family_state(symbol)
        return not entries

    def _emergency_flatten_unprotected(self, symbol: str, actual: dict, reason: str):
        logger.critical(f"[{symbol}] {reason}; emergency-closing naked position")
        try:
            self.exchange.place_market_close(actual["size"], side=actual["side"])
        except Exception as exc:
            logger.critical(f"[{symbol}] emergency close failed: {exc}")
        finally:
            try:
                self._cancel_and_verify_symbol(symbol)
            except Exception:
                pass
            self.pending_orders.pop(symbol, None)
            self.positions.pop(symbol, None)
            self._missing_entry_since.pop(symbol, None)
            self._block_symbol(symbol, reason)

    def _install_position_state(
        self,
        symbol: str,
        order: PendingOrder,
        actual: dict,
        trigger_oids: list[int],
    ) -> OpenPosition:
        now = time.time()
        pos = OpenPosition(
            symbol=symbol,
            entry_price=float(actual["entry_price"]),
            quantity=float(actual["size"]),
            side=order.side,
            kind=order.kind,
            level_price=order.level_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            initial_sl=order.stop_loss,
            initial_tp=order.take_profit,
            highest_price=float(actual["entry_price"]),
            lowest_price=float(actual["entry_price"]),
            strength=order.strength,
            timeframes=order.timeframes,
            filled_at=now,
            leverage=order.leverage,
            last_synced_sl=order.stop_loss,
            last_synced_tp=order.take_profit,
            signal_id=order.signal_id,
            trigger_oids=trigger_oids,
            last_protection_check=now,
        )
        self.positions[symbol] = pos
        self.pending_orders.pop(symbol, None)
        self._missing_entry_since.pop(symbol, None)
        return pos

    def _on_order_filled(
        self,
        symbol: str,
        order: PendingOrder,
        fill_price: float,
        fill_qty: float,
    ):
        """Finalize a fill without unnecessarily destroying valid grouped TP/SL."""
        self._switch(symbol)

        if self.config.paper_trade:
            return super()._on_order_filled(symbol, order, fill_price, fill_qty)

        actual = self.exchange.get_position(symbol)
        if not actual or actual.get("size", 0) <= 0:
            # Keep pending state. Hyperliquid order/user state can propagate a
            # few ticks apart, so the pending checker will retry.
            logger.warning(f"[{symbol}] Fill transition waiting for exchange position state")
            return
        if actual["side"] != order.side:
            self._block_symbol(symbol, "fill side mismatch")
            return

        # Any partial fill is now the complete allowed position. Cancel the
        # remaining opening quantity before doing anything with protection.
        if not self._cancel_remaining_openers(symbol):
            self._emergency_flatten_unprotected(
                symbol, actual, "remaining opening quantity could not be cancelled"
            )
            return

        # Parent cancellation on a partially-filled normalTpsl family may or may
        # not preserve the children. Re-read exchange truth after cancellation.
        _, triggers = self._family_state(symbol)
        if self._valid_actual_protection(
            triggers,
            order.side,
            order.take_profit,
            order.stop_loss,
            float(actual["size"]),
        ):
            trigger_oids = [
                int(o["oid"]) for o in triggers if o.get("oid") is not None
            ]
            logger.info(
                f"[{symbol}] Preserving grouped reduce-only TP/SL after fill "
                f"qty={actual['size']}"
            )
        else:
            ok, trigger_oids = self._replace_protection(
                symbol,
                float(actual["size"]),
                order.side,
                order.take_profit,
                order.stop_loss,
            )
            if not ok:
                self._emergency_flatten_unprotected(
                    symbol, actual, "TP/SL protection failed after fill"
                )
                return

        self._install_position_state(symbol, order, actual, trigger_oids)
        logger.info(
            f"[{symbol}] V2 FILLED {order.side.upper()} signal={order.signal_id} "
            f"qty={actual['size']} entry={actual['entry_price']:.4f} "
            f"SL={order.stop_loss:.4f} TP={order.take_profit:.4f}"
        )
        if self.notifier:
            self.notifier.notify_entry(
                order.side,
                order.level_price,
                f"{symbol} {order.kind}",
                float(actual["entry_price"]),
                float(actual["size"]),
                order.stop_loss,
                order.take_profit,
                order.leverage,
            )

    def _repair_known_position(self, symbol: str, actual: dict, pos: OpenPosition) -> bool:
        """Ensure a locally-known position has no opener and exact protection."""
        self._switch(symbol)
        if not self._cancel_remaining_openers(symbol):
            self._emergency_flatten_unprotected(
                symbol, actual, "watchdog could not cancel remaining opener"
            )
            return False

        pos.quantity = float(actual["size"])
        pos.entry_price = float(actual["entry_price"])

        _, triggers = self._family_state(symbol)
        if self._valid_actual_protection(
            triggers,
            pos.side,
            pos.take_profit,
            pos.stop_loss,
            pos.quantity,
        ):
            pos.trigger_oids = [
                int(o["oid"]) for o in triggers if o.get("oid") is not None
            ]
            pos.last_protection_check = time.time()
            return True

        logger.warning(
            f"[{symbol}] WATCHDOG repairing protection: "
            f"triggers={len(triggers)} qty={actual['size']}"
        )
        ok, oids = self._replace_protection(
            symbol,
            pos.quantity,
            pos.side,
            pos.take_profit,
            pos.stop_loss,
        )
        if not ok:
            self._emergency_flatten_unprotected(
                symbol, actual, "watchdog TP/SL repair failed"
            )
            return False
        pos.trigger_oids = oids
        pos.last_protection_check = time.time()
        return True

    def _recover_orphan_position(self, symbol: str, actual: dict) -> bool:
        """Recover protection when exchange position exists but local state does not."""
        self._switch(symbol)
        try:
            current_price = self.exchange.get_ticker_price()
            recovery, levels = build_recovery_plan(
                self.exchange,
                self.config,
                symbol,
                entry=float(actual["entry_price"]),
                side=actual["side"],
                current_price=current_price,
            )
            self.known_levels[symbol] = {lv.price: lv for lv in levels}
        except Exception as exc:
            logger.critical(f"[{symbol}] orphan recovery plan failed: {exc}")
            self._emergency_flatten_unprotected(
                symbol, actual, "orphan position had no recoverable protection plan"
            )
            return False

        if not self._cancel_remaining_openers(symbol):
            self._emergency_flatten_unprotected(
                symbol, actual, "orphan position still had opening orders"
            )
            return False

        ok, oids = self._replace_protection(
            symbol,
            float(actual["size"]),
            actual["side"],
            recovery.take_profit,
            recovery.stop_loss,
        )
        if not ok:
            self._emergency_flatten_unprotected(
                symbol, actual, "orphan TP/SL recovery failed"
            )
            return False

        now = time.time()
        self.positions[symbol] = OpenPosition(
            symbol=symbol,
            entry_price=float(actual["entry_price"]),
            quantity=float(actual["size"]),
            side=actual["side"],
            kind="support" if actual["side"] == "long" else "resistance",
            level_price=recovery.level_price,
            stop_loss=recovery.stop_loss,
            take_profit=recovery.take_profit,
            initial_sl=recovery.stop_loss,
            initial_tp=recovery.take_profit,
            highest_price=current_price,
            lowest_price=current_price,
            strength=recovery.strength,
            timeframes=recovery.timeframes,
            filled_at=now,
            leverage=self._safe_leverage(),
            last_synced_sl=recovery.stop_loss,
            last_synced_tp=recovery.take_profit,
            trigger_oids=oids,
            last_protection_check=now,
        )
        logger.warning(
            f"[{symbol}] WATCHDOG recovered orphan {actual['side']} position "
            f"qty={actual['size']} TP={recovery.take_profit:.4f} "
            f"SL={recovery.stop_loss:.4f}"
        )
        return True

    def _watchdog_exchange_positions(self):
        if self.config.paper_trade:
            return

        configured = set(self._get_symbols())
        try:
            actual_positions = {
                p["coin"]: p for p in self.exchange.get_all_positions()
                if p.get("coin") in configured and p.get("size", 0) > 0
            }
        except Exception as exc:
            logger.error(f"Protection watchdog position read failed: {exc}")
            return

        for symbol, actual in actual_positions.items():
            pending = self.pending_orders.get(symbol)
            pos = self.positions.get(symbol)

            if pending and not pos:
                self._on_order_filled(
                    symbol,
                    pending,
                    float(actual["entry_price"]),
                    float(actual["size"]),
                )
                continue

            if pos:
                if pos.side != actual["side"]:
                    self.paused = True
                    self._block_symbol(
                        symbol, "watchdog found exchange/local side mismatch"
                    )
                    continue
                self._repair_known_position(symbol, actual, pos)
                continue

            # This is the exact SOL failure mode: exchange exposure exists but
            # local pending/position state no longer explains it.
            self._recover_orphan_position(symbol, actual)

    def run_once(self) -> dict:
        summary = super().run_once()
        if not self.config.paper_trade:
            self._watchdog_exchange_positions()
        return summary
