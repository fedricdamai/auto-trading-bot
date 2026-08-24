"""Safety wrapper for Trader V2 execution.

This module keeps the reaction-based strategy unchanged and hardens only the
exchange execution layer.

Production invariants:
- At most one opening order family may exist per symbol.
- A symbol with an exchange position can never receive another opening order.
- Entry, TP and SL are submitted together using Hyperliquid normalTpsl grouping.
- Entry is intentionally NOT reduce-only because it opens exposure.
- Both TP and SL are always reduce-only.
- A partially filled entry is treated as a live position immediately. Any
  remaining opening quantity is cancelled and the actual position is protected.
- Duplicate exchange entries trigger cleanup and a circuit breaker instead of
  allowing the position to grow unexpectedly.
- Fill/order propagation gets a short grace window before local state is dropped.
"""

from __future__ import annotations

import logging
import time

from bot.strategy_v2 import build_recovery_plan
from bot.trader_v2 import OpenPosition, PendingOrder
from bot.trader_v2_continuous import Trader as ContinuousTrader

logger = logging.getLogger(__name__)


class Trader(ContinuousTrader):
    """Reaction-S/R trader with a strict one-family-per-symbol execution model."""

    FILL_RESOLUTION_GRACE_SECONDS = 5.0
    FAMILY_VISIBILITY_TIMEOUT_SECONDS = 5.0

    def __init__(self, config, exchange, notifier=None):
        super().__init__(config, exchange, notifier)
        self._entry_submit_inflight: set[str] = set()
        self._missing_entry_since: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Exchange family inspection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_trigger(order: dict) -> bool:
        if order.get("isTrigger"):
            return True
        typ = str(order.get("orderType", ""))
        return typ.startswith("Stop") or typ.startswith("Take Profit")

    def _flatten_frontend_orders(self, symbol: str) -> list[dict]:
        """Return parent and child frontend orders, deduplicated by oid.

        Hyperliquid may expose normalTpsl protection as children of the resting
        entry before fill. The older adapter only inspected top-level orders,
        which made an attached bracket look invisible to local verification.
        """
        if self.config.paper_trade:
            return []

        info = getattr(self.exchange, "info", None)
        address = getattr(self.exchange, "address", None)
        if info is None or not address or not hasattr(info, "frontend_open_orders"):
            return []

        try:
            roots = info.frontend_open_orders(address)
        except Exception as exc:
            logger.warning(f"[{symbol}] frontend family read failed: {exc}")
            return []

        flattened: list[dict] = []

        def walk(raw, inherited_coin=None):
            if not isinstance(raw, dict):
                return
            item = dict(raw)
            coin = item.get("coin") or inherited_coin
            if coin:
                item["coin"] = coin
            if coin == symbol:
                flattened.append(item)
            for child in item.get("children", []) or []:
                walk(child, coin)

        for root in roots or []:
            walk(root)

        by_key: dict[str, dict] = {}
        anonymous = 0
        for order in flattened:
            oid = order.get("oid")
            if oid is None:
                anonymous += 1
                key = f"anon:{anonymous}:{order.get('orderType')}:{order.get('triggerPx')}"
            else:
                key = f"oid:{oid}"
            by_key[key] = order
        return list(by_key.values())

    def _family_state(self, symbol: str) -> tuple[list[dict], list[dict]]:
        """Return opening entries and TP/SL triggers for one symbol."""
        if self.config.paper_trade:
            return [], []

        raw = self._flatten_frontend_orders(symbol)
        if raw:
            entries = [o for o in raw if not self._is_trigger(o)]
            triggers = [o for o in raw if self._is_trigger(o)]
            return entries, triggers

        # Fallback for tests or adapters that do not expose raw frontend data.
        entries = list(self.exchange.get_open_orders_for_symbol(symbol))
        triggers = list(self.exchange.get_trigger_orders_for_symbol(symbol))
        return entries, triggers

    @staticmethod
    def _trigger_reduce_only(order: dict) -> bool:
        # Tests and old synthetic exchange data may omit this field. In live
        # frontendOpenOrders it is explicit and must be true.
        value = order.get("reduceOnly")
        if value is None:
            return False
        return bool(value)

    def _valid_protection_pair(
        self,
        triggers: list[dict],
        side: str,
        tp: float,
        sl: float,
    ) -> bool:
        if len(triggers) != 2:
            return False
        if not all(self._trigger_reduce_only(o) for o in triggers):
            return False

        parsed_tp, parsed_sl = self._extract_trigger_prices(triggers, side, 0)
        if parsed_tp is None or parsed_sl is None:
            return False

        tolerance = 0.002
        tp_ok = abs(parsed_tp - tp) / max(abs(tp), 1e-12) <= tolerance
        sl_ok = abs(parsed_sl - sl) / max(abs(sl), 1e-12) <= tolerance
        return tp_ok and sl_ok

    @staticmethod
    def _group_response_accepted(result: dict) -> bool:
        """Check that entry, TP and SL were all accepted by the bulk request."""
        raw = result.get("raw", result) if isinstance(result, dict) else {}
        try:
            statuses = raw["response"]["data"]["statuses"]
        except (KeyError, TypeError):
            return False
        if len(statuses) < 3:
            return False
        for status in statuses[:3]:
            if isinstance(status, dict) and "error" in status:
                return False
        return True

    def _wait_for_grouped_family(
        self,
        symbol: str,
        expected_oid,
        side: str,
        tp: float,
        sl: float,
    ) -> bool:
        """Require one opening order and two reduce-only attached triggers."""
        if self.config.paper_trade:
            return True

        deadline = time.time() + self.FAMILY_VISIBILITY_TIMEOUT_SECONDS
        while time.time() < deadline:
            entries, triggers = self._family_state(symbol)
            if len(entries) > 1:
                return False

            entry_ok = len(entries) == 1
            if entry_ok and expected_oid is not None:
                entry_ok = str(entries[0].get("oid")) == str(expected_oid)

            if entry_ok and self._valid_protection_pair(triggers, side, tp, sl):
                return True
            time.sleep(0.20)
        return False

    # ------------------------------------------------------------------
    # Entry placement
    # ------------------------------------------------------------------

    @staticmethod
    def _pending_from_signal(signal, result: dict, leverage: int) -> PendingOrder:
        return PendingOrder(
            symbol=signal.symbol,
            oid=result.get("oid"),
            price=signal.entry_price,
            quantity=float(result["amount"]),
            side=signal.side,
            kind=signal.kind,
            level_price=signal.level_price,
            strength=signal.strength,
            effective_strength=signal.strength,
            timeframes=signal.timeframes,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            placed_at=time.time(),
            leverage=leverage,
            signal_id=signal.signal_id,
            confirmation_candle=signal.confirmation_candle,
            expires_at=signal.valid_until,
        )

    def _place_signal(self, signal, current_price: float):
        sym = signal.symbol

        # Local idempotency plus a re-entrancy guard protect against concurrent
        # callbacks within one process. The process-level lock in main.py handles
        # accidental multiple bot processes on the same VPS.
        if sym in self.positions or sym in self.pending_orders:
            logger.info(f"[{sym}] Entry suppressed: symbol already locally active")
            return
        if sym in self._entry_submit_inflight:
            logger.warning(f"[{sym}] Entry suppressed: submission already in flight")
            return

        self._entry_submit_inflight.add(sym)
        try:
            self._switch(sym)
            live_price = self.exchange.get_ticker_price()
            if signal.side == "long" and signal.entry_price >= live_price:
                logger.info(f"[{sym}] Skip stale LONG: entry is already marketable")
                return
            if signal.side == "short" and signal.entry_price <= live_price:
                logger.info(f"[{sym}] Skip stale SHORT: entry is already marketable")
                return

            # Exchange truth is authoritative. Never submit another opener if a
            # position already exists for this symbol.
            if not self.config.paper_trade and self.exchange.get_position(sym):
                logger.warning(f"[{sym}] Exchange position already exists; refusing another opener")
                return

            if not self.config.paper_trade:
                entries, triggers = self._family_state(sym)
                if entries or triggers:
                    logger.warning(
                        f"[{sym}] Exchange family already exists before new entry "
                        f"(entries={len(entries)} triggers={len(triggers)}); cleaning and waiting"
                    )
                    if not self._cancel_and_verify_symbol(sym):
                        self._block_symbol(sym, "pre-entry family cleanup failed")
                    return

                # A second clean read protects against frontend propagation lag
                # and makes accidental duplicate submission much less likely.
                time.sleep(0.25)
                if self.exchange.get_position(sym):
                    logger.warning(f"[{sym}] Position appeared during pre-entry guard")
                    return
                entries, triggers = self._family_state(sym)
                if entries or triggers:
                    logger.warning(f"[{sym}] Order appeared during pre-entry guard; refusing duplicate")
                    return

            leverage = self._safe_leverage()
            self.config.hl_leverage = leverage
            if hasattr(self.exchange, "_set_leverage"):
                self.exchange._set_leverage()

            notional = self._compute_order_notional(signal, leverage)
            if notional <= 0:
                logger.info(f"[{sym}] Signal skipped: risk-sized notional below minimum")
                return

            # Entry + TP + SL are sent in one Hyperliquid normalTpsl request.
            # The adapter marks the opening order reduce_only=False and both
            # protection triggers reduce_only=True.
            if signal.side == "long":
                result = self.exchange.place_limit_buy(
                    signal.entry_price,
                    notional,
                    tp_price=signal.take_profit,
                    sl_price=signal.stop_loss,
                )
            else:
                result = self.exchange.place_limit_sell(
                    signal.entry_price,
                    notional,
                    tp_price=signal.take_profit,
                    sl_price=signal.stop_loss,
                )

            if not self.config.paper_trade and not self._group_response_accepted(result):
                logger.critical(f"[{sym}] Grouped entry response did not accept all three orders")
                self._cancel_and_verify_symbol(sym)
                self._block_symbol(sym, "entry/TP/SL grouped submission incomplete")
                return

            pending = self._pending_from_signal(signal, result, leverage)
            self.pending_orders[sym] = pending
            self.executed_signal_ids.add(signal.signal_id)

            if result.get("status") == "filled":
                self._on_order_filled(
                    sym,
                    pending,
                    float(result["price"]),
                    float(result["amount"]),
                )
                return

            if not self.config.paper_trade:
                if not self._wait_for_grouped_family(
                    sym,
                    pending.oid,
                    pending.side,
                    pending.take_profit,
                    pending.stop_loss,
                ):
                    entries, triggers = self._family_state(sym)
                    logger.critical(
                        f"[{sym}] Invalid grouped family after submission: "
                        f"entries={len(entries)} triggers={len(triggers)}"
                    )
                    self.pending_orders.pop(sym, None)
                    self._cancel_and_verify_symbol(sym)
                    self._block_symbol(sym, "grouped entry family not verifiable")
                    return

            logger.info(
                f"[{sym}] V2 GROUPED PENDING {signal.side.upper()} "
                f"entry={signal.entry_price:.4f} TP={signal.take_profit:.4f} "
                f"SL={signal.stop_loss:.4f} RR={signal.risk_reward:.2f}"
            )
            if self.notifier:
                self.notifier.notify_limit_order(
                    signal.side,
                    signal.entry_price,
                    f"{sym} confirmed {signal.kind}",
                    pending.quantity,
                    signal.stop_loss,
                    signal.take_profit,
                    leverage,
                    signal.strength,
                )
        finally:
            self._entry_submit_inflight.discard(sym)

    # ------------------------------------------------------------------
    # Pending family lifecycle
    # ------------------------------------------------------------------

    def _check_pending_order(self, symbol: str, current_price: float, now: float | None = None):
        order = self.pending_orders.get(symbol)
        if not order:
            return
        now = now or time.time()

        if now >= order.expires_at:
            self._cancel_pending_order(symbol, reason="30m_expiry")
            return

        if self.config.paper_trade:
            if order.side == "long" and current_price <= order.price:
                self._on_order_filled(symbol, order, order.price, order.quantity)
            elif order.side == "short" and current_price >= order.price:
                self._on_order_filled(symbol, order, order.price, order.quantity)
            return

        # Check position BEFORE checking whether the entry is still resting. A
        # partial fill can create a real position while the remainder stays open.
        position = self.exchange.get_position(symbol)
        if position and position.get("size", 0) > 0:
            if position["side"] != order.side:
                self._block_symbol(symbol, "pending family produced opposite-side position")
                return
            self._missing_entry_since.pop(symbol, None)
            self._on_order_filled(
                symbol,
                order,
                position["entry_price"],
                position["size"],
            )
            return

        entries, triggers = self._family_state(symbol)
        if len(entries) > 1:
            logger.critical(f"[{symbol}] Duplicate opening orders detected: {len(entries)}")
            self.pending_orders.pop(symbol, None)
            self._cancel_and_verify_symbol(symbol)
            self._block_symbol(symbol, "duplicate opening orders detected")
            return

        still_open = any(str(o.get("oid")) == str(order.oid) for o in entries)
        if still_open:
            self._missing_entry_since.pop(symbol, None)
            if not self._valid_protection_pair(
                triggers,
                order.side,
                order.take_profit,
                order.stop_loss,
            ):
                logger.critical(f"[{symbol}] Pending entry lost its reduce-only TP/SL bracket")
                self.pending_orders.pop(symbol, None)
                self._cancel_and_verify_symbol(symbol)
                self._block_symbol(symbol, "pending grouped family lost protection")
            return

        # A filled order can disappear from frontendOpenOrders before user_state
        # reflects the position. Do not forget the pending thesis after one read.
        missing_since = self._missing_entry_since.setdefault(symbol, now)
        if now - missing_since < self.FILL_RESOLUTION_GRACE_SECONDS:
            return

        position = self.exchange.get_position(symbol)
        if position and position.get("size", 0) > 0:
            self._missing_entry_since.pop(symbol, None)
            self._on_order_filled(
                symbol,
                order,
                position["entry_price"],
                position["size"],
            )
            return

        logger.info(f"[{symbol}] Grouped pending oid={order.oid} disappeared without fill")
        self._missing_entry_since.pop(symbol, None)
        self.pending_orders.pop(symbol, None)
        if triggers:
            self._cancel_and_verify_symbol(symbol)

    def _cancel_pending_order(self, symbol: str, reason: str = "manual") -> bool:
        order = self.pending_orders.get(symbol)
        if not order:
            return True
        order.cancel_requested = True
        self._switch(symbol)

        if self.config.paper_trade:
            self.pending_orders.pop(symbol, None)
            self._missing_entry_since.pop(symbol, None)
            return True

        # Cancelling a normalTpsl entry must cancel the whole family, not only
        # the opening order. This avoids orphan TP/SL children.
        position_before = self.exchange.get_position(symbol)
        if position_before and position_before.get("size", 0) > 0:
            self._on_order_filled(
                symbol,
                order,
                position_before["entry_price"],
                position_before["size"],
            )
            return False

        ok = self._cancel_and_verify_symbol(symbol)

        # Resolve a fill/cancel race after family cancellation.
        position_after = self.exchange.get_position(symbol)
        if position_after and position_after.get("size", 0) > 0:
            logger.warning(f"[{symbol}] Entry filled while grouped cancellation was in flight")
            self._on_order_filled(
                symbol,
                order,
                position_after["entry_price"],
                position_after["size"],
            )
            return False

        if not ok:
            self._block_symbol(symbol, "grouped pending cancellation not verified")
            return False

        self.pending_orders.pop(symbol, None)
        self._missing_entry_since.pop(symbol, None)
        logger.info(f"[{symbol}] Grouped pending signal cancelled ({reason}): {order.signal_id}")
        if self.notifier:
            self.notifier.notify_order_cancelled(
                order.side,
                order.level_price,
                f"{symbol} {order.kind}",
            )
        return True

    # ------------------------------------------------------------------
    # Protection verification
    # ------------------------------------------------------------------

    def _replace_protection(
        self,
        symbol: str,
        quantity: float,
        side: str,
        tp: float,
        sl: float,
    ) -> tuple[bool, list[int]]:
        """Build exactly one reduce-only TP and one reduce-only SL."""
        if self.config.paper_trade:
            return True, []
        self._switch(symbol)

        # Cancel every visible protection order, including children if present.
        _, existing = self._family_state(symbol)
        for trigger in existing:
            oid = trigger.get("oid")
            if oid is None:
                continue
            try:
                self.exchange.cancel_order(float(trigger.get("limitPx", 0) or 0), int(oid))
            except Exception as exc:
                logger.warning(f"[{symbol}] Protection cancel oid={oid} failed: {exc}")

        cancel_deadline = time.time() + 3.0
        while time.time() < cancel_deadline:
            _, remaining = self._family_state(symbol)
            if not remaining:
                break
            time.sleep(0.20)
        else:
            logger.error(f"[{symbol}] Existing protection could not be cleared")
            return False, []

        result = self.exchange.place_tp_sl_orders(quantity, side, tp, sl)
        if isinstance(result, dict) and any(k in result for k in ("error", "tp_error", "sl_error")):
            logger.error(f"[{symbol}] TP/SL placement returned error: {result}")
            return False, []

        deadline = time.time() + self.FAMILY_VISIBILITY_TIMEOUT_SECONDS
        triggers: list[dict] = []
        while time.time() < deadline:
            _, triggers = self._family_state(symbol)
            if self._valid_protection_pair(triggers, side, tp, sl):
                oids = [int(o["oid"]) for o in triggers if o.get("oid") is not None]
                logger.info(
                    f"[{symbol}] Reduce-only protection verified: "
                    f"TP={tp:.4f} SL={sl:.4f} oids={oids}"
                )
                return True, oids
            time.sleep(0.20)

        logger.error(
            f"[{symbol}] Protection verification failed: triggers={len(triggers)} "
            f"reduce_only={[o.get('reduceOnly') for o in triggers]}"
        )
        return False, []

    # ------------------------------------------------------------------
    # Cleanup verification
    # ------------------------------------------------------------------

    def _cancel_and_verify_symbol(self, symbol: str) -> bool:
        if self.config.paper_trade:
            return True
        self._switch(symbol)

        try:
            self.exchange.cancel_orders_for_symbol(symbol)
        except Exception as exc:
            logger.warning(f"[{symbol}] broad family cancel failed: {exc}")

        # If grouped children survive parent cancellation, cancel their oids
        # explicitly. This is intentionally idempotent.
        for _ in range(3):
            entries, triggers = self._family_state(symbol)
            leftovers = entries + triggers
            if not leftovers:
                return True
            for item in leftovers:
                oid = item.get("oid")
                if oid is None:
                    continue
                try:
                    self.exchange.cancel_order(float(item.get("limitPx", 0) or 0), int(oid))
                except Exception:
                    pass
            time.sleep(0.25)

        deadline = time.time() + 3.0
        while time.time() < deadline:
            entries, triggers = self._family_state(symbol)
            if not entries and not triggers:
                return True
            time.sleep(0.20)

        entries, triggers = self._family_state(symbol)
        logger.error(
            f"[{symbol}] family cleanup verification failed: "
            f"entries={len(entries)} triggers={len(triggers)}"
        )
        return False
