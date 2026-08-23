import logging
import time
from dataclasses import dataclass, field

from bot.config import Config
from bot.levels import detect_levels, detect_levels_multi_tf, get_limit_order_prices, compute_tp_sl, Level
from bot.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    """A limit order waiting to be filled."""
    level_price: float
    entry_price: float
    kind: str          # "support" or "resistance"
    strength: float
    order: dict
    stop_loss: float
    take_profit: float
    placed_at: float   # timestamp


@dataclass
class OpenPosition:
    """A filled position being managed."""
    entry_price: float
    quantity: float
    kind: str
    level_price: float
    stop_loss: float
    take_profit: float
    initial_sl: float = 0.0
    initial_tp: float = 0.0
    highest_price: float = 0.0


class Trader:
    """Smart trading logic with limit orders at scored S/R levels."""

    def __init__(self, config: Config, exchange, notifier: TelegramNotifier | None = None):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.pending_orders: list[PendingOrder] = []
        self.open_positions: list[OpenPosition] = []
        self.known_levels: dict[float, Level] = {}
        self.max_pending = int(config.max_open_orders)
        self.order_ttl = config.order_ttl_hours * 3600

    def run_once(self) -> dict:
        """Single cycle: detect levels → manage orders → check positions."""
        current_price = self.exchange.get_ticker_price()

        # 1. Detect and score levels across 1D, 4H, 1H
        try:
            levels = detect_levels_multi_tf(
                self.exchange,
                tolerance_pct=self.config.level_tolerance_pct,
                min_touches=self.config.min_touches,
            )
        except Exception as e:
            logger.warning(f"Multi-TF detection failed, falling back to single TF: {e}")
            df = self.exchange.fetch_ohlcv()
            levels = detect_levels(
                df,
                tolerance_pct=self.config.level_tolerance_pct,
                min_touches=self.config.min_touches,
            )
        self.known_levels = {l.price: l for l in levels}

        support = [l for l in levels if l.kind == "support"]
        resistance = [l for l in levels if l.kind == "resistance"]

        logger.info(
            f"Price: {current_price:.2f} | "
            f"Levels found: {len(support)} support, {len(resistance)} resistance"
        )
        for l in levels[:8]:
            tfs = ",".join(l.timeframes) if l.timeframes else "—"
            logger.info(f"  {l.kind.upper():>10} {l.price:.2f} | strength={l.strength} touches={l.touches} tf=[{tfs}]")

        if self.notifier:
            self.notifier.notify_levels(
                current_price,
                [l.price for l in support[:5]],
                [l.price for l in resistance[:5]],
            )

        # 2. Cancel stale or invalidated pending orders
        self._cleanup_pending(current_price, levels)

        # 3. Place new limit orders at best levels
        self._place_limit_orders(levels, current_price)

        # 4. Check if any pending orders have filled (paper mode simulation)
        self._check_fills(current_price)

        # 5. Manage open positions — check SL/TP
        self._manage_positions(current_price)

        return {
            "current_price": current_price,
            "levels_detected": len(levels),
            "pending_orders": len(self.pending_orders),
            "open_positions": len(self.open_positions),
        }

    def _place_limit_orders(self, levels: list[Level], current_price: float):
        """Place limit buy orders at the strongest levels."""
        existing_prices = {p.level_price for p in self.pending_orders}
        existing_prices.update(p.level_price for p in self.open_positions)

        available_slots = self.max_pending - len(self.pending_orders)
        if available_slots <= 0:
            return

        leverage = self.config.hl_leverage
        targets = get_limit_order_prices(
            levels, current_price,
            leverage=leverage,
            target_pnl_pct=self.config.target_pnl_pct,
            max_loss_pct=self.config.max_loss_pct,
            max_orders=available_slots,
        )

        for target in targets:
            if target["price"] in existing_prices:
                continue

            if target["kind"] == "support" and target["price"] >= current_price:
                continue

            entry = target["price"]
            sl = target["sl_price"]
            tp = target["tp_price"]

            try:
                order = self.exchange.place_limit_buy(entry, self.config.order_size)

                pending = PendingOrder(
                    level_price=target["price"],
                    entry_price=entry,
                    kind=target["kind"],
                    strength=target["strength"],
                    order=order,
                    stop_loss=sl,
                    take_profit=tp,
                    placed_at=time.time(),
                )
                self.pending_orders.append(pending)
                existing_prices.add(target["price"])

                logger.info(
                    f"LIMIT BUY placed at {target['kind']} {entry:.2f} | "
                    f"Strength: {target['strength']} | SL: {sl:.2f} | TP: {tp:.2f} | "
                    f"{leverage}x lev | liq: {target['liq_price']:.2f}"
                )

                if self.notifier:
                    qty = self.config.order_size / entry
                    self.notifier.notify_buy(
                        target["price"], target["kind"], entry, qty, sl, tp,
                    )

            except Exception as e:
                logger.error(f"Failed to place limit order at {entry:.2f}: {e}")
                if self.notifier:
                    self.notifier.notify_error(str(e))

    def _cleanup_pending(self, current_price: float, levels: list[Level]):
        """Cancel orders that are stale or whose levels are no longer valid."""
        now = time.time()
        active_level_prices = {l.price for l in levels}
        remaining = []

        for pending in self.pending_orders:
            age = now - pending.placed_at
            level_gone = pending.level_price not in active_level_prices

            if age > self.order_ttl:
                logger.info(f"Cancelling stale order at {pending.entry_price:.2f} (age: {age/3600:.1f}h)")
                self._cancel_order(pending)
            elif level_gone:
                logger.info(f"Cancelling order at {pending.entry_price:.2f} — level no longer valid")
                self._cancel_order(pending)
            else:
                remaining.append(pending)

        self.pending_orders = remaining

    def _cancel_order(self, pending: PendingOrder):
        """Cancel a pending order on the exchange."""
        if self.config.paper_trade:
            return
        try:
            oid = pending.order.get("oid")
            if oid:
                self.exchange.cancel_order(pending.entry_price, oid)
        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")

    def _check_fills(self, current_price: float):
        """In paper mode, simulate fills when price reaches the limit order."""
        remaining = []
        for pending in self.pending_orders:
            filled = False

            if self.config.paper_trade:
                # Support order fills when price drops to it
                if pending.kind == "support" and current_price <= pending.entry_price:
                    filled = True
                # Resistance breakout fills when price rises to it
                elif pending.kind == "resistance" and current_price >= pending.entry_price:
                    filled = True
            else:
                # Live mode: check order status from exchange
                status = pending.order.get("status", "")
                if status == "filled":
                    filled = True

            if filled:
                qty = self.config.order_size / pending.entry_price
                position = OpenPosition(
                    entry_price=pending.entry_price,
                    quantity=qty,
                    kind=pending.kind,
                    level_price=pending.level_price,
                    stop_loss=pending.stop_loss,
                    take_profit=pending.take_profit,
                    initial_sl=pending.stop_loss,
                    initial_tp=pending.take_profit,
                    highest_price=pending.entry_price,
                )
                self.open_positions.append(position)
                logger.info(
                    f"FILLED: {pending.kind} limit buy @ {pending.entry_price:.2f} | "
                    f"Qty: {qty:.6f} | SL: {pending.stop_loss:.2f} | TP: {pending.take_profit:.2f}"
                )
            else:
                remaining.append(pending)

        self.pending_orders = remaining

    def _manage_positions(self, current_price: float):
        """Check SL/TP on open positions with smart trailing based on S/R levels."""
        remaining = []
        for pos in self.open_positions:
            # Track highest price for trailing logic
            if current_price > pos.highest_price:
                pos.highest_price = current_price

            # Adapt TP/SL using live S/R levels
            self._adapt_tp_sl(pos, current_price)

            margin_pnl = (current_price - pos.entry_price) / pos.entry_price * 100 * self.config.hl_leverage

            if current_price <= pos.stop_loss:
                price_pnl = (current_price - pos.entry_price) / pos.entry_price * 100
                logger.info(
                    f"STOP LOSS — {pos.kind} @ {pos.entry_price:.2f} | "
                    f"Exit: {current_price:.2f} | PnL: {price_pnl:+.2f}% price, {margin_pnl:+.2f}% margin"
                )
                if self.notifier:
                    self.notifier.notify_exit("stop_loss", pos.level_price, pos.kind, current_price, pos.entry_price, leverage)
            elif current_price >= pos.take_profit:
                price_pnl = (current_price - pos.entry_price) / pos.entry_price * 100
                logger.info(
                    f"TAKE PROFIT — {pos.kind} @ {pos.entry_price:.2f} | "
                    f"Exit: {current_price:.2f} | PnL: {price_pnl:+.2f}% price, {margin_pnl:+.2f}% margin"
                )
                if self.notifier:
                    self.notifier.notify_exit("take_profit", pos.level_price, pos.kind, current_price, pos.entry_price, leverage)
            else:
                remaining.append(pos)

        self.open_positions = remaining

    def _adapt_tp_sl(self, pos: OpenPosition, current_price: float):
        """Adjust TP/SL based on live S/R levels and price movement.

        Rules:
        1. Trail SL up to nearest support below current price (never lower SL)
        2. If price passed original TP, move TP to next resistance above
        3. Once in profit, move SL to breakeven then trail with support
        """
        if not self.known_levels:
            return

        leverage = self.config.hl_leverage
        levels_sorted = sorted(self.known_levels.values(), key=lambda l: l.price)

        # Find nearest support below current price
        supports_below = [l for l in levels_sorted if l.kind == "support" and l.price < current_price]
        # Find nearest resistance above current price
        resistances_above = [l for l in levels_sorted if l.kind == "resistance" and l.price > current_price]

        # Rule 1: Trail SL up to nearest support (never lower it)
        if supports_below:
            nearest_support = supports_below[-1].price
            # Only move SL up if the support is above current SL and below entry
            # (or above entry if we're trailing in profit)
            if nearest_support > pos.stop_loss:
                old_sl = pos.stop_loss
                pos.stop_loss = round(nearest_support * 0.998, 2)  # just below support
                if pos.stop_loss > old_sl:
                    logger.info(
                        f"  SL trailed: {old_sl:.2f} → {pos.stop_loss:.2f} "
                        f"(support @ {nearest_support:.2f})"
                    )

        # Rule 2: Breakeven stop — once profit exceeds target, lock in entry
        profit_pct = (current_price - pos.entry_price) / pos.entry_price * 100 * leverage
        if profit_pct >= self.config.target_pnl_pct and pos.stop_loss < pos.entry_price:
            pos.stop_loss = round(pos.entry_price * 1.001, 2)  # just above entry
            logger.info(f"  SL moved to breakeven: {pos.stop_loss:.2f}")

        # Rule 3: Extend TP to next resistance if price already passed original TP level
        if resistances_above and current_price >= pos.initial_tp * 0.995:
            next_resistance = resistances_above[0].price
            if next_resistance > pos.take_profit:
                old_tp = pos.take_profit
                pos.take_profit = round(next_resistance * 0.998, 2)  # just below resistance
                logger.info(
                    f"  TP extended: {old_tp:.2f} → {pos.take_profit:.2f} "
                    f"(resistance @ {next_resistance:.2f})"
                )

    def run_loop(self):
        mode = "PAPER" if self.config.paper_trade else "LIVE"
        backend = self.config.exchange_backend.upper()
        symbol = self.config.hl_symbol if self.config.exchange_backend == "hyperliquid" else self.config.symbol

        logger.info(f"Starting bot [{mode}] on {backend} — {symbol} ({self.config.timeframe})")
        logger.info(f"Max pending orders: {self.max_pending} | Order TTL: {self.config.order_ttl_hours}h")

        if self.notifier:
            self.notifier.notify_startup(symbol, self.config.timeframe, self.config.paper_trade, self.config.hl_leverage)

        while True:
            try:
                summary = self.run_once()
                logger.info(f"Cycle complete: {summary}")
            except Exception as e:
                logger.error(f"Error in trading cycle: {e}")
                if self.notifier:
                    self.notifier.notify_error(str(e))
            time.sleep(self.config.check_interval)
