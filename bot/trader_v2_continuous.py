"""Continuous signal evaluation and exchange-safety layer for Trader V2.

Trading decisions are re-evaluated every minute from closed 30m/1h/4h data.
Exchange state is managed every normal bot tick (1s by default).

Additional safety:
- a vanished entry order gets a fill-resolution grace period before local state
  is discarded, preventing exchange-state propagation races
- any exchange position that is not represented locally is recovered and
  protected on the next tick
- TP/SL placement is verified with a multi-second retry window rather than one
  immediate frontend-open-orders read
"""

from __future__ import annotations

import logging
import time

from bot.strategy_v2 import build_signal, build_recovery_plan
from bot.trader_v2 import Trader as CandleTrader, OpenPosition

logger = logging.getLogger(__name__)


class Trader(CandleTrader):
    def __init__(self, config, exchange, notifier=None):
        super().__init__(config, exchange, notifier)
        self._last_continuous_scan_at = 0.0
        self._last_scan_completed_at = 0.0
        self._last_heartbeat_at = 0.0
        self._entry_resolution_started: dict[str, float] = {}

    def _signal_scan_interval(self) -> float:
        return max(
            5.0,
            float(getattr(self.config, "v2_signal_scan_interval_seconds", 60.0)),
        )

    def _heartbeat_interval(self) -> float:
        return max(
            60.0,
            float(getattr(self.config, "v2_heartbeat_interval_seconds", 600.0)),
        )

    def run_once(self) -> dict:
        if self._synced and not self.config.paper_trade:
            self._reconcile_untracked_exchange_positions()

        previous_bucket = self._last_signal_bucket
        summary = super().run_once()
        now = time.time()

        if not self.config.paper_trade:
            self._reconcile_untracked_exchange_positions()

        if self._last_signal_bucket != previous_bucket:
            self._last_continuous_scan_at = now
            self._last_scan_completed_at = now
            self._maybe_send_heartbeat(now)
            return summary

        if now - self._last_continuous_scan_at >= self._signal_scan_interval():
            self._last_continuous_scan_at = now
            self._revalidate_pending_signals(now)
            self._on_new_30m_candle(now)
            self._last_scan_completed_at = now

        self._maybe_send_heartbeat(now)
        return summary

    def _check_pending_order(self, symbol: str, current_price: float, now: float | None = None):
        order = self.pending_orders.get(symbol)
        if not order:
            self._entry_resolution_started.pop(symbol, None)
            return

        now = now or time.time()
        if now >= order.expires_at:
            self._entry_resolution_started.pop(symbol, None)
            self._cancel_pending_order(symbol, reason="30m_expiry")
            return

        if self.config.paper_trade:
            return super()._check_pending_order(symbol, current_price, now)

        open_orders = self.exchange.get_open_orders_for_symbol(symbol)
        still_open = any(str(o.get("oid")) == str(order.oid) for o in open_orders)
        if still_open:
            self._entry_resolution_started.pop(symbol, None)
            return

        position = self.exchange.get_position(symbol)
        if position and position["size"] > 0:
            self._entry_resolution_started.pop(symbol, None)
            if position["side"] != order.side:
                self._block_symbol(symbol, "entry disappeared into opposite-side position")
                return
            self._on_order_filled(
                symbol, order, position["entry_price"], position["size"],
            )
            return

        started = self._entry_resolution_started.setdefault(symbol, now)
        grace = 5.0
        if now - started < grace:
            if now - started < 1.5:
                logger.warning(
                    f"[{symbol}] Entry oid={order.oid} disappeared; waiting for fill-state "
                    f"propagation before discarding local order"
                )
            return

        logger.info(
            f"[{symbol}] Pending oid={order.oid} disappeared without a position "
            f"after {grace:.0f}s resolution window"
        )
        self.pending_orders.pop(symbol, None)
        self._entry_resolution_started.pop(symbol, None)

    def _reconcile_untracked_exchange_positions(self):
        try:
            exchange_positions = {
                p["coin"]: p for p in self.exchange.get_all_positions()
                if p and p.get("coin")
            }
        except Exception as exc:
            logger.warning(f"Exchange-position reconciliation failed: {exc}")
            return

        configured = set(self._get_symbols())
        for symbol, pos_data in exchange_positions.items():
            if symbol not in configured or symbol in self.positions:
                continue

            self._switch(symbol)
            pending = self.pending_orders.get(symbol)
            if pending:
                logger.warning(
                    f"[{symbol}] Found exchange position not yet tracked locally; "
                    "resolving pending entry as FILLED"
                )
                self._entry_resolution_started.pop(symbol, None)
                self._on_order_filled(
                    symbol,
                    pending,
                    pos_data["entry_price"],
                    pos_data["size"],
                )
                continue

            try:
                current_price = self.exchange.get_ticker_price()
                recovery, levels = build_recovery_plan(
                    self.exchange,
                    self.config,
                    symbol,
                    entry=pos_data["entry_price"],
                    side=pos_data["side"],
                    current_price=current_price,
                )
                self.known_levels[symbol] = {lv.price: lv for lv in levels}

                triggers = self.exchange.get_trigger_orders_for_symbol(symbol)
                parsed_tp, parsed_sl = self._extract_trigger_prices(
                    triggers, pos_data["side"], pos_data["entry_price"]
                )
                if len(triggers) == 2 and parsed_tp and parsed_sl:
                    tp, sl = parsed_tp, parsed_sl
                    trigger_oids = [
                        int(o["oid"]) for o in triggers if o.get("oid") is not None
                    ]
                else:
                    tp, sl = recovery.take_profit, recovery.stop_loss
                    ok, trigger_oids = self._replace_protection(
                        symbol, pos_data["size"], pos_data["side"], tp, sl
                    )
                    if not ok:
                        logger.critical(
                            f"[{symbol}] Orphan position could not be protected; "
                            "emergency market close"
                        )
                        self.exchange.place_market_close(
                            pos_data["size"], side=pos_data["side"]
                        )
                        self._cancel_and_verify_symbol(symbol)
                        self._block_symbol(
                            symbol, "orphan exchange position protection failed"
                        )
                        continue

                lev = self._safe_leverage()
                self.positions[symbol] = OpenPosition(
                    symbol=symbol,
                    entry_price=pos_data["entry_price"],
                    quantity=pos_data["size"],
                    side=pos_data["side"],
                    kind="support" if pos_data["side"] == "long" else "resistance",
                    level_price=recovery.level_price,
                    stop_loss=sl,
                    take_profit=tp,
                    initial_sl=sl,
                    initial_tp=tp,
                    highest_price=current_price,
                    lowest_price=current_price,
                    strength=recovery.strength,
                    timeframes=recovery.timeframes,
                    filled_at=time.time(),
                    leverage=lev,
                    last_synced_sl=sl,
                    last_synced_tp=tp,
                    trigger_oids=trigger_oids,
                    last_protection_check=time.time(),
                )
                logger.critical(
                    f"[{symbol}] RECOVERED untracked {pos_data['side'].upper()} "
                    f"{pos_data['size']} @ {pos_data['entry_price']:.4f} | "
                    f"SL={sl:.4f} TP={tp:.4f}"
                )
            except Exception as exc:
                logger.critical(f"[{symbol}] Untracked-position recovery failed: {exc}")

        self._switch(self.primary_symbol)

    def _replace_protection(
        self,
        symbol: str,
        quantity: float,
        side: str,
        tp: float,
        sl: float,
    ) -> tuple[bool, list[int]]:
        """Place and verify TP/SL with enough time for HL state propagation."""
        if self.config.paper_trade:
            return True, []
        self._switch(symbol)

        self.exchange.cancel_trigger_orders_for_symbol(symbol)
        clear_deadline = time.time() + 3.0
        while time.time() < clear_deadline:
            if not self.exchange.get_trigger_orders_for_symbol(symbol):
                break
            time.sleep(0.25)
        if self.exchange.get_trigger_orders_for_symbol(symbol):
            logger.error(f"[{symbol}] Existing TP/SL could not be cleared")
            return False, []

        result = self.exchange.place_tp_sl_orders(quantity, side, tp, sl)
        if isinstance(result, dict) and any(
            key in result for key in ("error", "tp_error", "sl_error")
        ):
            logger.error(f"[{symbol}] TP/SL placement returned error: {result}")

        verify_deadline = time.time() + 5.0
        last_count = 0
        while time.time() < verify_deadline:
            triggers = self.exchange.get_trigger_orders_for_symbol(symbol)
            last_count = len(triggers)
            if len(triggers) == 2:
                parsed_tp, parsed_sl = self._extract_trigger_prices(
                    triggers, side, 0
                )
                tp_ok = (
                    parsed_tp is not None
                    and abs(parsed_tp - tp) / max(abs(tp), 1e-12) <= 0.002
                )
                sl_ok = (
                    parsed_sl is not None
                    and abs(parsed_sl - sl) / max(abs(sl), 1e-12) <= 0.002
                )
                if tp_ok and sl_ok:
                    oids = [
                        int(o["oid"]) for o in triggers if o.get("oid") is not None
                    ]
                    logger.info(
                        f"[{symbol}] Protection verified: TP={tp:.4f} "
                        f"SL={sl:.4f} oids={oids}"
                    )
                    return True, oids
            elif len(triggers) > 2:
                logger.error(
                    f"[{symbol}] Duplicate protection detected during verification: "
                    f"{len(triggers)} triggers"
                )
                break
            time.sleep(0.25)

        logger.error(
            f"[{symbol}] TP/SL verification timed out; expected 2 protection "
            f"triggers, last observed={last_count}"
        )
        self.exchange.cancel_trigger_orders_for_symbol(symbol)
        return False, []

    def _revalidate_pending_signals(self, now: float):
        for symbol in list(self.pending_orders.keys()):
            order = self.pending_orders.get(symbol)
            if not order:
                continue
            if order.expires_at and now >= order.expires_at:
                continue

            try:
                self._switch(symbol)
                current_price = self.exchange.get_ticker_price()
                signal, levels, regime = build_signal(
                    self.exchange,
                    self.config,
                    symbol,
                    current_price=current_price,
                    now=now,
                )
                self.known_levels[symbol] = {lv.price: lv for lv in levels}
                self.last_trend_bias[symbol] = regime

                if signal is None:
                    logger.info(
                        f"[{symbol}] Pending {order.side.upper()} invalidated by "
                        f"fresh S/R scan; cancelling signal={order.signal_id}"
                    )
                    self._cancel_pending_order(symbol, reason="setup_invalidated")
                    continue

                if signal.signal_id != order.signal_id or signal.side != order.side:
                    logger.info(
                        f"[{symbol}] Pending S/R thesis changed "
                        f"{order.signal_id} -> {signal.signal_id}; cancelling old entry"
                    )
                    self._cancel_pending_order(symbol, reason="setup_changed")
                    continue

            except Exception as exc:
                logger.warning(f"[{symbol}] Pending setup revalidation failed: {exc}")

        self._switch(self.primary_symbol)

    def _describe_idle_symbol(self, symbol: str) -> str:
        if symbol in self.blocked_symbols:
            return "BLOCKED: exchange state needs review"

        if symbol in self.positions:
            pos = self.positions[symbol]
            return f"POSITION {pos.side.upper()} @ {pos.entry_price:.4g}"

        if symbol in self.pending_orders:
            order = self.pending_orders[symbol]
            return f"PENDING {order.side.upper()} @ {order.price:.4g}"

        now = time.time()
        cooldown = self.cooldown_until.get(symbol, 0.0)
        if cooldown > now:
            mins = max(1, int((cooldown - now + 59) // 60))
            return f"cooldown ~{mins}m"

        levels = list(self.known_levels.get(symbol, {}).values())
        min_strength = float(getattr(self.config, "v2_min_level_strength", 65.0))
        supports = [
            lv for lv in levels
            if lv.kind == "support" and lv.strength >= min_strength
        ]
        resistances = [
            lv for lv in levels
            if lv.kind == "resistance" and lv.strength >= min_strength
        ]
        if not supports and not resistances:
            return "no validated 30m/1h S/R"

        pieces = []
        if supports:
            s = max(supports, key=lambda lv: (lv.strength, lv.touches))
            pieces.append(f"S {s.price:.4g}({','.join(s.timeframes or [])})")
        if resistances:
            r = max(resistances, key=lambda lv: (lv.strength, lv.touches))
            pieces.append(f"R {r.price:.4g}({','.join(r.timeframes or [])})")
        return " | ".join(pieces) + "; waiting price/R:R"

    def _heartbeat_snapshot(self, now: float) -> dict:
        rows = []
        for symbol in self._get_symbols():
            regime = self.last_trend_bias.get(symbol)
            if regime:
                regime_text = f"{regime.direction} {regime.confidence:.0f}%"
            else:
                regime_text = "unknown"
            rows.append({
                "symbol": symbol,
                "regime": regime_text,
                "status": self._describe_idle_symbol(symbol),
            })

        mode = "PAPER"
        if not self.config.paper_trade:
            mode = "LIVE MAINNET" if self.config.hl_mainnet else "LIVE TESTNET"

        return {
            "mode": mode,
            "paused": self.paused,
            "scan_interval_seconds": int(self._signal_scan_interval()),
            "heartbeat_interval_seconds": int(self._heartbeat_interval()),
            "last_scan_at": self._last_scan_completed_at or now,
            "positions": len(self.positions),
            "pending": len(self.pending_orders),
            "blocked": len(self.blocked_symbols),
            "rows": rows,
        }

    def _maybe_send_heartbeat(self, now: float):
        if not self.notifier or not hasattr(self.notifier, "notify_heartbeat"):
            return
        if self._last_scan_completed_at <= 0:
            return
        if (
            self._last_heartbeat_at
            and now - self._last_heartbeat_at < self._heartbeat_interval()
        ):
            return

        snapshot = self._heartbeat_snapshot(now)
        try:
            self.notifier.notify_heartbeat(snapshot)
            self._last_heartbeat_at = now
        except Exception as exc:
            logger.warning(f"Telegram heartbeat delivery failed: {exc}")
