import logging
import time
from dataclasses import dataclass, field

from bot.config import Config
from bot.levels import (
    detect_levels, detect_levels_multi_tf, compute_tp_sl, detect_doji,
    Level, DojiSignal,
)
from bot.telegram_notifier import TelegramNotifier
from bot.trade_journal import TradeJournal, TradeRecord
from bot.strategy_learner import StrategyLearner

logger = logging.getLogger(__name__)


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
    """Scalp trader: fast price polling, enter on S/R touch, long+short."""

    def __init__(self, config: Config, exchange, notifier: TelegramNotifier | None = None):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.position: OpenPosition | None = None
        self.known_levels: dict[float, Level] = {}
        self.last_level_refresh: float = 0
        self.last_doji_check: float = 0
        self.last_doji_signal: DojiSignal | None = None
        self.last_exit_time: float = 0
        self.pending_orders: list = []

        self.journal = TradeJournal()
        self.learner = StrategyLearner(self.journal)
        self._apply_learned_params()

    @property
    def open_positions(self) -> list[OpenPosition]:
        return [self.position] if self.position else []

    def run_once(self) -> dict:
        """Single tick: refresh levels if needed, manage position or look for entry."""
        now = time.time()
        current_price = self.exchange.get_ticker_price()

        if now - self.last_level_refresh >= self.config.level_refresh_seconds:
            self._refresh_levels(current_price)
            self.last_level_refresh = now

        if now - self.last_doji_check >= self.config.doji_check_seconds:
            self._check_doji()
            self.last_doji_check = now

        if self.position:
            self._manage_position(current_price)
        else:
            if now - self.last_exit_time >= self.config.cooldown_seconds:
                self._scan_for_entry(current_price)

        return {
            "current_price": current_price,
            "levels_detected": len(self.known_levels),
            "pending_orders": 0,
            "open_positions": 1 if self.position else 0,
            "position_side": self.position.side if self.position else None,
        }

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

        support = [l for l in levels if l.kind == "support"]
        resistance = [l for l in levels if l.kind == "resistance"]

        logger.info(
            f"Levels refreshed: {len(support)} support, {len(resistance)} resistance | "
            f"Price: {current_price:.2f}"
        )
        for l in levels[:8]:
            tfs = ",".join(l.timeframes) if l.timeframes else "-"
            logger.info(f"  {l.kind.upper():>10} {l.price:.2f} | str={l.strength} t={l.touches} [{tfs}]")

        if self.notifier:
            self.notifier.notify_levels(
                current_price,
                [l.price for l in support[:5]],
                [l.price for l in resistance[:5]],
            )

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

    def _scan_for_entry(self, current_price: float):
        if not self.known_levels:
            return

        lp = self.learner.params
        leverage = self.config.hl_leverage

        best_entry = None
        best_strength = 0

        for level in self.known_levels.values():
            if level.strength < lp.min_strength:
                continue
            tfs = level.timeframes or []
            if len(tfs) < lp.min_timeframes:
                continue

            distance_pct = abs(current_price - level.price) / level.price * 100

            if distance_pct > self.config.touch_pct:
                if distance_pct <= self.config.approach_pct:
                    logger.debug(
                        f"Approaching {level.kind} @ {level.price:.2f} "
                        f"(dist={distance_pct:.3f}%)"
                    )
                continue

            weight = lp.support_weight if level.kind == "support" else lp.resistance_weight
            if weight < 0.4:
                continue

            effective_strength = level.strength * weight

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
                best_entry = level

        if best_entry:
            if best_entry.kind == "support":
                self._enter_long(current_price, best_entry)
            else:
                self._enter_short(current_price, best_entry)

    def _enter_long(self, price: float, level: Level):
        leverage = self.config.hl_leverage
        order_size = self.config.order_size * self.learner.params.confidence_scale
        tpsl = compute_tp_sl(price, leverage, self.config.target_pnl_pct, self.config.max_loss_pct, side="long")

        try:
            order = self.exchange.place_market_buy(order_size)
            qty = order["amount"]
            fill_price = order["price"]

            self.position = OpenPosition(
                entry_price=fill_price,
                quantity=qty,
                side="long",
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
                f"LONG ENTRY at support {level.price:.2f} | "
                f"Fill: {fill_price:.2f} | Qty: {qty} | "
                f"SL: {tpsl['sl_price']:.2f} | TP: {tpsl['tp_price']:.2f} | "
                f"{leverage}x lev"
            )

            if self.notifier:
                self.notifier.notify_entry(
                    "long", level.price, level.kind, fill_price, qty,
                    tpsl["sl_price"], tpsl["tp_price"], leverage,
                )

        except Exception as e:
            logger.error(f"Failed to enter long: {e}")
            if self.notifier:
                self.notifier.notify_error(f"Long entry failed: {e}")

    def _enter_short(self, price: float, level: Level):
        leverage = self.config.hl_leverage
        order_size = self.config.order_size * self.learner.params.confidence_scale
        tpsl = compute_tp_sl(price, leverage, self.config.target_pnl_pct, self.config.max_loss_pct, side="short")

        try:
            order = self.exchange.place_market_short(order_size)
            qty = order["amount"]
            fill_price = order["price"]

            self.position = OpenPosition(
                entry_price=fill_price,
                quantity=qty,
                side="short",
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
                f"SHORT ENTRY at resistance {level.price:.2f} | "
                f"Fill: {fill_price:.2f} | Qty: {qty} | "
                f"SL: {tpsl['sl_price']:.2f} | TP: {tpsl['tp_price']:.2f} | "
                f"{leverage}x lev"
            )

            if self.notifier:
                self.notifier.notify_entry(
                    "short", level.price, level.kind, fill_price, qty,
                    tpsl["sl_price"], tpsl["tp_price"], leverage,
                )

        except Exception as e:
            logger.error(f"Failed to enter short: {e}")
            if self.notifier:
                self.notifier.notify_error(f"Short entry failed: {e}")

    def _manage_position(self, current_price: float):
        pos = self.position
        leverage = self.config.hl_leverage

        if pos.side == "long":
            if current_price > pos.highest_price:
                pos.highest_price = current_price

            self._trail_long(pos, current_price)

            if current_price <= pos.stop_loss:
                self._close_position(current_price, "stop_loss")
            elif current_price >= pos.take_profit:
                self._close_position(current_price, "take_profit")

        else:  # short
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

    def run_loop(self):
        mode = "PAPER" if self.config.paper_trade else "LIVE"
        backend = self.config.exchange_backend.upper()
        symbol = self.config.hl_symbol if self.config.exchange_backend == "hyperliquid" else self.config.symbol

        logger.info(f"Starting scalp bot [{mode}] on {backend} — {symbol}")
        logger.info(
            f"Tick interval: {self.config.check_interval}s | "
            f"Level refresh: {self.config.level_refresh_seconds}s | "
            f"Touch: {self.config.touch_pct}% | Approach: {self.config.approach_pct}%"
        )

        if self.notifier:
            self.notifier.notify_startup(
                symbol, self.config.timeframe, self.config.paper_trade,
                self.config.hl_leverage,
            )

        while True:
            try:
                summary = self.run_once()
                pos_info = ""
                if summary["position_side"]:
                    pos_info = f" | {summary['position_side'].upper()} open"
                logger.info(
                    f"Tick: {summary['current_price']:.2f} | "
                    f"Levels: {summary['levels_detected']}{pos_info}"
                )
            except Exception as e:
                logger.error(f"Tick error: {e}")
                if self.notifier:
                    self.notifier.notify_error(str(e))
            time.sleep(self.config.check_interval)
