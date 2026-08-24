"""Trade-ID and lifecycle audit layer for production Trader V2.

This wrapper does not change strategy or execution behavior. It adds one stable
``trade_id`` to each trade thesis and writes structured lifecycle events from
trigger through final result.
"""

from __future__ import annotations

import logging
from pathlib import Path
import time

from bot.trade_lifecycle import TradeLifecycleLog, generate_trade_id
from bot.trader_v2_repairing import Trader as RepairingTrader

logger = logging.getLogger(__name__)


class Trader(RepairingTrader):
    """Repair-only V2 trader with persistent per-trade audit IDs."""

    LIFECYCLE_FILE = Path("trade_lifecycle.jsonl")

    def __init__(self, config, exchange, notifier=None):
        super().__init__(config, exchange, notifier)
        self.lifecycle = TradeLifecycleLog(self.LIFECYCLE_FILE)
        self._event_last: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # Trade ID helpers
    # ------------------------------------------------------------------

    def _ensure_signal_trade_id(self, signal) -> str:
        current = str(getattr(signal, "trade_id", "") or "")
        if current:
            return current

        existing = self._trade_theses.get(signal.symbol, {})
        if (
            existing.get("signal_id") == signal.signal_id
            and existing.get("trade_id")
        ):
            trade_id = str(existing["trade_id"])
        else:
            trade_id = generate_trade_id(signal.symbol, signal.side)

        # StrategySignal is a normal dataclass, not slotted, so attaching the
        # audit ID does not change strategy serialization or scoring behavior.
        setattr(signal, "trade_id", trade_id)
        return trade_id

    def get_trade_id(self, symbol: str) -> str:
        pending = self.pending_orders.get(symbol)
        if pending is not None:
            value = str(getattr(pending, "trade_id", "") or "")
            if value:
                return value

        pos = self.positions.get(symbol)
        if pos is not None:
            value = str(getattr(pos, "trade_id", "") or "")
            if value:
                return value

        thesis = self._trade_theses.get(symbol, {})
        return str(thesis.get("trade_id", "") or "")

    def _emit(
        self,
        trade_id: str,
        event: str,
        symbol: str,
        side: str = "",
        min_interval: float = 0.0,
        **details,
    ):
        if not trade_id:
            return
        now = time.time()
        key = (trade_id, event)
        if min_interval > 0 and now - self._event_last.get(key, 0.0) < min_interval:
            return
        self._event_last[key] = now
        self.lifecycle.event(trade_id, event, symbol, side, **details)
        logger.info(f"[{symbol}] [{trade_id}] lifecycle={event}")

    # ------------------------------------------------------------------
    # Thesis persistence with stable trade ID
    # ------------------------------------------------------------------

    def _recover_trade_theses_from_log(self) -> dict[str, dict]:
        recovered = super()._recover_trade_theses_from_log()
        for symbol, thesis in recovered.items():
            if not thesis.get("trade_id"):
                thesis["trade_id"] = generate_trade_id(
                    symbol, str(thesis.get("side", "long"))
                )
                thesis["legacy_trade_id"] = True
        return recovered

    def _remember_signal_thesis(self, signal):
        trade_id = self._ensure_signal_trade_id(signal)
        super()._remember_signal_thesis(signal)
        thesis = self._trade_theses.get(signal.symbol)
        if thesis is not None:
            thesis["trade_id"] = trade_id
            self._save_trade_theses()

    def _pending_from_saved_thesis(self, symbol: str, actual: dict, thesis: dict):
        pending = super()._pending_from_saved_thesis(symbol, actual, thesis)
        setattr(pending, "trade_id", str(thesis.get("trade_id", "") or ""))
        return pending

    # ------------------------------------------------------------------
    # Trigger and opening order
    # ------------------------------------------------------------------

    def _place_signal(self, signal, current_price: float):
        trade_id = self._ensure_signal_trade_id(signal)
        regime = getattr(signal, "regime", None)
        self._emit(
            trade_id,
            "TRIGGER_CREATED",
            signal.symbol,
            signal.side,
            signal_id=signal.signal_id,
            kind=signal.kind,
            level_price=float(signal.level_price),
            current_price=float(current_price),
            planned_entry=float(signal.entry_price),
            planned_sl=float(signal.stop_loss),
            planned_tp=float(signal.take_profit),
            risk_reward=float(signal.risk_reward),
            strength=float(signal.strength),
            timeframes=list(signal.timeframes or []),
            trend_direction=getattr(regime, "direction", "neutral"),
            trend_confidence=float(getattr(regime, "confidence", 0.0) or 0.0),
        )

        result = super()._place_signal(signal, current_price)

        pending = self.pending_orders.get(signal.symbol)
        if pending is not None:
            was_tagged = bool(getattr(pending, "trade_id", ""))
            setattr(pending, "trade_id", trade_id)
            if not was_tagged:
                self._emit(
                    trade_id,
                    "ORDER_PENDING",
                    signal.symbol,
                    signal.side,
                    exchange_oid=getattr(pending, "oid", None),
                    entry=float(pending.price),
                    quantity=float(pending.quantity),
                    tp=float(pending.take_profit),
                    sl=float(pending.stop_loss),
                    reduce_only_entry=False,
                    reduce_only_tp_sl=True,
                )
        elif signal.symbol not in self.positions:
            # The signal was evaluated but no live/pending trade family survived
            # placement guards. Keep the audit record even though no exposure
            # was created.
            self._emit(
                trade_id,
                "ORDER_NOT_OPENED",
                signal.symbol,
                signal.side,
                min_interval=30.0,
            )
        return result

    # ------------------------------------------------------------------
    # Fill and protection lifecycle
    # ------------------------------------------------------------------

    def _on_order_filled(self, symbol, order, fill_price: float, fill_qty: float):
        trade_id = str(
            getattr(order, "trade_id", "")
            or self._trade_theses.get(symbol, {}).get("trade_id", "")
            or ""
        )
        setattr(order, "trade_id", trade_id)

        result = super()._on_order_filled(symbol, order, fill_price, fill_qty)
        pos = self.positions.get(symbol)
        if pos is not None:
            setattr(pos, "trade_id", trade_id)
            if not getattr(pos, "_fill_event_logged", False):
                setattr(pos, "_fill_event_logged", True)
                self._emit(
                    trade_id,
                    "FILLED",
                    symbol,
                    pos.side,
                    actual_entry=float(pos.entry_price),
                    actual_quantity=float(pos.quantity),
                    planned_tp=float(pos.take_profit),
                    planned_sl=float(pos.stop_loss),
                    signal_id=getattr(pos, "signal_id", ""),
                )
                self._emit(
                    trade_id,
                    "PROTECTION_ACTIVE",
                    symbol,
                    pos.side,
                    tp=float(pos.take_profit),
                    sl=float(pos.stop_loss),
                    quantity=float(pos.quantity),
                    reduce_only=True,
                )
        elif symbol in self.protection_repair_needed:
            actual = None
            try:
                actual = self.exchange.get_position(symbol)
            except Exception:
                pass
            self._emit(
                trade_id,
                "FILL_PROTECTION_PENDING",
                symbol,
                order.side,
                min_interval=30.0,
                actual_entry=float(actual.get("entry_price", fill_price)) if actual else float(fill_price),
                actual_quantity=float(actual.get("size", fill_qty)) if actual else float(fill_qty),
                tp=float(order.take_profit),
                sl=float(order.stop_loss),
            )
        return result

    def _emergency_flatten_unprotected(self, symbol: str, actual: dict, reason: str):
        # Parent implementation is repair-only despite the legacy method name.
        trade_id = self.get_trade_id(symbol)
        self._emit(
            trade_id,
            "PROTECTION_REPAIR_PENDING",
            symbol,
            str(actual.get("side", "")),
            min_interval=30.0,
            reason=reason,
            quantity=float(actual.get("size", 0) or 0),
            entry=float(actual.get("entry_price", 0) or 0),
        )
        return super()._emergency_flatten_unprotected(symbol, actual, reason)

    def _clear_repair_flag(self, symbol: str, tp: float, sl: float, quantity: float):
        was_pending = symbol in self.protection_repair_needed
        trade_id = self.get_trade_id(symbol)
        side = ""
        pos = self.positions.get(symbol)
        if pos is not None:
            side = pos.side
        result = super()._clear_repair_flag(symbol, tp, sl, quantity)
        if was_pending:
            self._emit(
                trade_id,
                "PROTECTION_REPAIRED",
                symbol,
                side,
                tp=float(tp),
                sl=float(sl),
                quantity=float(quantity),
                reduce_only=True,
            )
        return result

    # ------------------------------------------------------------------
    # Cancellation and restart recovery
    # ------------------------------------------------------------------

    def _cancel_pending_order(self, symbol: str, reason: str = "manual") -> bool:
        order = self.pending_orders.get(symbol)
        trade_id = self.get_trade_id(symbol)
        side = order.side if order else ""
        result = super()._cancel_pending_order(symbol, reason=reason)
        if result and symbol not in self.pending_orders and symbol not in self.positions:
            self._emit(
                trade_id,
                "ORDER_CANCELLED",
                symbol,
                side,
                reason=reason,
            )
            self._forget_signal_thesis(symbol)
        return result

    def _startup_reconcile(self):
        result = super()._startup_reconcile()
        for symbol, pos in self.positions.items():
            trade_id = self.get_trade_id(symbol)
            if trade_id:
                setattr(pos, "trade_id", trade_id)
                self._emit(
                    trade_id,
                    "POSITION_RECOVERED_AFTER_RESTART",
                    symbol,
                    pos.side,
                    min_interval=5.0,
                    entry=float(pos.entry_price),
                    quantity=float(pos.quantity),
                    tp=float(pos.take_profit),
                    sl=float(pos.stop_loss),
                )
        return result

    # ------------------------------------------------------------------
    # Final result
    # ------------------------------------------------------------------

    @staticmethod
    def _result_details(pos, exit_price: float, reason: str) -> dict:
        if pos.side == "long":
            pnl_usd = pos.quantity * (exit_price - pos.entry_price)
            price_pnl = (exit_price - pos.entry_price) / pos.entry_price * 100
        else:
            pnl_usd = pos.quantity * (pos.entry_price - exit_price)
            price_pnl = (pos.entry_price - exit_price) / pos.entry_price * 100
        margin_pnl = price_pnl * pos.leverage
        outcome = "WIN" if pnl_usd > 0 else ("LOSS" if pnl_usd < 0 else "BREAKEVEN")
        return {
            "reason": reason,
            "outcome": outcome,
            "entry": float(pos.entry_price),
            "exit": float(exit_price),
            "quantity": float(pos.quantity),
            "pnl_usd": round(float(pnl_usd), 6),
            "price_pnl_pct": round(float(price_pnl), 6),
            "margin_pnl_pct": round(float(margin_pnl), 6),
            "planned_tp": float(pos.take_profit),
            "planned_sl": float(pos.stop_loss),
            "hold_seconds": max(0.0, time.time() - float(pos.filled_at or time.time())),
        }

    def _finalize_external_exit(self, symbol: str, exit_price: float, reason: str):
        pos = self.positions.get(symbol)
        trade_id = self.get_trade_id(symbol)
        side = pos.side if pos else ""
        details = self._result_details(pos, exit_price, reason) if pos else {"reason": reason}
        result = super()._finalize_external_exit(symbol, exit_price, reason)
        self._emit(trade_id, "EXIT", symbol, side, **details)
        return result

    def _close_position(self, symbol: str, exit_price: float, reason: str):
        pos = self.positions.get(symbol)
        trade_id = self.get_trade_id(symbol)
        side = pos.side if pos else ""
        details = self._result_details(pos, exit_price, reason) if pos else {"reason": reason}
        result = super()._close_position(symbol, exit_price, reason)
        self._emit(trade_id, "EXIT", symbol, side, **details)
        return result
