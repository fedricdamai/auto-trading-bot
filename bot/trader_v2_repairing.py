"""Repair-only execution policy for Trader V2.

This is the production wrapper above ``trader_v2_guarded``.

User-required invariants:
- NEVER market-close a position merely because TP/SL placement or verification
  failed.
- The signal's already-calculated TP/SL remain authoritative after fill.
- Those exact signal levels are persisted so a restart can recover them.
- A protection failure blocks NEW entries for that symbol but keeps the live
  position and thesis intact.
- The exchange-authoritative watchdog retries protection on every runtime tick
  until exactly one reduce-only TP and one reduce-only SL exist for the actual
  position size.

Manual close commands and normal TP/SL exits are unaffected. This policy only
removes automatic emergency flattening caused by protection-repair failures.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import time

from bot.trader_v2 import PendingOrder
from bot.trader_v2_guarded import Trader as GuardedTrader

logger = logging.getLogger(__name__)


class Trader(GuardedTrader):
    """Guarded V2 trader that only repairs protection and never auto-flattens."""

    THESIS_STATE_FILE = Path("v2_trade_thesis.json")

    def __init__(self, config, exchange, notifier=None):
        super().__init__(config, exchange, notifier)
        self.protection_repair_needed: dict[str, str] = {}
        self.protection_repair_last_attempt: dict[str, float] = {}
        self._repair_blocked_symbols: set[str] = set()
        self._trade_theses: dict[str, dict] = self._load_trade_theses()

    # ------------------------------------------------------------------
    # Exact signal thesis persistence
    # ------------------------------------------------------------------

    def _load_trade_theses(self) -> dict[str, dict]:
        try:
            if not self.THESIS_STATE_FILE.exists():
                return {}
            raw = json.loads(self.THESIS_STATE_FILE.read_text())
            return raw if isinstance(raw, dict) else {}
        except Exception as exc:
            logger.warning(f"Could not load V2 trade thesis state: {exc}")
            return {}

    def _save_trade_theses(self):
        try:
            tmp = self.THESIS_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._trade_theses, indent=2, sort_keys=True))
            tmp.replace(self.THESIS_STATE_FILE)
        except Exception as exc:
            logger.warning(f"Could not persist V2 trade thesis state: {exc}")

    def _remember_signal_thesis(self, signal):
        self._trade_theses[signal.symbol] = {
            "signal_id": signal.signal_id,
            "symbol": signal.symbol,
            "side": signal.side,
            "kind": signal.kind,
            "level_price": float(signal.level_price),
            "entry_price": float(signal.entry_price),
            "stop_loss": float(signal.stop_loss),
            "take_profit": float(signal.take_profit),
            "strength": float(signal.strength),
            "timeframes": list(signal.timeframes or []),
            "created_at": float(signal.created_at),
        }
        self._save_trade_theses()

    def _forget_signal_thesis(self, symbol: str):
        if self._trade_theses.pop(symbol, None) is not None:
            self._save_trade_theses()

    def _matching_saved_thesis(self, symbol: str, actual: dict) -> dict | None:
        thesis = self._trade_theses.get(symbol)
        if not thesis or thesis.get("side") != actual.get("side"):
            return None
        try:
            expected = float(thesis.get("entry_price", 0) or 0)
            actual_entry = float(actual.get("entry_price", 0) or 0)
        except (TypeError, ValueError):
            return None
        if expected <= 0 or actual_entry <= 0:
            return None
        # Exact fill price can differ slightly from the resting limit. Reject a
        # clearly stale thesis, but allow normal execution/slippage differences.
        if abs(actual_entry - expected) / expected > 0.03:
            return None
        return thesis

    def _place_signal(self, signal, current_price: float):
        # Persist BEFORE submitting so the exact TP/SL survive a process crash
        # immediately after Hyperliquid accepts the grouped order.
        self._remember_signal_thesis(signal)
        result = super()._place_signal(signal, current_price)

        active = signal.symbol in self.pending_orders or signal.symbol in self.positions
        if not active and not self.config.paper_trade:
            try:
                active = bool(self.exchange.get_position(signal.symbol))
            except Exception:
                # Unknown exchange state is not a reason to discard the thesis.
                active = True
        if not active:
            self._forget_signal_thesis(signal.symbol)
        return result

    # ------------------------------------------------------------------
    # Repair-only failure policy
    # ------------------------------------------------------------------

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
            self._repair_blocked_symbols.add(symbol)

        logger.critical(
            f"[{symbol}] PROTECTION REPAIR PENDING: {reason}. "
            f"Keeping {actual.get('side')} position open qty={actual.get('size')} "
            "and retrying TP/SL on subsequent ticks. NO automatic market close."
        )

    def _clear_repair_flag(self, symbol: str, tp: float, sl: float, quantity: float):
        if symbol in self.protection_repair_needed:
            logger.info(
                f"[{symbol}] PROTECTION REPAIRED using stored signal levels: "
                f"TP={tp:.4f} SL={sl:.4f} qty={quantity}"
            )
        self.protection_repair_needed.pop(symbol, None)
        self.protection_repair_last_attempt.pop(symbol, None)
        if symbol in self._repair_blocked_symbols:
            self.blocked_symbols.discard(symbol)
            self._repair_blocked_symbols.discard(symbol)
            logger.info(f"[{symbol}] New entries unblocked after TP/SL repair")

    def _repair_known_position(self, symbol: str, actual: dict, pos) -> bool:
        ok = super()._repair_known_position(symbol, actual, pos)
        if ok:
            self._clear_repair_flag(
                symbol,
                float(pos.take_profit),
                float(pos.stop_loss),
                float(actual["size"]),
            )
        else:
            self.protection_repair_last_attempt[symbol] = time.time()
        return ok

    def _on_order_filled(self, symbol, order, fill_price: float, fill_qty: float):
        """Use the exact TP/SL already calculated and sent to Telegram."""
        result = super()._on_order_filled(symbol, order, fill_price, fill_qty)
        pos = self.positions.get(symbol)
        if pos is not None:
            self._clear_repair_flag(
                symbol,
                float(pos.take_profit),
                float(pos.stop_loss),
                float(pos.quantity),
            )
        return result

    # ------------------------------------------------------------------
    # Restart/orphan recovery
    # ------------------------------------------------------------------

    def _pending_from_saved_thesis(self, symbol: str, actual: dict, thesis: dict) -> PendingOrder:
        now = time.time()
        return PendingOrder(
            symbol=symbol,
            oid=None,
            price=float(thesis["entry_price"]),
            quantity=float(actual["size"]),
            side=str(thesis["side"]),
            kind=str(thesis.get("kind", "support" if actual["side"] == "long" else "resistance")),
            level_price=float(thesis.get("level_price", thesis["entry_price"])),
            strength=float(thesis.get("strength", 0.0)),
            effective_strength=float(thesis.get("strength", 0.0)),
            timeframes=list(thesis.get("timeframes", [])),
            stop_loss=float(thesis["stop_loss"]),
            take_profit=float(thesis["take_profit"]),
            placed_at=float(thesis.get("created_at", now)),
            leverage=self._safe_leverage(),
            signal_id=str(thesis.get("signal_id", f"{symbol}:recovered")),
            confirmation_candle="recovered",
            expires_at=0.0,
        )

    def _recover_orphan_position(self, symbol: str, actual: dict) -> bool:
        thesis = self._matching_saved_thesis(symbol, actual)
        if thesis is not None:
            order = self._pending_from_saved_thesis(symbol, actual, thesis)
            logger.warning(
                f"[{symbol}] WATCHDOG restoring ORIGINAL saved protection: "
                f"TP={order.take_profit:.4f} SL={order.stop_loss:.4f}"
            )

            # Keep trying to freeze position size, but never close exposure if
            # the remaining opener cannot be cancelled immediately.
            if not self._cancel_remaining_openers(symbol):
                self._emergency_flatten_unprotected(
                    symbol, actual, "saved-thesis recovery still has opening quantity"
                )

            ok, oids = self._replace_protection(
                symbol,
                float(actual["size"]),
                actual["side"],
                order.take_profit,
                order.stop_loss,
            )
            if not ok:
                # Install local state anyway so every following watchdog tick has
                # the exact original TP/SL values available for another retry.
                self._install_position_state(symbol, order, actual, [])
                self._emergency_flatten_unprotected(
                    symbol, actual, "saved-thesis TP/SL recovery pending"
                )
                return False

            self._install_position_state(symbol, order, actual, oids)
            self._clear_repair_flag(
                symbol, order.take_profit, order.stop_loss, float(actual["size"])
            )
            return True

        ok = super()._recover_orphan_position(symbol, actual)
        if ok:
            pos = self.positions.get(symbol)
            if pos is not None:
                self._clear_repair_flag(
                    symbol,
                    float(pos.take_profit),
                    float(pos.stop_loss),
                    float(pos.quantity),
                )
            else:
                self.protection_repair_needed.pop(symbol, None)
                self.protection_repair_last_attempt.pop(symbol, None)
        else:
            self.protection_repair_last_attempt[symbol] = time.time()
        return ok

    def _startup_reconcile(self):
        super()._startup_reconcile()
        if self.config.paper_trade:
            return

        # Base reconciliation may have rebuilt generic structural protection for
        # an exchange position. If we have the exact original signal thesis,
        # restore those original TP/SL values instead.
        for symbol, pos in list(self.positions.items()):
            try:
                actual = self.exchange.get_position(symbol)
            except Exception:
                actual = None
            if not actual:
                continue
            thesis = self._matching_saved_thesis(symbol, actual)
            if thesis is None:
                continue
            pos.stop_loss = float(thesis["stop_loss"])
            pos.take_profit = float(thesis["take_profit"])
            pos.initial_sl = pos.stop_loss
            pos.initial_tp = pos.take_profit
            logger.warning(
                f"[{symbol}] Startup restoring saved signal TP/SL: "
                f"TP={pos.take_profit:.4f} SL={pos.stop_loss:.4f}"
            )
            self._repair_known_position(symbol, actual, pos)

    # ------------------------------------------------------------------
    # Thesis cleanup after explicit/normal exits
    # ------------------------------------------------------------------

    def _finalize_external_exit(self, symbol: str, exit_price: float, reason: str):
        result = super()._finalize_external_exit(symbol, exit_price, reason)
        self._forget_signal_thesis(symbol)
        return result

    def _close_position(self, symbol: str, exit_price: float, reason: str):
        result = super()._close_position(symbol, exit_price, reason)
        self._forget_signal_thesis(symbol)
        return result
