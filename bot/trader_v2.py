"""Trader V2: conservative HTF strategy with strict order lifecycle.

This module deliberately leaves the previous Trader intact so the migration is
reviewable and reversible.  Hyperliquid main.py opts into this implementation.

Core safety properties:
- New signals are evaluated once per 30m candle, not every 5-second tick.
- 1m/5m inputs are not used for trade decisions.
- Only one order family per symbol is allowed.
- TP/SL triggers are NOT created until the entry has actually filled.
- An unfilled entry expires at the next 30m candle and is verifiably cancelled.
- No opposite/new signal is allowed until exchange-side cleanup is confirmed.
- Stops are structural + ATR based; position size is derived from dollar risk.
- Leverage is fixed low while validating the strategy; adaptive learning is not
  allowed to change SL/TP/leverage.
- Startup discards stale unfilled entries instead of resetting their age.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time

from bot.levels import Level
from bot.strategy_v2 import StrategySignal, Regime, build_signal, build_recovery_plan
from bot.trade_journal import TradeJournal, TradeRecord
from bot.strategy_learner import StrategyLearner

logger = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    symbol: str
    oid: int | str | None
    price: float
    quantity: float
    side: str
    kind: str
    level_price: float
    strength: float
    effective_strength: float
    timeframes: list[str]
    stop_loss: float
    take_profit: float
    placed_at: float
    leverage: int = 3
    signal_id: str = ""
    confirmation_candle: str = ""
    expires_at: float = 0.0
    cancel_requested: bool = False


@dataclass
class OpenPosition:
    symbol: str
    entry_price: float
    quantity: float
    side: str
    kind: str
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
    leverage: int = 3
    last_synced_sl: float = 0.0
    last_synced_tp: float = 0.0
    signal_id: str = ""
    trigger_oids: list[int] | None = None
    last_protection_check: float = 0.0


@dataclass
class ScanOpportunity:
    symbol: str
    score: float
    level: Level
    trend: Regime
    distance_pct: float


class HTFScanner:
    """Telegram /scan compatibility using the same V2 strategy as execution."""

    def __init__(self, trader: "Trader"):
        self.trader = trader

    def scan_all(self, symbols=None, top_n: int = 10):
        symbols = symbols or self.trader._get_symbols()
        out: list[ScanOpportunity] = []
        now = time.time()
        for sym in symbols:
            try:
                self.trader._switch(sym)
                price = self.trader.exchange.get_ticker_price()
                signal, levels, regime = build_signal(
                    self.trader.exchange, self.trader.config, sym,
                    current_price=price, now=now,
                )
                self.trader.known_levels[sym] = {lv.price: lv for lv in levels}
                self.trader.last_trend_bias[sym] = regime
                if signal:
                    lv = Level(
                        price=signal.level_price,
                        kind=signal.kind,
                        touches=0,
                        strength=signal.strength,
                        volume_avg=0,
                        last_touch_idx=0,
                        timeframes=signal.timeframes,
                    )
                    out.append(ScanOpportunity(
                        symbol=sym,
                        score=signal.strength,
                        level=lv,
                        trend=regime,
                        distance_pct=abs(price - signal.level_price) / signal.level_price * 100,
                    ))
            except Exception as exc:
                logger.warning(f"[{sym}] HTF scan failed: {exc}")
        self.trader._switch(self.trader.primary_symbol)
        out.sort(key=lambda x: x.score, reverse=True)
        return out[:top_n]


class Trader:
    """V2 higher-timeframe trader with exchange-verified state transitions."""

    def __init__(self, config, exchange, notifier=None):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.primary_symbol = config.hl_symbol

        self.positions: dict[str, OpenPosition] = {}
        self.pending_orders: dict[str, PendingOrder] = {}
        self.known_levels: dict[str, dict[float, Level]] = {}
        self.last_trend_bias: dict[str, Regime] = {}
        self.cooldown_until: dict[str, float] = {}
        self.blocked_symbols: set[str] = set()
        self.executed_signal_ids: set[str] = set()

        self.max_positions = config.hl_max_positions
        self.paused = False
        self.running = True
        self._synced = False
        self._last_signal_bucket: int | None = None
        self.last_exit_time = 0.0

        # Kept for Telegram journal/learn compatibility. V2 never calls learn()
        # and never applies adaptive parameters to live trading.
        self.journal = TradeJournal()
        self.learner = StrategyLearner(self.journal)
        self.scanner = HTFScanner(self) if config.hl_multi_symbol else None

    @property
    def position(self):
        return next(iter(self.positions.values()), None)

    @property
    def pending_order(self):
        return next(iter(self.pending_orders.values()), None)

    @property
    def open_positions(self):
        return list(self.positions.values())

    def _get_symbols(self) -> list[str]:
        if self.config.hl_multi_symbol and self.config.hl_symbols:
            return [s.strip() for s in self.config.hl_symbols.split(",") if s.strip()]
        return [self.primary_symbol]

    def _switch(self, symbol: str):
        self.exchange.switch_symbol(symbol)

    def _safe_leverage(self) -> int:
        requested = int(getattr(self.config, "v2_leverage", 3))
        low = int(getattr(self.config, "hl_leverage_min", 1))
        high = int(getattr(self.config, "hl_leverage_max", max(requested, low)))
        return max(low, min(requested, high))

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_once(self) -> dict:
        now = time.time()
        if not self._synced:
            self._startup_reconcile()
            self._synced = True

        # Position/order management still happens every normal 5-second tick.
        for sym in list(self.positions.keys()):
            try:
                self._switch(sym)
                price = self.exchange.get_ticker_price()
                self._manage_position(sym, price)
            except Exception as exc:
                logger.error(f"[{sym}] position management failed: {exc}")

        for sym in list(self.pending_orders.keys()):
            try:
                self._switch(sym)
                price = self.exchange.get_ticker_price()
                self._check_pending_order(sym, price, now)
            except Exception as exc:
                logger.error(f"[{sym}] pending-order management failed: {exc}")

        # Trade decisions happen once per 30m bucket only.
        signal_bucket = int(now // (30 * 60))
        new_signal_candle = self._last_signal_bucket != signal_bucket
        if new_signal_candle:
            self._last_signal_bucket = signal_bucket
            self._on_new_30m_candle(now)

        self._switch(self.primary_symbol)
        try:
            primary_price = self.exchange.get_ticker_price()
        except Exception:
            primary_price = 0.0

        return {
            "current_price": primary_price,
            "levels_detected": sum(len(v) for v in self.known_levels.values()),
            "pending_orders": len(self.pending_orders),
            "open_positions": len(self.positions),
            "position_side": ", ".join(f"{s}:{p.side}" for s, p in self.positions.items()) or None,
            "symbols_active": list(set(self.positions) | set(self.pending_orders)) or None,
        }

    def _on_new_30m_candle(self, now: float):
        # Every old pending entry belongs to the previous execution candle.
        # Cancel it before evaluating any replacement/opposite thesis.
        for sym in list(self.pending_orders.keys()):
            order = self.pending_orders.get(sym)
            if order and order.expires_at <= now + 2:
                logger.info(f"[{sym}] 30m signal expired; cancelling {order.signal_id}")
                self._cancel_pending_order(sym, reason="30m_expiry")

        if self.paused:
            return

        active_count = len(self.positions) + len(self.pending_orders)
        if active_count >= self.max_positions:
            return

        for sym in self._get_symbols():
            if sym in self.positions or sym in self.pending_orders:
                continue
            if sym in self.blocked_symbols:
                continue
            if now < self.cooldown_until.get(sym, 0):
                continue
            if len(self.positions) + len(self.pending_orders) >= self.max_positions:
                break

            try:
                self._switch(sym)
                current_price = self.exchange.get_ticker_price()
                signal, levels, regime = build_signal(
                    self.exchange, self.config, sym,
                    current_price=current_price, now=now,
                )
                self.known_levels[sym] = {lv.price: lv for lv in levels}
                self.last_trend_bias[sym] = regime
                if not signal:
                    logger.info(
                        f"[{sym}] No V2 setup | regime={regime.direction} "
                        f"conf={regime.confidence:.0f}%"
                    )
                    continue
                if signal.signal_id in self.executed_signal_ids:
                    logger.warning(f"[{sym}] Duplicate signal suppressed: {signal.signal_id}")
                    continue
                self._place_signal(signal, current_price)
            except Exception as exc:
                logger.error(f"[{sym}] V2 signal scan failed: {exc}")

        self._switch(self.primary_symbol)

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    def _startup_reconcile(self):
        if self.config.paper_trade:
            logger.info("V2 startup: paper mode, no exchange orders to reconcile")
            return

        configured = set(self._get_symbols())
        exchange_positions = {p["coin"]: p for p in self.exchange.get_all_positions()}

        for sym in configured:
            self._switch(sym)
            pos_data = exchange_positions.get(sym)
            normal_orders = self.exchange.get_open_orders_for_symbol(sym)
            triggers = self.exchange.get_trigger_orders_for_symbol(sym)

            if not pos_data:
                if normal_orders or triggers:
                    logger.warning(
                        f"[{sym}] Startup discarding stale orders: "
                        f"entries={len(normal_orders)} triggers={len(triggers)}"
                    )
                    if not self._cancel_and_verify_symbol(sym):
                        self._block_symbol(sym, "startup order cleanup could not be verified")
                continue

            # A real position exists. Any additional non-trigger entry is stale
            # and must be removed without deliberately touching valid triggers.
            for order in normal_orders:
                oid = order.get("oid")
                if oid is None:
                    continue
                try:
                    self.exchange.cancel_order(float(order.get("limitPx", 0)), int(oid))
                except Exception as exc:
                    logger.warning(f"[{sym}] Failed to cancel stale entry oid={oid}: {exc}")

            current_price = self.exchange.get_ticker_price()
            recovery, levels = build_recovery_plan(
                self.exchange, self.config, sym,
                entry=pos_data["entry_price"], side=pos_data["side"],
                current_price=current_price,
            )
            self.known_levels[sym] = {lv.price: lv for lv in levels}

            parsed_tp, parsed_sl = self._extract_trigger_prices(triggers, pos_data["side"], pos_data["entry_price"])
            if len(triggers) == 2 and parsed_tp and parsed_sl:
                tp, sl = parsed_tp, parsed_sl
                trigger_oids = [int(o["oid"]) for o in triggers if o.get("oid") is not None]
                logger.info(f"[{sym}] Preserving verified startup TP/SL pair")
            else:
                tp, sl = recovery.take_profit, recovery.stop_loss
                ok, trigger_oids = self._replace_protection(
                    sym, pos_data["size"], pos_data["side"], tp, sl,
                )
                if not ok:
                    self._block_symbol(sym, "could not establish startup TP/SL protection")
                    continue

            lev = self._safe_leverage()
            self.positions[sym] = OpenPosition(
                symbol=sym,
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
            logger.info(
                f"[{sym}] Recovered {pos_data['side']} {pos_data['size']} @ "
                f"{pos_data['entry_price']:.4f} | SL={sl:.4f} TP={tp:.4f}"
            )

        self._switch(self.primary_symbol)

    # ------------------------------------------------------------------
    # Entry placement and position sizing
    # ------------------------------------------------------------------

    def _compute_order_notional(self, signal: StrategySignal, leverage: int) -> float:
        stop_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
        if stop_pct <= 0:
            return 0.0

        risk_usd = float(getattr(self.config, "v2_risk_per_trade_usd", 4.0))
        desired = risk_usd / stop_pct
        hard_cap = float(getattr(self.config, "v2_max_position_notional_usd", self.config.order_size))
        hard_cap = min(hard_cap, float(self.config.order_size))

        # Optional account-value cap prevents several concurrent trades from
        # consuming excessive isolated margin even if risk math asks for more.
        try:
            state = self.exchange.get_account_state()
            account_value = float(state.get("marginSummary", {}).get("accountValue", 0) or 0)
            max_margin_fraction = float(getattr(self.config, "v2_max_margin_fraction", 0.10))
            if account_value > 0:
                margin_notional_cap = account_value * max_margin_fraction * leverage
                hard_cap = min(hard_cap, margin_notional_cap)
        except Exception:
            pass

        notional = min(desired, hard_cap)
        minimum = float(getattr(self.config, "v2_min_position_notional_usd", 10.0))
        if notional < minimum:
            return 0.0

        logger.info(
            f"[{signal.symbol}] Risk sizing: stop={stop_pct*100:.2f}% "
            f"risk=${risk_usd:.2f} desired=${desired:.2f} cap=${hard_cap:.2f} "
            f"using=${notional:.2f}"
        )
        return notional

    def _place_signal(self, signal: StrategySignal, current_price: float):
        sym = signal.symbol
        self._switch(sym)

        # Re-check passive entry condition immediately before submission.
        live_price = self.exchange.get_ticker_price()
        if signal.side == "long" and signal.entry_price >= live_price:
            logger.info(f"[{sym}] Skip stale LONG: pullback entry is already marketable")
            return
        if signal.side == "short" and signal.entry_price <= live_price:
            logger.info(f"[{sym}] Skip stale SHORT: pullback entry is already marketable")
            return

        # The symbol must be completely clean before a new family is created.
        if self.exchange.get_position(sym):
            logger.warning(f"[{sym}] Position appeared before entry; refusing new signal")
            return
        if self.exchange.get_open_orders_for_symbol(sym) or self.exchange.get_trigger_orders_for_symbol(sym):
            if not self._cancel_and_verify_symbol(sym):
                self._block_symbol(sym, "pre-entry cleanup could not be verified")
                return

        leverage = self._safe_leverage()
        self.config.hl_leverage = leverage
        if hasattr(self.exchange, "_set_leverage"):
            self.exchange._set_leverage()

        notional = self._compute_order_notional(signal, leverage)
        if notional <= 0:
            logger.info(f"[{sym}] Signal skipped: risk-sized notional below minimum")
            return

        # Intentionally DO NOT pass TP/SL here.  Triggers are only created
        # after the exchange confirms an actual position exists.
        if signal.side == "long":
            result = self.exchange.place_limit_buy(signal.entry_price, notional)
        else:
            result = self.exchange.place_limit_sell(signal.entry_price, notional)

        self.executed_signal_ids.add(signal.signal_id)
        if len(self.executed_signal_ids) > 1000:
            # Bound memory. Signal IDs include candle timestamps, so retaining
            # the latest large set is only an idempotency guard, not persistence.
            self.executed_signal_ids = set(list(self.executed_signal_ids)[-500:])

        if result.get("status") == "filled":
            pending = PendingOrder(
                symbol=sym,
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
            self._on_order_filled(sym, pending, float(result["price"]), float(result["amount"]))
            return

        pending = PendingOrder(
            symbol=sym,
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
        self.pending_orders[sym] = pending

        # There must be zero TP/SL triggers while an entry is merely pending.
        stray_triggers = self.exchange.get_trigger_orders_for_symbol(sym) if not self.config.paper_trade else []
        if stray_triggers:
            logger.error(f"[{sym}] Found {len(stray_triggers)} pre-fill trigger(s); cleaning entire family")
            self._cancel_pending_order(sym, reason="stray_pre_fill_trigger")
            self._block_symbol(sym, "pre-fill TP/SL trigger detected")
            return

        logger.info(
            f"[{sym}] V2 PENDING {signal.side.upper()} signal={signal.signal_id} "
            f"entry={signal.entry_price:.4f} SL={signal.stop_loss:.4f} "
            f"TP={signal.take_profit:.4f} RR={signal.risk_reward:.2f} "
            f"expires={time.strftime('%H:%M:%S', time.localtime(signal.valid_until))}"
        )
        if self.notifier:
            self.notifier.notify_limit_order(
                signal.side, signal.entry_price,
                f"{sym} confirmed {signal.kind}",
                pending.quantity, signal.stop_loss, signal.take_profit,
                leverage, signal.strength,
            )

    # ------------------------------------------------------------------
    # Pending entry lifecycle
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

        open_orders = self.exchange.get_open_orders_for_symbol(symbol)
        still_open = any(str(o.get("oid")) == str(order.oid) for o in open_orders)
        if still_open:
            return

        # Entry disappeared. Resolve the exchange position before changing any
        # local state, because cancellation and fill can race.
        position = self.exchange.get_position(symbol)
        if position and position["size"] > 0:
            if position["side"] != order.side:
                self._block_symbol(symbol, "entry disappeared into opposite-side position")
                return
            self._on_order_filled(
                symbol, order, position["entry_price"], position["size"],
            )
        else:
            logger.info(f"[{symbol}] Pending oid={order.oid} disappeared without fill")
            self.pending_orders.pop(symbol, None)

    def _cancel_pending_order(self, symbol: str, reason: str = "manual") -> bool:
        order = self.pending_orders.get(symbol)
        if not order:
            return True
        order.cancel_requested = True
        self._switch(symbol)

        if self.config.paper_trade:
            self.pending_orders.pop(symbol, None)
            return True

        # Cancel exact entry first. There should be no triggers before fill.
        if order.oid is not None:
            try:
                self.exchange.cancel_order(order.price, int(order.oid))
            except Exception as exc:
                logger.warning(f"[{symbol}] Exact cancel oid={order.oid} failed: {exc}")

        for _ in range(4):
            open_orders = self.exchange.get_open_orders_for_symbol(symbol)
            exact_live = any(str(o.get("oid")) == str(order.oid) for o in open_orders)
            if not exact_live:
                break
            time.sleep(0.25)

        # Resolve fill/cancel race before broad cleanup.
        position = self.exchange.get_position(symbol)
        if position and position["size"] > 0:
            if position["side"] != order.side:
                self._block_symbol(symbol, "cancel race produced opposite-side position")
                return False
            logger.warning(f"[{symbol}] Entry filled while cancellation was in flight; protecting position")
            self._on_order_filled(symbol, order, position["entry_price"], position["size"])
            return False

        remaining = self.exchange.get_open_orders_for_symbol(symbol)
        if remaining:
            # No position exists, so broad symbol cleanup is safe here.
            if not self._cancel_and_verify_symbol(symbol):
                logger.error(f"[{symbol}] Cancel NOT verified; symbol remains locked")
                return False

        if self.exchange.get_trigger_orders_for_symbol(symbol):
            if not self._cancel_and_verify_symbol(symbol):
                self._block_symbol(symbol, "orphan trigger cleanup failed")
                return False

        self.pending_orders.pop(symbol, None)
        logger.info(f"[{symbol}] Pending signal cancelled ({reason}): {order.signal_id}")
        if self.notifier:
            self.notifier.notify_order_cancelled(
                order.side, order.level_price, f"{symbol} {order.kind}",
            )
        return True

    # ------------------------------------------------------------------
    # Fill and protection lifecycle
    # ------------------------------------------------------------------

    def _on_order_filled(self, symbol: str, order: PendingOrder,
                         fill_price: float, fill_qty: float):
        self._switch(symbol)
        if not self.config.paper_trade:
            actual = self.exchange.get_position(symbol)
            if not actual or actual["size"] <= 0:
                logger.error(f"[{symbol}] Fill reported but no exchange position exists")
                return
            if actual["side"] != order.side:
                self._block_symbol(symbol, "fill side mismatch")
                return
            fill_price = actual["entry_price"]
            fill_qty = actual["size"]

            # Any leftover entry order after a position exists is dangerous.
            for stale in self.exchange.get_open_orders_for_symbol(symbol):
                oid = stale.get("oid")
                if oid is not None:
                    try:
                        self.exchange.cancel_order(float(stale.get("limitPx", 0)), int(oid))
                    except Exception as exc:
                        logger.warning(f"[{symbol}] post-fill stale-entry cancel failed: {exc}")

            ok, trigger_oids = self._replace_protection(
                symbol, fill_qty, order.side, order.take_profit, order.stop_loss,
            )
            if not ok:
                logger.critical(f"[{symbol}] Position cannot be protected; emergency market close")
                try:
                    self.exchange.place_market_close(fill_qty, side=order.side)
                finally:
                    self._cancel_and_verify_symbol(symbol)
                    self.pending_orders.pop(symbol, None)
                    self._block_symbol(symbol, "TP/SL protection failed after fill")
                return
        else:
            trigger_oids = []

        pos = OpenPosition(
            symbol=symbol,
            entry_price=fill_price,
            quantity=fill_qty,
            side=order.side,
            kind=order.kind,
            level_price=order.level_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            initial_sl=order.stop_loss,
            initial_tp=order.take_profit,
            highest_price=fill_price,
            lowest_price=fill_price,
            strength=order.strength,
            timeframes=order.timeframes,
            filled_at=time.time(),
            leverage=order.leverage,
            last_synced_sl=order.stop_loss,
            last_synced_tp=order.take_profit,
            signal_id=order.signal_id,
            trigger_oids=trigger_oids,
            last_protection_check=time.time(),
        )
        self.positions[symbol] = pos
        self.pending_orders.pop(symbol, None)

        logger.info(
            f"[{symbol}] V2 FILLED {order.side.upper()} signal={order.signal_id} "
            f"qty={fill_qty} entry={fill_price:.4f} SL={order.stop_loss:.4f} "
            f"TP={order.take_profit:.4f}"
        )
        if self.notifier:
            self.notifier.notify_entry(
                order.side, order.level_price, f"{symbol} {order.kind}",
                fill_price, fill_qty, order.stop_loss, order.take_profit,
                order.leverage,
            )

    def _replace_protection(self, symbol: str, quantity: float, side: str,
                            tp: float, sl: float) -> tuple[bool, list[int]]:
        if self.config.paper_trade:
            return True, []
        self._switch(symbol)

        # Cancel existing triggers and VERIFY before placing the new pair.
        self.exchange.cancel_trigger_orders_for_symbol(symbol)
        for _ in range(5):
            if not self.exchange.get_trigger_orders_for_symbol(symbol):
                break
            time.sleep(0.25)
        if self.exchange.get_trigger_orders_for_symbol(symbol):
            logger.error(f"[{symbol}] Existing TP/SL could not be cleared")
            return False, []

        result = self.exchange.place_tp_sl_orders(quantity, side, tp, sl)
        if isinstance(result, dict) and any(k in result for k in ("error", "tp_error", "sl_error")):
            logger.error(f"[{symbol}] TP/SL placement returned error: {result}")

        time.sleep(0.25)
        triggers = self.exchange.get_trigger_orders_for_symbol(symbol)
        if len(triggers) != 2:
            logger.error(f"[{symbol}] Expected exactly 2 protection triggers, found {len(triggers)}")
            self.exchange.cancel_trigger_orders_for_symbol(symbol)
            return False, []

        parsed_tp, parsed_sl = self._extract_trigger_prices(triggers, side, 0)
        if parsed_tp and abs(parsed_tp - tp) / max(abs(tp), 1e-12) > 0.002:
            logger.error(f"[{symbol}] TP verification mismatch: expected {tp}, got {parsed_tp}")
            return False, []
        if parsed_sl and abs(parsed_sl - sl) / max(abs(sl), 1e-12) > 0.002:
            logger.error(f"[{symbol}] SL verification mismatch: expected {sl}, got {parsed_sl}")
            return False, []

        oids = [int(o["oid"]) for o in triggers if o.get("oid") is not None]
        logger.info(f"[{symbol}] Protection verified: TP={tp:.4f} SL={sl:.4f} oids={oids}")
        return True, oids

    @staticmethod
    def _extract_trigger_prices(triggers: list[dict], side: str,
                                entry: float) -> tuple[float | None, float | None]:
        tp = None
        sl = None
        unknown: list[float] = []
        for order in triggers:
            raw_px = order.get("triggerPx", order.get("limitPx", 0))
            try:
                px = float(raw_px)
            except (TypeError, ValueError):
                continue
            typ = str(order.get("orderType", "")).lower()
            if "take profit" in typ:
                tp = px
            elif "stop" in typ:
                sl = px
            else:
                unknown.append(px)

        if (tp is None or sl is None) and len(triggers) == 2:
            prices = []
            for order in triggers:
                try:
                    prices.append(float(order.get("triggerPx", order.get("limitPx", 0))))
                except (TypeError, ValueError):
                    pass
            if len(prices) == 2:
                low, high = min(prices), max(prices)
                if side == "long":
                    sl = sl or low
                    tp = tp or high
                else:
                    tp = tp or low
                    sl = sl or high
        return tp, sl

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _manage_position(self, symbol: str, current_price: float):
        pos = self.positions.get(symbol)
        if not pos:
            return

        if pos.side == "long":
            pos.highest_price = max(pos.highest_price, current_price)
        else:
            pos.lowest_price = min(pos.lowest_price, current_price)

        if self.config.paper_trade:
            if pos.side == "long" and current_price <= pos.stop_loss:
                self._close_position(symbol, current_price, "stop_loss")
            elif pos.side == "long" and current_price >= pos.take_profit:
                self._close_position(symbol, current_price, "take_profit")
            elif pos.side == "short" and current_price >= pos.stop_loss:
                self._close_position(symbol, current_price, "stop_loss")
            elif pos.side == "short" and current_price <= pos.take_profit:
                self._close_position(symbol, current_price, "take_profit")
            return

        actual = self.exchange.get_position(symbol)
        if not actual:
            reason = self._infer_exit_reason(pos, current_price)
            self._finalize_external_exit(symbol, current_price, reason)
            return

        if actual["side"] != pos.side:
            self._block_symbol(symbol, "exchange position side no longer matches local state")
            self.paused = True
            return

        size_diff = abs(actual["size"] - pos.quantity) / max(pos.quantity, 1e-12)
        if size_diff > 0.05:
            logger.warning(
                f"[{symbol}] Position size changed {pos.quantity} -> {actual['size']}; "
                "re-protecting full exchange size"
            )
            pos.quantity = actual["size"]
            ok, oids = self._replace_protection(
                symbol, pos.quantity, pos.side, pos.take_profit, pos.stop_loss,
            )
            if not ok:
                self._block_symbol(symbol, "could not resize TP/SL to actual position")
                self.paused = True
                return
            pos.trigger_oids = oids
            pos.last_protection_check = time.time()

        # Periodic exchange-side verification. No trailing logic is used in V2;
        # repeated tightening was part of the stop-out problem being removed.
        if time.time() - pos.last_protection_check >= 60:
            triggers = self.exchange.get_trigger_orders_for_symbol(symbol)
            tp, sl = self._extract_trigger_prices(triggers, pos.side, pos.entry_price)
            prices_ok = (
                tp is not None and sl is not None
                and abs(tp - pos.take_profit) / max(abs(pos.take_profit), 1e-12) <= 0.002
                and abs(sl - pos.stop_loss) / max(abs(pos.stop_loss), 1e-12) <= 0.002
            )
            if len(triggers) != 2 or not prices_ok:
                logger.warning(f"[{symbol}] Protection drift/duplicate detected; rebuilding clean pair")
                ok, oids = self._replace_protection(
                    symbol, pos.quantity, pos.side, pos.take_profit, pos.stop_loss,
                )
                if not ok:
                    self._block_symbol(symbol, "periodic TP/SL verification failed")
                    self.paused = True
                    return
                pos.trigger_oids = oids
            pos.last_protection_check = time.time()

    @staticmethod
    def _infer_exit_reason(pos: OpenPosition, price: float) -> str:
        tolerance = 0.002
        if pos.side == "long":
            if price <= pos.stop_loss * (1 + tolerance):
                return "stop_loss"
            if price >= pos.take_profit * (1 - tolerance):
                return "take_profit"
        else:
            if price >= pos.stop_loss * (1 - tolerance):
                return "stop_loss"
            if price <= pos.take_profit * (1 + tolerance):
                return "take_profit"
        return "exchange_exit"

    def _finalize_external_exit(self, symbol: str, exit_price: float, reason: str):
        pos = self.positions.get(symbol)
        if not pos:
            return
        # Any reduce-only trigger left after a position is gone is orphaned.
        self._cancel_and_verify_symbol(symbol)
        self._record_trade(pos, exit_price, reason)
        self.positions.pop(symbol, None)
        self._set_cooldown(symbol)
        logger.info(f"[{symbol}] Position closed on exchange ({reason}) around {exit_price:.4f}")
        if self.notifier:
            self.notifier.notify_exit(
                reason, pos.level_price, f"{symbol} {pos.kind}", exit_price,
                pos.entry_price, pos.leverage, pos.side,
            )

    def _close_position(self, symbol: str, exit_price: float, reason: str):
        pos = self.positions.get(symbol)
        if not pos:
            return
        self._switch(symbol)

        if not self.config.paper_trade:
            actual = self.exchange.get_position(symbol)
            if actual and actual["size"] > 0:
                # Clear triggers first, then close the exact exchange size.
                self.exchange.cancel_trigger_orders_for_symbol(symbol)
                for _ in range(4):
                    if not self.exchange.get_trigger_orders_for_symbol(symbol):
                        break
                    time.sleep(0.25)
                self.exchange.place_market_close(actual["size"], side=actual["side"])
                time.sleep(0.25)
                if self.exchange.get_position(symbol):
                    raise RuntimeError(f"{symbol} market close could not be verified")
            self._cancel_and_verify_symbol(symbol)

        self._record_trade(pos, exit_price, reason)
        self.positions.pop(symbol, None)
        self._set_cooldown(symbol)
        logger.info(f"[{symbol}] Closed {pos.side} ({reason}) @ {exit_price:.4f}")
        if self.notifier:
            self.notifier.notify_exit(
                reason, pos.level_price, f"{symbol} {pos.kind}", exit_price,
                pos.entry_price, pos.leverage, pos.side,
            )

    def _set_cooldown(self, symbol: str):
        minutes = float(getattr(self.config, "v2_symbol_cooldown_minutes", 30.0))
        self.cooldown_until[symbol] = time.time() + minutes * 60
        self.last_exit_time = time.time()

    # ------------------------------------------------------------------
    # Exchange cleanup / circuit breakers
    # ------------------------------------------------------------------

    def _cancel_and_verify_symbol(self, symbol: str) -> bool:
        if self.config.paper_trade:
            return True
        self._switch(symbol)
        try:
            self.exchange.cancel_orders_for_symbol(symbol)
        except Exception as exc:
            logger.warning(f"[{symbol}] broad cancel call failed: {exc}")

        for _ in range(6):
            normal = self.exchange.get_open_orders_for_symbol(symbol)
            triggers = self.exchange.get_trigger_orders_for_symbol(symbol)
            if not normal and not triggers:
                return True
            time.sleep(0.25)
        logger.error(
            f"[{symbol}] exchange cleanup verification failed: "
            f"entries={len(self.exchange.get_open_orders_for_symbol(symbol))} "
            f"triggers={len(self.exchange.get_trigger_orders_for_symbol(symbol))}"
        )
        return False

    def _block_symbol(self, symbol: str, reason: str):
        self.blocked_symbols.add(symbol)
        logger.critical(f"[{symbol}] CIRCUIT BREAKER: {reason}")
        if self.notifier:
            self.notifier.notify_error(
                f"{symbol} circuit breaker: {reason}. New entries blocked until restart/review."
            )

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    def _record_trade(self, pos: OpenPosition, exit_price: float, reason: str):
        if pos.side == "long":
            price_pnl = (exit_price - pos.entry_price) / pos.entry_price * 100
            pnl_usd = pos.quantity * (exit_price - pos.entry_price)
        else:
            price_pnl = (pos.entry_price - exit_price) / pos.entry_price * 100
            pnl_usd = pos.quantity * (pos.entry_price - exit_price)

        # Leverage changes margin ROI, not the raw USD PnL of a fixed notional
        # position. The previous trader incorrectly multiplied USD PnL by lev.
        margin_pnl = price_pnl * pos.leverage
        hold_h = (time.time() - pos.filled_at) / 3600 if pos.filled_at else 0
        self.journal.record(TradeRecord(
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.filled_at,
            exit_time=time.time(),
            side=pos.side,
            kind=pos.kind,
            exit_reason=reason,
            level_strength=pos.strength,
            timeframes=pos.timeframes or [],
            leverage=pos.leverage,
            price_pnl_pct=round(price_pnl, 4),
            margin_pnl_pct=round(margin_pnl, 4),
            quantity=pos.quantity,
            pnl_usd=round(pnl_usd, 4),
            sl_trailed=False,
            tp_extended=False,
            hold_duration_h=round(hold_h, 2),
        ))

    # ------------------------------------------------------------------
    # Loop wrapper
    # ------------------------------------------------------------------

    def run_loop(self):
        mode = "PAPER" if self.config.paper_trade else "LIVE"
        leverage = self._safe_leverage()
        logger.info(
            f"Starting Trader V2 [{mode}] | 4h regime -> 1h structure -> 30m confirmation | "
            f"fixed {leverage}x isolated | symbols={','.join(self._get_symbols())}"
        )
        if self.notifier:
            self.notifier.notify_startup(
                ", ".join(self._get_symbols()), "30m/1h/4h",
                self.config.paper_trade, leverage, self.config.hl_mainnet,
            )

        while self.running:
            try:
                summary = self.run_once()
                logger.info(
                    f"V2 Tick: levels={summary['levels_detected']} "
                    f"pos={summary['open_positions']} pend={summary['pending_orders']} "
                    f"blocked={len(self.blocked_symbols)}"
                )
            except Exception as exc:
                logger.exception(f"V2 tick error: {exc}")
                if self.notifier:
                    self.notifier.notify_error(str(exc))
            time.sleep(self.config.check_interval)

        logger.info("Trader V2 stopped")
