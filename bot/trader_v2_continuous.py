"""Continuous signal evaluation for Trader V2.

The strategy timeframes remain 30m/1h/4h and all indicator/structure inputs use
closed candles.  What changes here is *evaluation frequency*: the bot can
re-evaluate the historical setup every minute using the latest market price
instead of waiting for a new 30m candle before looking again.

Safety properties from Trader V2 are preserved:
- one order family per symbol
- signal IDs prevent duplicate execution of the same confirmed setup
- pending entries still expire at their strategy-defined deadline
- pending entries are revalidated on every signal scan and cancelled early when
  their regime/level thesis is no longer valid
- no TP/SL exists before an entry actually fills
"""

from __future__ import annotations

import logging
import time

from bot.strategy_v2 import build_signal
from bot.trader_v2 import Trader as CandleTrader

logger = logging.getLogger(__name__)


class Trader(CandleTrader):
    """Trader V2 with continuous historical re-evaluation.

    Exchange state is still managed every normal bot tick. New-entry analysis
    runs every ``v2_signal_scan_interval_seconds`` (60s by default), while the
    underlying strategy continues to use only closed 30m/1h/4h candles.
    """

    def __init__(self, config, exchange, notifier=None):
        super().__init__(config, exchange, notifier)
        self._last_continuous_scan_at = 0.0

    def _signal_scan_interval(self) -> float:
        return max(
            5.0,
            float(getattr(self.config, "v2_signal_scan_interval_seconds", 60.0)),
        )

    def run_once(self) -> dict:
        # Let the original V2 engine manage positions, fills, expiry and the
        # normal scan that occurs exactly when a new 30m candle becomes known.
        previous_bucket = self._last_signal_bucket
        summary = super().run_once()
        now = time.time()

        # If super() just handled a new 30m boundary, count that as the latest
        # scan rather than immediately performing the same work twice.
        if self._last_signal_bucket != previous_bucket:
            self._last_continuous_scan_at = now
            return summary

        if now - self._last_continuous_scan_at < self._signal_scan_interval():
            return summary

        self._last_continuous_scan_at = now

        # Existing passive entries must remain justified by the current HTF
        # regime and current price. This is the important counterpart to more
        # frequent scanning: we do not leave a stale limit order sitting simply
        # because its original 30m candle has not expired yet.
        self._revalidate_pending_signals(now)

        # Despite the legacy method name, this method is simply V2's guarded
        # signal scan. Calling it here does not use unfinished 1m/5m candles.
        # It re-runs the historical 30m/1h/4h model against the latest price.
        self._on_new_30m_candle(now)
        return summary

    def _revalidate_pending_signals(self, now: float):
        for symbol in list(self.pending_orders.keys()):
            order = self.pending_orders.get(symbol)
            if not order:
                continue
            if order.expires_at and now >= order.expires_at:
                # Normal pending-order management handles expiry and verifies
                # cancellation. Do not duplicate that state transition here.
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
                        f"[{symbol}] Pending {order.side.upper()} invalidated by fresh HTF scan; "
                        f"cancelling signal={order.signal_id}"
                    )
                    self._cancel_pending_order(symbol, reason="setup_invalidated")
                    continue

                if signal.signal_id != order.signal_id or signal.side != order.side:
                    logger.info(
                        f"[{symbol}] Pending thesis changed "
                        f"{order.signal_id} -> {signal.signal_id}; cancelling old entry"
                    )
                    self._cancel_pending_order(symbol, reason="setup_changed")
                    continue

            except Exception as exc:
                # A temporary market-data error is not proof that the thesis is
                # invalid. Keep the existing order subject to its normal expiry,
                # and retry on the next continuous scan.
                logger.warning(f"[{symbol}] Pending setup revalidation failed: {exc}")

        self._switch(self.primary_symbol)
