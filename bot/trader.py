import logging
import time
from dataclasses import dataclass

from bot.config import Config
from bot.levels import (
    detect_levels, detect_levels_multi_tf, compute_tp_sl, detect_doji,
    compute_trend_bias, check_breakout_fakeout,
    Level, DojiSignal, TrendBias, BreakoutCheck,
)
from bot.telegram_notifier import TelegramNotifier
from bot.trade_journal import TradeJournal, TradeRecord
from bot.strategy_learner import StrategyLearner
from bot.scanner import MarketScanner, SymbolOpportunity

logger = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    """A single limit order waiting to be filled."""
    oid: int | str | None
    price: float
    quantity: float
    side: str           # "long" or "short"
    kind: str           # "support" or "resistance"
    level_price: float
    strength: float
    effective_strength: float
    timeframes: list[str]
    stop_loss: float
    take_profit: float
    placed_at: float


@dataclass
class OpenPosition:
    entry_price: float
    quantity: float
    side: str           # "long" or "short"
    kind: str           # "support" or "resistance"
    level_price: float
    stop_loss: float
    take_profit: float
    initial_sl: float = 0.0
    initial_tp: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    strength: float = 0.0
    timeframes: list[str] | None = None
    filled_at: float = 0.0
    doji_entry: bool = False


class Trader:
    """Limit-order trader: one order at the highest-confidence S/R level."""

    def __init__(self, config: Config, exchange, notifier: TelegramNotifier | None = None):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.position: OpenPosition | None = None
        self.pending_order: PendingOrder | None = None
        self.known_levels: dict[float, Level] = {}
        self.last_level_refresh: float = 0
        self.last_doji_check: float = 0
        self.last_doji_signal: DojiSignal | None = None
        self.last_exit_time: float = 0
        self.last_trend_bias: TrendBias | None = None
        self._synced: bool = False
        self.scanner: MarketScanner | None = None
        self.last_scan_time: float = 0
        self.scan_interval: int = 300

        if config.hl_multi_symbol and hasattr(exchange, "info"):
            self.scanner = MarketScanner(exchange.info, config)

        self.journal = TradeJournal()
        self.learner = StrategyLearner(self.journal)
        self._apply_learned_params()

    @property
    def open_positions(self) -> list[OpenPosition]:
        return [self.position] if self.position else []

    def run_once(self) -> dict:
        """Single tick: check order/position state, refresh levels, act."""
        now = time.time()
        current_price = self.exchange.get_ticker_price()

        if not self._synced:
            self._sync_from_exchange(current_price)
            self._synced = True

        if now - self.last_level_refresh >= self.config.level_refresh_seconds:
            self._refresh_levels(current_price)
            self.last_level_refresh = now

        if now - self.last_doji_check >= self.config.doji_check_seconds:
            self._check_doji()
            self.last_doji_check = now

        if self.position:
            self._manage_position(current_price)
        elif self.pending_order:
            self._check_pending_order(current_price)
        else:
            if now - self.last_exit_time >= self.config.cooldown_seconds:
                self._place_best_order(current_price)

        return {
            "current_price": current_price,
            "levels_detected": len(self.known_levels),
            "pending_orders": 1 if self.pending_order else 0,
            "open_positions": 1 if self.position else 0,
            "position_side": self.position.side if self.position else None,
        }

    def _sync_from_exchange(self, current_price: float):
        """One-time startup sync: recover state from exchange."""
        if self.config.paper_trade:
            return

        position = self.exchange.get_position()
        if position and position["size"] > 0:
            entry = position["entry_price"]
            side = position["side"]
            kind = "support" if side == "long" else "resistance"
            tpsl = compute_tp_sl(
                entry, self.config.hl_leverage,
                self.config.target_pnl_pct, self.config.max_loss_pct, side,
            )
            self.position = OpenPosition(
                entry_price=entry,
                quantity=position["size"],
                side=side,
                kind=kind,
                level_price=entry,
                stop_loss=tpsl["sl_price"],
                take_profit=tpsl["tp_price"],
                initial_sl=tpsl["sl_price"],
                initial_tp=tpsl["tp_price"],
                highest_price=current_price,
                lowest_price=current_price,
                strength=0,
                filled_at=time.time(),
            )
            logger.info(
                f"Synced position from exchange: {side} {position['size']} @ {entry} | "
                f"SL: {tpsl['sl_price']:.2f} TP: {tpsl['tp_price']:.2f}"
            )
            return

        open_orders = self.exchange.get_open_orders()
        if not open_orders:
            return

        if len(open_orders) > 1:
            cancelled = self.exchange.cancel_all_orders()
            logger.info(f"Startup cleanup: cancelled {cancelled} duplicate orders")
            return

        o = open_orders[0]
        price = float(o.get("limitPx", 0))
        side_raw = o.get("side", "")
        side = "long" if side_raw == "B" else "short"
        kind = "support" if side == "long" else "resistance"
        qty = float(o.get("sz", 0))
        tpsl = compute_tp_sl(
            price, self.config.hl_leverage,
            self.config.target_pnl_pct, self.config.max_loss_pct, side,
        )
        self.pending_order = PendingOrder(
            oid=o.get("oid"),
            price=price,
            quantity=qty,
            side=side,
            kind=kind,
            level_price=price,
            strength=0,
            effective_strength=0,
            timeframes=[],
            stop_loss=tpsl["sl_price"],
            take_profit=tpsl["tp_price"],
            placed_at=time.time(),
        )
        logger.info(f"Synced pending order from exchange: {side} @ {price} oid={o.get('oid')}")

    # ── Level detection ──────────────────────────────────────────────

    def _refresh_levels(self, current_price: float):
        try:
            levels = detect_levels_multi_tf(
                self.exchange,
                tolerance_pct=self.config.level_tolerance_pct,
                min_touches=self.config.min_touches,
            )
        except Exception as e:
            logger.warning(f"Multi-TF detection failed, falling back to single TF: {e}")
            try:
                df = self.exchange.fetch_ohlcv()
                levels = detect_levels(
                    df,
                    tolerance_pct=self.config.level_tolerance_pct,
                    min_touches=self.config.min_touches,
                )
            except Exception as e2:
                logger.error(f"Level detection failed: {e2}")
                return

        self.known_levels = {l.price: l for l in levels}

        support = sorted(
            [l for l in levels if l.kind == "support"],
            key=lambda l: abs(current_price - l.price),
        )
        resistance = sorted(
            [l for l in levels if l.kind == "resistance"],
            key=lambda l: abs(current_price - l.price),
        )

        fib_count = sum(1 for l in levels if l.fib_ratio is not None)
        logger.info(
            f"Levels refreshed: {len(support)} support, {len(resistance)} resistance, "
            f"{fib_count} fibonacci | Price: {current_price:.2f}"
        )
        for l in levels[:10]:
            tfs = ",".join(l.timeframes) if l.timeframes else "-"
            dist = abs(current_price - l.price) / current_price * 100
            fib_tag = f" Fib{l.fib_ratio}" if l.fib_ratio else ""
            logger.info(f"  {l.kind.upper():>10} {l.price:.2f} | str={l.strength} t={l.touches} [{tfs}] {dist:.1f}%{fib_tag}")

        if self.notifier:
            def _lv_info(l):
                return {"price": l.price, "fib": l.fib_ratio}
            self.notifier.notify_levels(
                current_price,
                [_lv_info(l) for l in support[:5]],
                [_lv_info(l) for l in resistance[:5]],
            )

        if self.pending_order and not self.position:
            self._reevaluate_pending_order(current_price)

    def _check_doji(self):
        try:
            df = self.exchange.fetch_ohlcv(timeframe="5m", lookback=20)
            signal = detect_doji(df)
            if signal and signal.strength >= 40:
                self.last_doji_signal = signal
                logger.info(
                    f"DOJI detected: {signal.signal} | strength={signal.strength:.0f} "
                    f"@ {signal.price:.2f} range={signal.candle_range:.2f}"
                )
                if self.notifier:
                    self.notifier.send(
                        f"<b>Doji Signal: {signal.signal.upper()}</b>\n"
                        f"Price: <code>{signal.price:.2f}</code>\n"
                        f"Strength: <code>{signal.strength:.0f}</code>\n"
                        f"Range: <code>{signal.candle_range:.2f}</code>"
                    )
            else:
                self.last_doji_signal = None
        except Exception as e:
            logger.warning(f"Doji check failed: {e}")

    # ── Best-level selection ─────────────────────────────────────────

    def _find_best_level(self, current_price: float, trend: TrendBias | None = None) -> tuple[Level | None, float]:
        """Return the single highest-confidence level and its effective strength."""
        if not self.known_levels:
            return None, 0

        lp = self.learner.params
        best_level = None
        best_strength = 0.0

        for level in self.known_levels.values():
            if level.strength < lp.min_strength:
                continue
            tfs = level.timeframes or []
            if len(tfs) < lp.min_timeframes:
                continue

            if trend and trend.confidence >= 55:
                if trend.direction == "bullish" and level.kind == "resistance":
                    continue
                if trend.direction == "bearish" and level.kind == "support":
                    continue

            distance_pct = abs(current_price - level.price) / level.price * 100
            if distance_pct > 10:
                continue
            if distance_pct < 0.3:
                continue

            weight = lp.support_weight if level.kind == "support" else lp.resistance_weight
            if weight < 0.4:
                continue

            proximity = 1.0 - (distance_pct / 15.0)
            effective_strength = level.strength * weight * proximity

            doji = self.last_doji_signal
            if doji and doji.strength >= 50:
                if level.kind == "support" and doji.signal == "bullish":
                    effective_strength *= 1.3
                elif level.kind == "resistance" and doji.signal == "bearish":
                    effective_strength *= 1.3
                elif level.kind == "support" and doji.signal == "bearish":
                    effective_strength *= 0.5
                elif level.kind == "resistance" and doji.signal == "bullish":
                    effective_strength *= 0.5

            if effective_strength > best_strength:
                best_strength = effective_strength
                best_level = level

        return best_level, best_strength

    # ── Limit order placement ────────────────────────────────────────

    def _place_best_order(self, current_price: float):
        """Ensure exactly 1 limit order at the best S/R level.

        In multi-symbol mode, scans all markets first and switches to
        the best opportunity. Runs trend bias + breakout/fakeout analysis
        and sends a decision report to Telegram before placing or skipping.
        """
        position = self.exchange.get_position()
        if position and position["size"] > 0:
            return

        if self.scanner and time.time() - self.last_scan_time >= self.scan_interval:
            self._run_multi_symbol_scan(current_price)

        try:
            trend = compute_trend_bias(self.exchange)
            self.last_trend_bias = trend
            logger.info(
                f"Trend bias [{self.config.hl_symbol}]: {trend.direction.upper()} ({trend.confidence:.0f}%)"
            )
        except Exception as e:
            logger.warning(f"Trend bias computation failed: {e}")
            trend = TrendBias(direction="neutral", confidence=0)

        best_level, best_strength = self._find_best_level(current_price, trend)
        if not best_level:
            return

        open_orders = self.exchange.get_open_orders()

        if len(open_orders) == 1:
            existing_price = float(open_orders[0].get("limitPx", 0))
            if existing_price > 0:
                diff_pct = abs(best_level.price - existing_price) / existing_price * 100
                if diff_pct < 0.5:
                    return

        try:
            bo_check = check_breakout_fakeout(self.exchange, best_level, current_price)
        except Exception as e:
            logger.warning(f"Breakout check failed: {e}")
            bo_check = BreakoutCheck(
                is_breakout=False, is_fakeout=False, confidence=0,
                volume_confirmed=False, momentum_confirmed=False,
                trend_aligned=False, retest_seen=False, details=str(e),
            )

        distance_pct = abs(current_price - best_level.price) / best_level.price * 100
        report = {
            "level": {
                "price": best_level.price,
                "kind": best_level.kind,
                "strength": best_level.strength,
                "effective_strength": round(best_strength, 1),
                "timeframes": ",".join(best_level.timeframes) if best_level.timeframes else "-",
                "fib_ratio": best_level.fib_ratio,
                "distance_pct": round(distance_pct, 2),
            },
            "trend": {
                "direction": trend.direction,
                "confidence": trend.confidence,
                "tf_details": trend.tf_details,
            },
            "breakout": {
                "is_breakout": bo_check.is_breakout,
                "is_fakeout": bo_check.is_fakeout,
                "confidence": bo_check.confidence,
                "volume_confirmed": bo_check.volume_confirmed,
                "momentum_confirmed": bo_check.momentum_confirmed,
                "trend_aligned": bo_check.trend_aligned,
                "retest_seen": bo_check.retest_seen,
                "details": bo_check.details,
            },
        }

        if bo_check.is_fakeout:
            report["decision"] = "SKIP (FAKEOUT)"
            report["reason"] = (
                f"Level {best_level.price:.2f} breakout flagged as fakeout "
                f"({bo_check.confidence:.0f}% conf). Not placing order."
            )
            logger.info(f"SKIP fakeout at {best_level.kind} {best_level.price:.2f}: {bo_check.details}")
            if self.notifier:
                self.notifier.notify_decision_report(report)
            return

        if trend.confidence >= 55:
            if trend.direction == "bullish" and best_level.kind == "resistance":
                report["decision"] = "SKIP (TREND CONFLICT)"
                report["reason"] = "Trend is bullish but best level is resistance — skipping."
                logger.info("SKIP: bullish trend vs resistance level")
                if self.notifier:
                    self.notifier.notify_decision_report(report)
                return
            if trend.direction == "bearish" and best_level.kind == "support":
                report["decision"] = "SKIP (TREND CONFLICT)"
                report["reason"] = "Trend is bearish but best level is support — skipping."
                logger.info("SKIP: bearish trend vs support level")
                if self.notifier:
                    self.notifier.notify_decision_report(report)
                return

        side = "long" if best_level.kind == "support" else "short"
        report["decision"] = f"PLACE {side.upper()}"
        report["reason"] = (
            f"Placing limit {side} at {best_level.kind} {best_level.price:.2f} | "
            f"Trend: {trend.direction} ({trend.confidence:.0f}%) | "
            f"Breakout conf: {bo_check.confidence:.0f}%"
        )

        logger.info(f"DECISION: {report['decision']} — {report['reason']}")
        if self.notifier:
            self.notifier.notify_decision_report(report)

        if open_orders:
            self.exchange.cancel_all_orders()
            self.pending_order = None
            logger.info(f"Cancelled {len(open_orders)} orders (stale/duplicate cleanup)")

        self._place_limit_order(current_price, best_level, best_strength)

    def _run_multi_symbol_scan(self, current_price: float):
        """Scan multiple markets and switch to the best opportunity."""
        self.last_scan_time = time.time()

        symbols = None
        if self.config.hl_symbols:
            symbols = [s.strip() for s in self.config.hl_symbols.split(",") if s.strip()]

        logger.info(f"Scanning {'custom list' if symbols else f'top {self.config.hl_scan_top_n}'} markets...")
        opportunities = self.scanner.scan_all(
            symbols=symbols,
            top_n=self.config.hl_scan_top_n,
        )

        if not opportunities:
            logger.info("No opportunities found across scanned markets")
            return

        best = opportunities[0]

        if best.symbol != self.config.hl_symbol:
            open_orders = self.exchange.get_open_orders()
            if open_orders:
                self.exchange.cancel_all_orders()
                self.pending_order = None
                logger.info(f"Cancelled orders on {self.config.hl_symbol} for symbol switch")

            self.exchange.switch_symbol(best.symbol)
            self.known_levels = {}
            self.last_level_refresh = 0

        scan_summary = "\n".join(
            f"  {i+1}. {o.symbol}: score={o.score:.1f} | {o.level.kind} @ {o.level.price:.2f} | "
            f"trend={o.trend.direction} ({o.trend.confidence:.0f}%)"
            for i, o in enumerate(opportunities[:5])
        )
        logger.info(f"Top opportunities:\n{scan_summary}")

        if self.notifier:
            lines = [f"<b>MARKET SCAN ({len(opportunities)} found)</b>\n"]
            for i, o in enumerate(opportunities[:5]):
                marker = " [ACTIVE]" if o.symbol == self.config.hl_symbol else ""
                lines.append(
                    f"{i+1}. <code>{o.symbol}</code> score={o.score:.1f}{marker}\n"
                    f"   {o.level.kind} @ <code>{o.level.price:.2f}</code> | "
                    f"trend: {o.trend.direction} ({o.trend.confidence:.0f}%)"
                )
            self.notifier.send("\n".join(lines))

    def _place_limit_order(self, current_price: float, level: Level, effective_strength: float):
        leverage = self.config.hl_leverage
        order_size = self.config.order_size * self.learner.params.confidence_scale

        if level.kind == "support":
            side = "long"
        else:
            side = "short"

        entry_price = level.price
        tpsl = compute_tp_sl(
            entry_price, leverage,
            self.config.target_pnl_pct, self.config.max_loss_pct,
            side=side,
        )

        try:
            if side == "long":
                order = self.exchange.place_limit_buy(entry_price, order_size)
            else:
                order = self.exchange.place_limit_sell(entry_price, order_size)

            if order.get("status") == "filled":
                self._create_position_from_fill(
                    fill_price=order["price"],
                    quantity=order["amount"],
                    side=side,
                    level=level,
                    tpsl=tpsl,
                )
                return

            self.pending_order = PendingOrder(
                oid=order.get("oid"),
                price=entry_price,
                quantity=order["amount"],
                side=side,
                kind=level.kind,
                level_price=level.price,
                strength=level.strength,
                effective_strength=effective_strength,
                timeframes=level.timeframes or [],
                stop_loss=tpsl["sl_price"],
                take_profit=tpsl["tp_price"],
                placed_at=time.time(),
            )

            fib_tag = f" Fib{level.fib_ratio}" if level.fib_ratio else ""
            logger.info(
                f"LIMIT {side.upper()} at {level.kind.upper()} {level.price:.2f}{fib_tag} | "
                f"Size: {order['amount']} | SL: {tpsl['sl_price']:.2f} | "
                f"TP: {tpsl['tp_price']:.2f} | Strength: {effective_strength:.1f}"
            )

            if self.notifier:
                level_label = level.kind
                if level.fib_ratio:
                    level_label = f"{level.kind} (Fib {level.fib_ratio})"
                self.notifier.notify_limit_order(
                    side, level.price, level_label, order["amount"],
                    tpsl["sl_price"], tpsl["tp_price"], leverage, effective_strength,
                )

        except Exception as e:
            logger.error(f"Failed to place limit {side} at {entry_price} (size ${order_size}): {e}")
            if self.notifier:
                self.notifier.notify_error(f"Limit {side} failed at {entry_price}: {e}")

    # ── Pending order management ─────────────────────────────────────

    def _check_pending_order(self, current_price: float):
        """Check if the pending order was filled, expired, or should be replaced."""
        order = self.pending_order

        if time.time() - order.placed_at > self.config.order_ttl_hours * 3600:
            logger.info(f"Order expired after {self.config.order_ttl_hours}h")
            self._cancel_pending_order()
            return

        if self.config.paper_trade:
            if order.side == "long" and current_price <= order.price:
                self._on_order_filled(order, order.price, order.quantity)
            elif order.side == "short" and current_price >= order.price:
                self._on_order_filled(order, order.price, order.quantity)
            return

        open_orders = self.exchange.get_open_orders()
        our_order_open = any(
            str(o.get("oid")) == str(order.oid)
            for o in open_orders
        ) if order.oid is not None else False

        if not our_order_open:
            position = self.exchange.get_position()
            if position and position["size"] > 0:
                self._on_order_filled(order, position["entry_price"], position["size"])
            else:
                logger.info(f"Pending order {order.oid} no longer open")
                self.pending_order = None

    def _on_order_filled(self, order: PendingOrder, fill_price: float, fill_qty: float):
        """Handle a limit order fill by creating a position."""
        level = Level(
            price=order.level_price,
            kind=order.kind,
            touches=0,
            strength=order.strength,
            volume_avg=0,
            last_touch_idx=0,
            timeframes=order.timeframes,
        )
        tpsl = {"sl_price": order.stop_loss, "tp_price": order.take_profit}
        self._create_position_from_fill(fill_price, fill_qty, order.side, level, tpsl)
        self.pending_order = None

    def _create_position_from_fill(self, fill_price: float, quantity: float,
                                   side: str, level: Level, tpsl: dict):
        leverage = self.config.hl_leverage
        self.position = OpenPosition(
            entry_price=fill_price,
            quantity=quantity,
            side=side,
            kind=level.kind,
            level_price=level.price,
            stop_loss=tpsl["sl_price"],
            take_profit=tpsl["tp_price"],
            initial_sl=tpsl["sl_price"],
            initial_tp=tpsl["tp_price"],
            highest_price=fill_price,
            lowest_price=fill_price,
            strength=level.strength,
            timeframes=level.timeframes or [],
            filled_at=time.time(),
            doji_entry=self.last_doji_signal is not None,
        )

        logger.info(
            f"ORDER FILLED — {side.upper()} @ {fill_price:.2f} | "
            f"Qty: {quantity} | SL: {tpsl['sl_price']:.2f} | TP: {tpsl['tp_price']:.2f} | "
            f"{leverage}x lev"
        )

        try:
            self.exchange.place_tp_sl_orders(
                quantity, side, tpsl["tp_price"], tpsl["sl_price"],
            )
            logger.info("TP/SL trigger orders placed on exchange")
        except Exception as e:
            logger.error(f"Failed to place TP/SL triggers: {e}")
            if self.notifier:
                self.notifier.notify_error(f"TP/SL trigger placement failed: {e}")

        if self.notifier:
            self.notifier.notify_entry(
                side, level.price, level.kind, fill_price, quantity,
                tpsl["sl_price"], tpsl["tp_price"], leverage,
            )

    def _reevaluate_pending_order(self, current_price: float):
        """Every 5min level refresh: re-validate thesis, replace if better level found."""
        order = self.pending_order
        if not order:
            return

        best_level, best_strength = self._find_best_level(current_price, self.last_trend_bias)
        if not best_level:
            return

        if abs(best_level.price - order.level_price) / order.level_price < 0.002:
            logger.info(f"Thesis validated: {order.kind} @ {order.level_price:.2f} still best")
            return

        if best_strength > order.effective_strength * 1.15:
            logger.info(
                f"Thesis changed — replacing: {order.kind} @ {order.level_price:.2f} "
                f"(str {order.effective_strength:.1f}) -> "
                f"{best_level.kind} @ {best_level.price:.2f} (str {best_strength:.1f})"
            )
            self._cancel_pending_order()
            self._place_limit_order(current_price, best_level, best_strength)

    def _cancel_pending_order(self):
        """Cancel the current pending order (and any strays)."""
        order = self.pending_order
        if not order:
            return

        if not self.config.paper_trade:
            try:
                self.exchange.cancel_all_orders()
            except Exception as e:
                logger.warning(f"Cancel failed (may be filled): {e}")
                position = self.exchange.get_position()
                if position and position["size"] > 0:
                    self._on_order_filled(order, position["entry_price"], position["size"])
                    return

        if self.notifier:
            self.notifier.notify_order_cancelled(
                order.side, order.level_price, order.kind,
            )
        self.pending_order = None

    # ── Position management ──────────────────────────────────────────

    def _manage_position(self, current_price: float):
        pos = self.position

        if pos.side == "long":
            if current_price > pos.highest_price:
                pos.highest_price = current_price
            self._trail_long(pos, current_price)
            if current_price <= pos.stop_loss:
                self._close_position(current_price, "stop_loss")
            elif current_price >= pos.take_profit:
                self._close_position(current_price, "take_profit")
        else:
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price
            self._trail_short(pos, current_price)
            if current_price >= pos.stop_loss:
                self._close_position(current_price, "stop_loss")
            elif current_price <= pos.take_profit:
                self._close_position(current_price, "take_profit")

    def _trail_long(self, pos: OpenPosition, current_price: float):
        if not self.known_levels:
            return

        leverage = self.config.hl_leverage
        levels_sorted = sorted(self.known_levels.values(), key=lambda l: l.price)

        supports_below = [l for l in levels_sorted if l.kind == "support" and l.price < current_price]
        resistances_above = [l for l in levels_sorted if l.kind == "resistance" and l.price > current_price]

        if supports_below:
            nearest_support = supports_below[-1].price
            new_sl = round(nearest_support * 0.998, 2)
            if new_sl > pos.stop_loss:
                old_sl = pos.stop_loss
                pos.stop_loss = new_sl
                logger.info(f"  Long SL trailed: {old_sl:.2f} -> {pos.stop_loss:.2f} (support @ {nearest_support:.2f})")

        profit_pct = (current_price - pos.entry_price) / pos.entry_price * 100 * leverage
        if profit_pct >= self.config.target_pnl_pct and pos.stop_loss < pos.entry_price:
            pos.stop_loss = round(pos.entry_price * 1.001, 2)
            logger.info(f"  Long SL moved to breakeven: {pos.stop_loss:.2f}")

        if resistances_above and current_price >= pos.initial_tp * 0.995:
            next_resistance = resistances_above[0].price
            if next_resistance > pos.take_profit:
                old_tp = pos.take_profit
                pos.take_profit = round(next_resistance * 0.998, 2)
                logger.info(f"  Long TP extended: {old_tp:.2f} -> {pos.take_profit:.2f}")

    def _trail_short(self, pos: OpenPosition, current_price: float):
        if not self.known_levels:
            return

        leverage = self.config.hl_leverage
        levels_sorted = sorted(self.known_levels.values(), key=lambda l: l.price)

        resistances_above = [l for l in levels_sorted if l.kind == "resistance" and l.price > current_price]
        supports_below = [l for l in levels_sorted if l.kind == "support" and l.price < current_price]

        if resistances_above:
            nearest_resistance = resistances_above[0].price
            new_sl = round(nearest_resistance * 1.002, 2)
            if new_sl < pos.stop_loss:
                old_sl = pos.stop_loss
                pos.stop_loss = new_sl
                logger.info(f"  Short SL trailed: {old_sl:.2f} -> {pos.stop_loss:.2f} (resistance @ {nearest_resistance:.2f})")

        profit_pct = (pos.entry_price - current_price) / pos.entry_price * 100 * leverage
        if profit_pct >= self.config.target_pnl_pct and pos.stop_loss > pos.entry_price:
            pos.stop_loss = round(pos.entry_price * 0.999, 2)
            logger.info(f"  Short SL moved to breakeven: {pos.stop_loss:.2f}")

        if supports_below and current_price <= pos.initial_tp * 1.005:
            next_support = supports_below[-1].price
            if next_support < pos.take_profit:
                old_tp = pos.take_profit
                pos.take_profit = round(next_support * 1.002, 2)
                logger.info(f"  Short TP extended: {old_tp:.2f} -> {pos.take_profit:.2f}")

    def _close_position(self, exit_price: float, reason: str):
        pos = self.position
        leverage = self.config.hl_leverage

        if pos.side == "long":
            price_pnl = (exit_price - pos.entry_price) / pos.entry_price * 100
        else:
            price_pnl = (pos.entry_price - exit_price) / pos.entry_price * 100

        margin_pnl = price_pnl * leverage

        cancelled = self.exchange.cancel_all_orders()
        if cancelled:
            logger.info(f"Cancelled {cancelled} orders (incl. TP/SL triggers) before closing")

        try:
            self.exchange.place_market_close(pos.quantity, side=pos.side)
        except Exception as e:
            logger.error(f"Failed to close {pos.side}: {e}")
            if self.notifier:
                self.notifier.notify_error(f"Close failed: {e}")
            return

        logger.info(
            f"{reason.upper()} — {pos.side.upper()} @ {pos.entry_price:.2f} | "
            f"Exit: {exit_price:.2f} | PnL: {price_pnl:+.2f}% price, {margin_pnl:+.2f}% margin"
        )

        if self.notifier:
            self.notifier.notify_exit(
                reason, pos.level_price, pos.kind, exit_price,
                pos.entry_price, leverage, pos.side,
            )

        self._record_trade(pos, exit_price, reason)
        self.position = None
        self.last_exit_time = time.time()

        self.learner.learn()
        self._apply_learned_params()

    # ── Trade journal ────────────────────────────────────────────────

    def _record_trade(self, pos: OpenPosition, exit_price: float, reason: str):
        leverage = self.config.hl_leverage

        if pos.side == "long":
            price_pnl = (exit_price - pos.entry_price) / pos.entry_price * 100
            pnl_usd = pos.quantity * (exit_price - pos.entry_price) * leverage
        else:
            price_pnl = (pos.entry_price - exit_price) / pos.entry_price * 100
            pnl_usd = pos.quantity * (pos.entry_price - exit_price) * leverage

        margin_pnl = price_pnl * leverage
        hold_h = (time.time() - pos.filled_at) / 3600 if pos.filled_at else 0

        record = TradeRecord(
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.filled_at,
            exit_time=time.time(),
            side=pos.side,
            kind=pos.kind,
            exit_reason=reason,
            level_strength=pos.strength,
            timeframes=pos.timeframes or [],
            leverage=leverage,
            price_pnl_pct=round(price_pnl, 4),
            margin_pnl_pct=round(margin_pnl, 4),
            quantity=pos.quantity,
            pnl_usd=round(pnl_usd, 4),
            sl_trailed=pos.stop_loss != pos.initial_sl,
            tp_extended=pos.take_profit != pos.initial_tp,
            hold_duration_h=round(hold_h, 2),
        )
        self.journal.record(record)

    def _apply_learned_params(self):
        lp = self.learner.params
        if self.journal.stats()["total"] >= 5:
            self.config.target_pnl_pct = lp.target_pnl_pct
            self.config.max_loss_pct = lp.max_loss_pct
            logger.info(
                f"Learned params applied: min_str={lp.min_strength} min_tf={lp.min_timeframes} "
                f"sup={lp.support_weight:.1f}x res={lp.resistance_weight:.1f}x "
                f"size={lp.confidence_scale:.2f}x tp={lp.target_pnl_pct}% sl={lp.max_loss_pct}%"
            )

    # ── Main loop ────────────────────────────────────────────────────

    def run_loop(self):
        mode = "PAPER" if self.config.paper_trade else "LIVE"
        backend = self.config.exchange_backend.upper()
        symbol = self.config.hl_symbol if self.config.exchange_backend == "hyperliquid" else self.config.symbol

        logger.info(f"Starting limit-order bot [{mode}] on {backend} — {symbol}")
        logger.info(
            f"Tick interval: {self.config.check_interval}s | "
            f"Level refresh: {self.config.level_refresh_seconds}s | "
            f"Max 1 limit order at highest-confidence level"
        )

        if self.notifier:
            self.notifier.notify_startup(
                symbol, self.config.timeframe, self.config.paper_trade,
                self.config.hl_leverage, self.config.hl_mainnet,
            )

        while True:
            try:
                summary = self.run_once()
                pos_info = ""
                if summary["position_side"]:
                    pos_info = f" | {summary['position_side'].upper()} open"
                elif summary["pending_orders"]:
                    po = self.pending_order
                    pos_info = f" | LIMIT {po.side.upper()} @ {po.price:.2f}"
                logger.info(
                    f"Tick: {summary['current_price']:.2f} | "
                    f"Levels: {summary['levels_detected']}{pos_info}"
                )
            except Exception as e:
                logger.error(f"Tick error: {e}")
                if self.notifier:
                    self.notifier.notify_error(str(e))
            time.sleep(self.config.check_interval)
