import logging
import time
from dataclasses import dataclass

from bot.config import Config
from bot.levels import (
    detect_levels, detect_levels_multi_tf, compute_tp_sl_from_levels,
    detect_doji, compute_trend_bias, check_breakout_fakeout,
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
    symbol: str
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
    leverage: int = 10


@dataclass
class OpenPosition:
    symbol: str
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
    leverage: int = 10
    # TP/SL prices last confirmed on the exchange, so trailing jitter
    # doesn't churn cancel/re-place cycles every tick
    last_synced_sl: float = 0.0
    last_synced_tp: float = 0.0


class Trader:
    """Multi-symbol limit-order trader: takes every good opportunity."""

    def __init__(self, config: Config, exchange, notifier: TelegramNotifier | None = None):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier

        self.positions: dict[str, OpenPosition] = {}
        self.pending_orders: dict[str, PendingOrder] = {}
        self.known_levels: dict[str, dict[float, Level]] = {}
        self.last_level_refresh: float = 0
        self.last_doji_check: float = 0
        self.last_doji_signal: DojiSignal | None = None
        self.last_exit_time: float = 0
        self.last_trend_bias: dict[str, TrendBias] = {}
        self._synced: bool = False
        self.scanner: MarketScanner | None = None
        self.last_scan_time: float = 0
        self.scan_interval: int = 300
        self.max_positions: int = config.hl_max_positions
        self.paused: bool = False
        self.running: bool = True

        if config.hl_multi_symbol and hasattr(exchange, "info"):
            self.scanner = MarketScanner(exchange.info, config)

        self.journal = TradeJournal()
        self.learner = StrategyLearner(self.journal)
        self._apply_learned_params()

    @property
    def position(self) -> OpenPosition | None:
        if self.positions:
            return next(iter(self.positions.values()))
        return None

    @property
    def pending_order(self) -> PendingOrder | None:
        if self.pending_orders:
            return next(iter(self.pending_orders.values()))
        return None

    @property
    def open_positions(self) -> list[OpenPosition]:
        return list(self.positions.values())

    def _get_symbols(self) -> list[str]:
        if self.config.hl_multi_symbol and self.config.hl_symbols:
            return [s.strip() for s in self.config.hl_symbols.split(",") if s.strip()]
        return [self.config.hl_symbol]

    def run_once(self) -> dict:
        """Single tick: manage all positions/orders, scan for new opportunities."""
        now = time.time()

        if not self._synced:
            self._sync_all_from_exchange()
            self._synced = True

        symbols = self._get_symbols()

        if now - self.last_level_refresh >= self.config.level_refresh_seconds:
            for sym in symbols:
                self.exchange.switch_symbol(sym)
                price = self.exchange.get_ticker_price()
                self._refresh_levels_for(sym, price)
            self.last_level_refresh = now

        if now - self.last_doji_check >= self.config.doji_check_seconds:
            self._check_doji()
            self.last_doji_check = now

        for sym in list(self.positions.keys()):
            self.exchange.switch_symbol(sym)
            try:
                price = self.exchange.get_ticker_price()
                self._manage_position(sym, price)
            except Exception as e:
                logger.error(f"Error managing {sym} position: {e}")

        for sym in list(self.pending_orders.keys()):
            self.exchange.switch_symbol(sym)
            try:
                price = self.exchange.get_ticker_price()
                self._check_pending_order(sym, price)
            except Exception as e:
                logger.error(f"Error checking {sym} pending order: {e}")

        if not self.paused and now - self.last_exit_time >= self.config.cooldown_seconds:
            active_count = len(self.positions) + len(self.pending_orders)
            if active_count < self.max_positions:
                self._scan_and_place(symbols)

        self.exchange.switch_symbol(self.config.hl_symbol)

        primary_price = 0
        try:
            primary_price = self.exchange.get_ticker_price()
        except Exception:
            pass

        return {
            "current_price": primary_price,
            "levels_detected": sum(len(v) for v in self.known_levels.values()),
            "pending_orders": len(self.pending_orders),
            "open_positions": len(self.positions),
            "position_side": ", ".join(f"{s}:{p.side}" for s, p in self.positions.items()) or None,
            "symbols_active": list(set(list(self.positions.keys()) + list(self.pending_orders.keys()))) or None,
        }

    # ── Startup sync ────────────────────────────────────────────────

    def _sync_all_from_exchange(self):
        """Recover positions and orders across all symbols from exchange."""
        if self.config.paper_trade:
            return

        all_pos = self.exchange.get_all_positions()
        for pos_data in all_pos:
            sym = pos_data["coin"]
            if sym not in self._get_symbols():
                continue
            entry = pos_data["entry_price"]
            side = pos_data["side"]
            kind = "support" if side == "long" else "resistance"
            self.exchange.switch_symbol(sym)
            price = self.exchange.get_ticker_price()
            try:
                self._refresh_levels_for(sym, price)
            except Exception as e:
                logger.warning(f"[{sym}] Level refresh during sync failed: {e}")
            tpsl = self._compute_tp_sl(sym, entry, side, self.config.hl_leverage)
            self.positions[sym] = OpenPosition(
                symbol=sym,
                entry_price=entry,
                quantity=pos_data["size"],
                side=side,
                kind=kind,
                level_price=entry,
                stop_loss=tpsl["sl_price"],
                take_profit=tpsl["tp_price"],
                initial_sl=tpsl["sl_price"],
                initial_tp=tpsl["tp_price"],
                highest_price=price,
                lowest_price=price,
                strength=0,
                filled_at=time.time(),
                leverage=self.config.hl_leverage,
            )
            logger.info(
                f"Synced {sym} position: {side} {pos_data['size']} @ {entry} | "
                f"SL: {tpsl['sl_price']:.2f} TP: {tpsl['tp_price']:.2f}"
            )

            if hasattr(self.exchange, 'get_trigger_orders_for_symbol'):
                existing_triggers = self.exchange.get_trigger_orders_for_symbol(sym)
                if len(existing_triggers) > 2:
                    # duplicates accumulated by earlier runs — wipe and re-place a clean pair
                    cancelled = self.exchange.cancel_trigger_orders_for_symbol(sym)
                    logger.info(f"[{sym}] Startup cleanup: cancelled {cancelled} duplicate TP/SL triggers")
                    existing_triggers = self.exchange.get_trigger_orders_for_symbol(sym)
                if not existing_triggers:
                    try:
                        self.exchange.place_tp_sl_orders(
                            pos_data["size"], side, tpsl["tp_price"], tpsl["sl_price"],
                        )
                        pos = self.positions[sym]
                        pos.last_synced_sl = tpsl["sl_price"]
                        pos.last_synced_tp = tpsl["tp_price"]
                        logger.info(f"[{sym}] TP/SL triggers placed for synced position")
                    except Exception as e:
                        logger.error(f"[{sym}] Failed to place TP/SL for synced position: {e}")
                else:
                    pos = self.positions[sym]
                    pos.last_synced_sl = tpsl["sl_price"]
                    pos.last_synced_tp = tpsl["tp_price"]
                    logger.info(f"[{sym}] {len(existing_triggers)} trigger orders already exist")

        for sym in self._get_symbols():
            if sym in self.positions:
                continue
            if hasattr(self.exchange, 'get_trigger_orders_for_symbol'):
                orphans = self.exchange.get_trigger_orders_for_symbol(sym)
                if orphans:
                    # reduce-only TP/SL triggers with no position behind them
                    cancelled = self.exchange.cancel_trigger_orders_for_symbol(sym)
                    logger.info(f"[{sym}] Cancelled {cancelled} orphaned TP/SL triggers (no open position)")
            orders = self.exchange.get_open_orders_for_symbol(sym)
            if not orders:
                continue
            if len(orders) > 1:
                cancelled = self.exchange.cancel_orders_for_symbol(sym)
                logger.info(f"Startup cleanup: cancelled {cancelled} duplicate orders on {sym}")
                continue

            o = orders[0]
            price = float(o.get("limitPx", 0))
            side_raw = o.get("side", "")
            side = "long" if side_raw == "B" else "short"
            kind = "support" if side == "long" else "resistance"
            qty = float(o.get("sz", 0))
            tpsl = self._compute_tp_sl(sym, price, side, self.config.hl_leverage, level_price=price)
            self.pending_orders[sym] = PendingOrder(
                symbol=sym,
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
                leverage=self.config.hl_leverage,
            )
            logger.info(f"Synced {sym} pending order: {side} @ {price}")

    # ── TP/SL computation ────────────────────────────────────────────

    def _compute_tp_sl(self, symbol: str, entry: float, side: str,
                       leverage: int, level_price: float | None = None) -> dict:
        """S/R-zone-aware TP/SL for a symbol, falling back to percent targets."""
        sym_levels = list(self.known_levels.get(symbol, {}).values())
        return compute_tp_sl_from_levels(
            entry, side, sym_levels, leverage,
            self.config.target_pnl_pct, self.config.max_loss_pct,
            level_price=level_price,
            sl_zone_buffer_pct=self.config.sl_zone_buffer_pct,
            tp_zone_buffer_pct=self.config.tp_zone_buffer_pct,
            min_rr=self.config.min_risk_reward,
        )

    # ── Level detection ──────────────────────────────────────────────

    def _refresh_levels_for(self, symbol: str, current_price: float):
        try:
            levels = detect_levels_multi_tf(
                self.exchange,
                tolerance_pct=self.config.level_tolerance_pct,
                min_touches=self.config.min_touches,
            )
        except Exception as e:
            logger.warning(f"Multi-TF detection failed for {symbol}, falling back: {e}")
            try:
                # fall back to the 1h chart — the day-trader S/R timeframe —
                # rather than the noisy 5m scalping feed
                df = self.exchange.fetch_ohlcv(timeframe="1h", lookback=500)
                levels = detect_levels(
                    df,
                    tolerance_pct=self.config.level_tolerance_pct,
                    min_touches=self.config.min_touches,
                )
            except Exception as e2:
                logger.error(f"Level detection failed for {symbol}: {e2}")
                return

        self.known_levels[symbol] = {l.price: l for l in levels}

        support = [l for l in levels if l.kind == "support"]
        resistance = [l for l in levels if l.kind == "resistance"]
        fib_count = sum(1 for l in levels if l.fib_ratio is not None)
        logger.info(
            f"[{symbol}] Levels: {len(support)} support, {len(resistance)} resistance, "
            f"{fib_count} fib | Price: {current_price:.2f}"
        )

        if symbol in self.pending_orders and symbol not in self.positions:
            self._reevaluate_pending_order(symbol, current_price)

    def _check_doji(self):
        try:
            df = self.exchange.fetch_ohlcv(timeframe="5m", lookback=20)
            signal = detect_doji(df)
            if signal and signal.strength >= 40:
                self.last_doji_signal = signal
            else:
                self.last_doji_signal = None
        except Exception as e:
            logger.warning(f"Doji check failed: {e}")

    # ── Dynamic leverage ─────────────────────────────────────────────

    def _compute_leverage(self, level: Level, trend: TrendBias,
                          bo_check: BreakoutCheck, effective_strength: float) -> int:
        """Compute leverage from signal quality: stronger signals = higher leverage."""
        min_lev = self.config.hl_leverage_min
        max_lev = self.config.hl_leverage_max
        score = 0.0

        # Level strength (0-30 points)
        score += min(level.strength / 3, 30)

        # Multi-timeframe confirmation (0-20 points)
        n_tfs = len(level.timeframes) if level.timeframes else 0
        score += min(n_tfs * 5, 20)

        # Trend alignment (0-25 points)
        if trend.confidence >= 60:
            aligned = (
                (trend.direction == "bullish" and level.kind == "support") or
                (trend.direction == "bearish" and level.kind == "resistance")
            )
            score += 25 if aligned else 0
        elif trend.confidence >= 40:
            score += 10

        # Breakout confirmation (0-15 points)
        checks = sum([
            bo_check.volume_confirmed,
            bo_check.momentum_confirmed,
            bo_check.trend_aligned,
            bo_check.retest_seen,
        ])
        score += checks * 3.75

        # Effective strength bonus (0-10 points)
        score += min(effective_strength / 10, 10)

        # Map score (0-100) to leverage range
        ratio = min(max(score / 80, 0), 1.0)
        leverage = int(min_lev + ratio * (max_lev - min_lev))
        leverage = max(min_lev, min(leverage, max_lev))

        return leverage

    # ── Scanning and placing ────────────────────────────────────────

    def _scan_and_place(self, symbols: list[str]):
        """Scan all symbols and place orders on every good opportunity."""
        for sym in symbols:
            if sym in self.positions or sym in self.pending_orders:
                continue
            if len(self.positions) + len(self.pending_orders) >= self.max_positions:
                break

            self.exchange.switch_symbol(sym)
            try:
                current_price = self.exchange.get_ticker_price()
            except Exception:
                continue

            try:
                trend = compute_trend_bias(self.exchange)
                self.last_trend_bias[sym] = trend
            except Exception:
                trend = TrendBias(direction="neutral", confidence=0)

            sym_levels = self.known_levels.get(sym, {})
            if not sym_levels:
                continue

            best_level, best_strength = self._find_best_level(sym, current_price, trend)
            if not best_level:
                continue

            try:
                bo_check = check_breakout_fakeout(self.exchange, best_level, current_price)
            except Exception:
                bo_check = BreakoutCheck(
                    is_breakout=False, is_fakeout=False, confidence=0,
                    volume_confirmed=False, momentum_confirmed=False,
                    trend_aligned=False, retest_seen=False,
                )

            distance_pct = abs(current_price - best_level.price) / best_level.price * 100
            report = self._build_report(sym, best_level, best_strength, distance_pct, trend, bo_check)

            if bo_check.is_fakeout:
                report["decision"] = f"SKIP {sym} (FAKEOUT)"
                report["reason"] = (
                    f"{sym} level {best_level.price:.2f} flagged as fakeout "
                    f"({bo_check.confidence:.0f}% conf)."
                )
                logger.info(f"SKIP {sym} fakeout at {best_level.kind} {best_level.price:.2f}")
                if self.notifier:
                    self.notifier.notify_decision_report(report)
                continue

            if trend.confidence >= 55:
                if trend.direction == "bullish" and best_level.kind == "resistance":
                    report["decision"] = f"SKIP {sym} (TREND CONFLICT)"
                    report["reason"] = f"{sym}: Trend bullish but level is resistance."
                    logger.info(f"SKIP {sym}: bullish trend vs resistance")
                    if self.notifier:
                        self.notifier.notify_decision_report(report)
                    continue
                if trend.direction == "bearish" and best_level.kind == "support":
                    report["decision"] = f"SKIP {sym} (TREND CONFLICT)"
                    report["reason"] = f"{sym}: Trend bearish but level is support."
                    logger.info(f"SKIP {sym}: bearish trend vs support")
                    if self.notifier:
                        self.notifier.notify_decision_report(report)
                    continue

            side = "long" if best_level.kind == "support" else "short"
            leverage = self._compute_leverage(best_level, trend, bo_check, best_strength)
            self.config.hl_leverage = leverage

            report["leverage"] = leverage
            report["decision"] = f"PLACE {side.upper()} on {sym}"
            report["reason"] = (
                f"Limit {side} at {best_level.kind} {best_level.price:.2f} | "
                f"Trend: {trend.direction} ({trend.confidence:.0f}%) | "
                f"Breakout: {bo_check.confidence:.0f}% | Lev: {leverage}x"
            )

            logger.info(f"DECISION: {report['decision']} — {report['reason']}")
            if self.notifier:
                self.notifier.notify_decision_report(report)

            existing_orders = self.exchange.get_open_orders()
            if existing_orders:
                self.exchange.cancel_all_orders()
                logger.info(f"Cancelled {len(existing_orders)} stale orders on {sym}")

            self._place_limit_order(sym, current_price, best_level, best_strength, leverage)

    def _build_report(self, symbol, level, strength, distance_pct, trend, bo_check):
        return {
            "symbol": symbol,
            "level": {
                "price": level.price,
                "kind": level.kind,
                "strength": level.strength,
                "effective_strength": round(strength, 1),
                "timeframes": ",".join(level.timeframes) if level.timeframes else "-",
                "fib_ratio": level.fib_ratio,
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

    # ── Best-level selection ─────────────────────────────────────────

    def _find_best_level(self, symbol: str, current_price: float,
                         trend: TrendBias | None = None) -> tuple[Level | None, float]:
        sym_levels = self.known_levels.get(symbol, {})
        if not sym_levels:
            return None, 0

        lp = self.learner.params
        best_level = None
        best_strength = 0.0

        for level in sym_levels.values():
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
            if distance_pct > 10 or distance_pct < 0.3:
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

    def _compute_order_size(self, entry_price: float, tp_price: float, leverage: int) -> float:
        """Size the position so that hitting TP yields target_profit_usd.

        Falls back to config.order_size when the TP is too close (would
        require an unreasonably large position) or when target_profit_usd
        is not set.  config.order_size always acts as the upper cap.
        """
        tp_move_pct = abs(tp_price - entry_price) / entry_price
        if tp_move_pct <= 0:
            return self.config.order_size

        target = self.config.target_profit_usd
        if target <= 0:
            return self.config.order_size

        needed = target / tp_move_pct
        cap = self.config.order_size * self.learner.params.confidence_scale
        size = min(needed, cap)
        margin = size / leverage
        logger.info(
            f"Position sizing: TP move {tp_move_pct*100:.3f}% → "
            f"need ${needed:.0f} notional for ${target:.0f} profit | "
            f"using ${size:.0f} (margin ${margin:.1f} @ {leverage}x)"
        )
        return size

    def _place_limit_order(self, symbol: str, current_price: float,
                           level: Level, effective_strength: float,
                           leverage: int | None = None):
        leverage = leverage or self.config.hl_leverage
        self.config.hl_leverage = leverage
        if hasattr(self.exchange, '_set_leverage'):
            self.exchange._set_leverage()
        side = "long" if level.kind == "support" else "short"
        entry_price = level.price

        tpsl = self._compute_tp_sl(symbol, entry_price, side, leverage, level_price=level.price)
        order_size = self._compute_order_size(entry_price, tpsl["tp_price"], leverage)

        try:
            if side == "long":
                order = self.exchange.place_limit_buy(
                    entry_price, order_size,
                    tp_price=tpsl["tp_price"], sl_price=tpsl["sl_price"],
                )
            else:
                order = self.exchange.place_limit_sell(
                    entry_price, order_size,
                    tp_price=tpsl["tp_price"], sl_price=tpsl["sl_price"],
                )

            if order.get("status") == "filled":
                self._create_position_from_fill(
                    symbol, order["price"], order["amount"], side, level, tpsl,
                )
                return

            self.pending_orders[symbol] = PendingOrder(
                symbol=symbol,
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
                leverage=leverage,
            )

            fib_tag = f" Fib{level.fib_ratio}" if level.fib_ratio else ""
            logger.info(
                f"[{symbol}] LIMIT {side.upper()} at {level.kind.upper()} {level.price:.2f}{fib_tag} | "
                f"Size: {order['amount']} | SL: {tpsl['sl_price']:.2f} ({tpsl.get('sl_basis', '?')}) | "
                f"TP: {tpsl['tp_price']:.2f} ({tpsl.get('tp_basis', '?')}) | "
                f"R:R {tpsl.get('risk_reward', 0):.1f} | Str: {effective_strength:.1f}"
            )

            if self.notifier:
                level_label = level.kind
                if level.fib_ratio:
                    level_label = f"{level.kind} (Fib {level.fib_ratio})"
                self.notifier.notify_limit_order(
                    side, level.price, f"{symbol} {level_label}", order["amount"],
                    tpsl["sl_price"], tpsl["tp_price"], leverage, effective_strength,
                )

        except Exception as e:
            logger.error(f"[{symbol}] Failed to place limit {side} at {entry_price}: {e}")
            if self.notifier:
                self.notifier.notify_error(f"{symbol} limit {side} failed: {e}")

    # ── Pending order management ─────────────────────────────────────

    def _check_pending_order(self, symbol: str, current_price: float):
        order = self.pending_orders.get(symbol)
        if not order:
            return

        if time.time() - order.placed_at > self.config.order_ttl_hours * 3600:
            logger.info(f"[{symbol}] Order expired after {self.config.order_ttl_hours}h")
            self._cancel_pending_order(symbol)
            return

        if self.config.paper_trade:
            if order.side == "long" and current_price <= order.price:
                self._on_order_filled(symbol, order, order.price, order.quantity)
            elif order.side == "short" and current_price >= order.price:
                self._on_order_filled(symbol, order, order.price, order.quantity)
            return

        open_orders = self.exchange.get_open_orders_for_symbol(symbol)
        our_order_open = any(
            str(o.get("oid")) == str(order.oid)
            for o in open_orders
        ) if order.oid is not None else False

        if not our_order_open:
            position = self.exchange.get_position(symbol)
            if position and position["size"] > 0:
                self._on_order_filled(symbol, order, position["entry_price"], position["size"])
            else:
                logger.info(f"[{symbol}] Pending order {order.oid} no longer open")
                self.pending_orders.pop(symbol, None)

    def _on_order_filled(self, symbol: str, order: PendingOrder, fill_price: float, fill_qty: float):
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
        self._create_position_from_fill(symbol, fill_price, fill_qty, order.side, level, tpsl)
        self.pending_orders.pop(symbol, None)

    def _create_position_from_fill(self, symbol: str, fill_price: float, quantity: float,
                                   side: str, level: Level, tpsl: dict):
        self.positions[symbol] = OpenPosition(
            symbol=symbol,
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
            leverage=self.config.hl_leverage,
            last_synced_sl=tpsl["sl_price"],
            last_synced_tp=tpsl["tp_price"],
        )

        logger.info(
            f"[{symbol}] FILLED — {side.upper()} @ {fill_price:.2f} | "
            f"Qty: {quantity} | SL: {tpsl['sl_price']:.2f} | TP: {tpsl['tp_price']:.2f}"
        )

        if self.notifier:
            self.notifier.notify_entry(
                side, level.price, f"{symbol} {level.kind}", fill_price, quantity,
                tpsl["sl_price"], tpsl["tp_price"], self.config.hl_leverage,
            )

    def _reevaluate_pending_order(self, symbol: str, current_price: float):
        order = self.pending_orders.get(symbol)
        if not order:
            return

        trend = self.last_trend_bias.get(symbol)
        best_level, best_strength = self._find_best_level(symbol, current_price, trend)
        if not best_level:
            return

        if abs(best_level.price - order.level_price) / order.level_price < 0.002:
            return

        if best_strength > order.effective_strength * 1.15:
            logger.info(
                f"[{symbol}] Thesis changed: {order.kind} @ {order.level_price:.2f} "
                f"-> {best_level.kind} @ {best_level.price:.2f}"
            )
            self._cancel_pending_order(symbol)
            self._place_limit_order(symbol, current_price, best_level, best_strength)

    def _cancel_pending_order(self, symbol: str):
        order = self.pending_orders.get(symbol)
        if not order:
            return

        if not self.config.paper_trade:
            try:
                self.exchange.cancel_orders_for_symbol(symbol)
            except Exception as e:
                logger.warning(f"[{symbol}] Cancel failed: {e}")
                position = self.exchange.get_position(symbol)
                if position and position["size"] > 0:
                    self._on_order_filled(symbol, order, position["entry_price"], position["size"])
                    return

        if self.notifier:
            self.notifier.notify_order_cancelled(
                order.side, order.level_price, f"{symbol} {order.kind}",
            )
        self.pending_orders.pop(symbol, None)

    # ── Position management ──────────────────────────────────────────

    def _manage_position(self, symbol: str, current_price: float):
        pos = self.positions.get(symbol)
        if not pos:
            return

        sym_levels = self.known_levels.get(symbol, {})

        if pos.side == "long":
            if current_price > pos.highest_price:
                pos.highest_price = current_price
            self._trail_long(pos, current_price, sym_levels)
            if current_price <= pos.stop_loss:
                self._close_position(symbol, current_price, "stop_loss")
            elif current_price >= pos.take_profit:
                self._close_position(symbol, current_price, "take_profit")
        else:
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price
            self._trail_short(pos, current_price, sym_levels)
            if current_price >= pos.stop_loss:
                self._close_position(symbol, current_price, "stop_loss")
            elif current_price <= pos.take_profit:
                self._close_position(symbol, current_price, "take_profit")

    def _trail_long(self, pos: OpenPosition, current_price: float, levels: dict):
        if not levels:
            return

        leverage = pos.leverage
        levels_sorted = sorted(levels.values(), key=lambda l: l.price)
        old_sl = pos.stop_loss
        old_tp = pos.take_profit
        sl_buf = 1 - self.config.sl_zone_buffer_pct / 100
        tp_buf = 1 - self.config.tp_zone_buffer_pct / 100
        # level means drift a little on every refresh; ignore sub-jitter moves
        min_step = pos.entry_price * 0.0005

        supports_below = [l for l in levels_sorted if l.kind == "support" and l.price < current_price]
        resistances_above = [l for l in levels_sorted if l.kind == "resistance" and l.price > current_price]

        if supports_below:
            nearest_support = supports_below[-1].price
            new_sl = round(nearest_support * sl_buf, 2)
            if new_sl > pos.stop_loss + min_step:
                pos.stop_loss = new_sl

        profit_pct = (current_price - pos.entry_price) / pos.entry_price * 100 * leverage
        if profit_pct >= self.config.target_pnl_pct and pos.stop_loss < pos.entry_price:
            pos.stop_loss = round(pos.entry_price * 1.001, 2)

        if resistances_above and current_price >= pos.initial_tp * 0.995:
            next_resistance = resistances_above[0].price
            new_tp = round(next_resistance * tp_buf, 2)
            if new_tp > pos.take_profit + min_step:
                pos.take_profit = new_tp

        if (pos.stop_loss != old_sl or pos.take_profit != old_tp):
            self._sync_tp_sl_to_exchange(pos)

    def _trail_short(self, pos: OpenPosition, current_price: float, levels: dict):
        if not levels:
            return

        leverage = pos.leverage
        levels_sorted = sorted(levels.values(), key=lambda l: l.price)
        old_sl = pos.stop_loss
        old_tp = pos.take_profit
        sl_buf = 1 + self.config.sl_zone_buffer_pct / 100
        tp_buf = 1 + self.config.tp_zone_buffer_pct / 100
        min_step = pos.entry_price * 0.0005

        resistances_above = [l for l in levels_sorted if l.kind == "resistance" and l.price > current_price]
        supports_below = [l for l in levels_sorted if l.kind == "support" and l.price < current_price]

        if resistances_above:
            nearest_resistance = resistances_above[0].price
            new_sl = round(nearest_resistance * sl_buf, 2)
            if new_sl < pos.stop_loss - min_step:
                pos.stop_loss = new_sl

        profit_pct = (pos.entry_price - current_price) / pos.entry_price * 100 * leverage
        if profit_pct >= self.config.target_pnl_pct and pos.stop_loss > pos.entry_price:
            pos.stop_loss = round(pos.entry_price * 0.999, 2)

        if supports_below and current_price <= pos.initial_tp * 1.005:
            next_support = supports_below[-1].price
            new_tp = round(next_support * tp_buf, 2)
            if new_tp < pos.take_profit - min_step:
                pos.take_profit = new_tp

        if (pos.stop_loss != old_sl or pos.take_profit != old_tp):
            self._sync_tp_sl_to_exchange(pos)

    def _sync_tp_sl_to_exchange(self, pos: OpenPosition):
        """Update TP/SL trigger orders on the exchange after trailing.

        Skips the round-trip unless a price actually moved beyond the
        configured threshold since the last confirmed sync — cancel/re-place
        on every tick is what floods the order book with duplicates.
        """
        if self.config.paper_trade:
            return
        if not hasattr(self.exchange, 'update_tp_sl_orders'):
            return

        threshold = self.config.tp_sl_sync_min_change_pct / 100

        def _changed(new: float, last: float) -> bool:
            return last <= 0 or abs(new - last) / last >= threshold

        if not (_changed(pos.stop_loss, pos.last_synced_sl)
                or _changed(pos.take_profit, pos.last_synced_tp)):
            return

        try:
            self.exchange.switch_symbol(pos.symbol)
            result = self.exchange.update_tp_sl_orders(
                pos.quantity, pos.side, pos.take_profit, pos.stop_loss,
            )
            if isinstance(result, dict) and any(
                k in result for k in ("error", "tp_error", "sl_error")
            ):
                logger.warning(f"[{pos.symbol}] TP/SL sync incomplete, will retry: {result}")
                return
            pos.last_synced_sl = pos.stop_loss
            pos.last_synced_tp = pos.take_profit
            logger.info(
                f"[{pos.symbol}] TP/SL triggers updated: "
                f"TP={pos.take_profit:.2f} SL={pos.stop_loss:.2f}"
            )
        except Exception as e:
            logger.error(f"[{pos.symbol}] Failed to update TP/SL triggers: {e}")

    def _close_position(self, symbol: str, exit_price: float, reason: str):
        pos = self.positions.get(symbol)
        if not pos:
            return

        leverage = pos.leverage

        if pos.side == "long":
            price_pnl = (exit_price - pos.entry_price) / pos.entry_price * 100
        else:
            price_pnl = (pos.entry_price - exit_price) / pos.entry_price * 100

        margin_pnl = price_pnl * leverage

        cancelled = self.exchange.cancel_orders_for_symbol(symbol)
        if cancelled:
            logger.info(f"[{symbol}] Cancelled {cancelled} orders before closing")

        try:
            self.exchange.place_market_close(pos.quantity, side=pos.side)
        except Exception as e:
            logger.error(f"[{symbol}] Failed to close {pos.side}: {e}")
            if self.notifier:
                self.notifier.notify_error(f"{symbol} close failed: {e}")
            return

        logger.info(
            f"[{symbol}] {reason.upper()} — {pos.side.upper()} @ {pos.entry_price:.2f} | "
            f"Exit: {exit_price:.2f} | PnL: {price_pnl:+.2f}% / {margin_pnl:+.2f}% margin"
        )

        if self.notifier:
            self.notifier.notify_exit(
                reason, pos.level_price, f"{symbol} {pos.kind}", exit_price,
                pos.entry_price, leverage, pos.side,
            )

        self._record_trade(pos, exit_price, reason)
        self.positions.pop(symbol, None)
        self.last_exit_time = time.time()

        self.learner.learn()
        self._apply_learned_params()

    # ── Trade journal ────────────────────────────────────────────────

    def _record_trade(self, pos: OpenPosition, exit_price: float, reason: str):
        leverage = pos.leverage

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

    # ── Main loop ────────────────────────────────────────────────────

    def run_loop(self):
        mode = "PAPER" if self.config.paper_trade else "LIVE"
        backend = self.config.exchange_backend.upper()
        symbols = self._get_symbols()

        logger.info(f"Starting multi-symbol bot [{mode}] on {backend}")
        logger.info(f"Symbols: {', '.join(symbols)} | Max positions: {self.max_positions}")
        logger.info(
            f"Tick: {self.config.check_interval}s | "
            f"Level refresh: {self.config.level_refresh_seconds}s"
        )

        if self.notifier:
            self.notifier.notify_startup(
                ", ".join(symbols), self.config.timeframe, self.config.paper_trade,
                self.config.hl_leverage, self.config.hl_mainnet,
            )

        while self.running:
            try:
                summary = self.run_once()
                pos_info = ""
                if summary["open_positions"]:
                    pos_info = f" | Positions: {summary['position_side']}"
                elif summary["pending_orders"]:
                    pends = [f"{s}:{o.side}@{o.price:.0f}" for s, o in self.pending_orders.items()]
                    pos_info = f" | Pending: {', '.join(pends)}"
                paused_tag = " [PAUSED]" if self.paused else ""
                logger.info(
                    f"Tick: levels={summary['levels_detected']} "
                    f"pos={summary['open_positions']} pend={summary['pending_orders']}{pos_info}{paused_tag}"
                )
            except Exception as e:
                logger.error(f"Tick error: {e}")
                if self.notifier:
                    self.notifier.notify_error(str(e))
            time.sleep(self.config.check_interval)

        logger.info("Bot stopped via /stop command")
        if self.notifier:
            self.notifier.send("<b>Bot stopped.</b> Restart with systemctl.")
