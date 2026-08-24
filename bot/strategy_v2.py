"""Higher-timeframe trading strategy used by Trader V2.

The strategy is intentionally conservative:
- 4h + 1h must agree on market regime.
- S/R is built only from 30m, 1h and 4h candles.
- A 30m candle must touch and reject the level before an order is armed.
- Entry is a pullback limit after confirmation, never a blind resting order.
- Stop loss is structural + ATR based, not leverage-derived.
- Take profit targets a modest fixed R multiple before opposing structure.

No 1m/5m signal inputs, breakout module, doji modifier or adaptive learner are
used here.  The goal is fewer, cleaner trades with deterministic behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

import numpy as np
import pandas as pd

from bot.levels import Level, detect_levels


TF_MS = {
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}

LEVEL_TFS = (
    ("4h", 180, 1.35),
    ("1h", 260, 1.00),
    ("30m", 300, 0.80),
)


@dataclass
class Regime:
    direction: str  # bullish / bearish / neutral
    confidence: float
    details: dict


@dataclass
class StrategySignal:
    signal_id: str
    symbol: str
    side: str
    kind: str
    level_price: float
    entry_price: float
    stop_loss: float
    take_profit: float
    strength: float
    timeframes: list[str]
    atr: float
    risk_reward: float
    regime: Regime
    confirmation_candle: str
    created_at: float
    valid_until: float


@dataclass
class RecoveryPlan:
    stop_loss: float
    take_profit: float
    level_price: float
    strength: float
    timeframes: list[str]
    atr: float


def _cfg(config, name: str, default):
    return getattr(config, name, default)


def _closed_candles(df: pd.DataFrame, timeframe: str, now: float | None = None) -> pd.DataFrame:
    """Return only candles whose full interval has completed."""
    if df is None or df.empty:
        return pd.DataFrame()
    now_ms = int((now or time.time()) * 1000)
    interval_ms = TF_MS[timeframe]
    out = df.copy()
    ts_ms = pd.to_datetime(out["timestamp"]).astype("int64") // 1_000_000
    out = out[(ts_ms + interval_ms) <= now_ms]
    return out.reset_index(drop=True)


def _fetch_closed(exchange, timeframe: str, lookback: int, now: float | None = None) -> pd.DataFrame:
    # HyperliquidExchange currently over-estimates the requested span for 30m,
    # so always tail() after filtering to make the strategy lookback explicit.
    df = exchange.fetch_ohlcv(timeframe=timeframe, lookback=lookback)
    closed = _closed_candles(df, timeframe, now=now)
    if len(closed) > lookback:
        closed = closed.tail(lookback).reset_index(drop=True)
    return closed


def _ema(values: pd.Series, span: int) -> pd.Series:
    return values.ewm(span=span, adjust=False).mean()


def _trend_leg(df: pd.DataFrame) -> tuple[str, float, dict]:
    if len(df) < 55:
        return "neutral", 0.0, {"reason": "insufficient candles"}

    closes = df["close"].astype(float)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    close = float(closes.iloc[-1])
    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])
    e20_prev = float(ema20.iloc[-4])
    slope = (e20 - e20_prev) / e20_prev if e20_prev else 0.0

    bull_checks = [close > e20, e20 > e50, slope > 0]
    bear_checks = [close < e20, e20 < e50, slope < 0]
    bull_score = sum(bull_checks) / 3
    bear_score = sum(bear_checks) / 3

    if bull_score >= 2 / 3 and bull_score > bear_score:
        direction = "bullish"
        confidence = bull_score * 100
    elif bear_score >= 2 / 3 and bear_score > bull_score:
        direction = "bearish"
        confidence = bear_score * 100
    else:
        direction = "neutral"
        confidence = max(bull_score, bear_score) * 100

    return direction, round(confidence, 1), {
        "close": round(close, 8),
        "ema20": round(e20, 8),
        "ema50": round(e50, 8),
        "ema20_slope_pct": round(slope * 100, 4),
    }


def compute_regime(exchange, config, now: float | None = None) -> Regime:
    """Require 4h and 1h trend agreement before allowing any trade."""
    details: dict[str, dict] = {}
    legs = []
    for tf, lookback in (("4h", 120), ("1h", 140)):
        df = _fetch_closed(exchange, tf, lookback, now=now)
        direction, confidence, detail = _trend_leg(df)
        detail["direction"] = direction
        detail["confidence"] = confidence
        details[tf] = detail
        legs.append((direction, confidence))

    if len(legs) != 2 or legs[0][0] == "neutral" or legs[1][0] == "neutral":
        return Regime("neutral", 0.0, details)
    if legs[0][0] != legs[1][0]:
        return Regime("neutral", 0.0, details)

    confidence = 0.6 * legs[0][1] + 0.4 * legs[1][1]
    minimum = float(_cfg(config, "v2_min_trend_confidence", 70.0))
    if confidence < minimum:
        return Regime("neutral", round(confidence, 1), details)
    return Regime(legs[0][0], round(confidence, 1), details)


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 2:
        return 0.0
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = float(tr.tail(period).mean())
    return value if math.isfinite(value) else 0.0


def _merge_levels(raw: list[tuple[Level, str, float]], current_price: float,
                  tolerance_pct: float) -> list[Level]:
    if not raw:
        return []
    tolerance = tolerance_pct / 100.0
    groups: list[dict] = []

    for level, tf, tf_weight in sorted(raw, key=lambda x: x[0].price):
        matched = None
        for group in groups:
            if abs(level.price - group["price"]) / group["price"] <= tolerance:
                matched = group
                break
        item_weight = max(level.strength, 1.0) * tf_weight
        if matched is None:
            groups.append({
                "price": level.price,
                "weighted_sum": level.price * item_weight,
                "weight": item_weight,
                "levels": [level],
                "tfs": {tf},
            })
        else:
            matched["levels"].append(level)
            matched["tfs"].add(tf)
            matched["weighted_sum"] += level.price * item_weight
            matched["weight"] += item_weight
            matched["price"] = matched["weighted_sum"] / matched["weight"]

    merged: list[Level] = []
    for group in groups:
        price = group["weighted_sum"] / group["weight"]
        tfs = sorted(group["tfs"], key=lambda x: TF_MS.get(x, 0), reverse=True)
        base = max(l.strength for l in group["levels"])
        confluence_bonus = max(0, len(tfs) - 1) * 10
        strength = min(100.0, base + confluence_bonus)
        touches = sum(l.touches for l in group["levels"])
        vol = max((l.volume_avg for l in group["levels"]), default=0.0)
        kind = "support" if price < current_price else "resistance"
        merged.append(Level(
            price=round(float(price), 8),
            kind=kind,
            touches=touches,
            strength=round(strength, 1),
            volume_avg=round(float(vol), 4),
            last_touch_idx=max((l.last_touch_idx for l in group["levels"]), default=0),
            timeframes=tfs,
            fib_ratio=None,
        ))

    merged.sort(key=lambda l: l.strength, reverse=True)
    return merged


def detect_htf_levels(exchange, config, current_price: float,
                      now: float | None = None) -> list[Level]:
    """Build S/R only from 30m, 1h and 4h data."""
    raw: list[tuple[Level, str, float]] = []
    tolerance = float(_cfg(config, "level_tolerance_pct", 0.5))
    min_touches = int(_cfg(config, "min_touches", 2))

    for tf, lookback, tf_weight in LEVEL_TFS:
        try:
            df = _fetch_closed(exchange, tf, lookback, now=now)
            if len(df) < 30:
                continue
            for lv in detect_levels(df, tolerance, min_touches):
                lv.timeframes = [tf]
                raw.append((lv, tf, tf_weight))
        except Exception:
            continue

    return _merge_levels(raw, current_price, tolerance)


def _pick_level(levels: list[Level], regime: Regime, current_price: float, config) -> Level | None:
    min_strength = float(_cfg(config, "v2_min_level_strength", 65.0))
    min_tfs = int(_cfg(config, "v2_min_level_timeframes", 2))
    max_distance = float(_cfg(config, "v2_max_level_distance_pct", 3.0))

    wanted_kind = "support" if regime.direction == "bullish" else "resistance"
    candidates: list[tuple[float, Level]] = []
    for lv in levels:
        if lv.kind != wanted_kind or lv.strength < min_strength:
            continue
        tfs = lv.timeframes or []
        if len(tfs) < min_tfs:
            continue
        if not ({"1h", "4h"} & set(tfs)):
            continue
        distance = abs(current_price - lv.price) / lv.price * 100
        if distance > max_distance:
            continue
        # Strength dominates, but closer levels are preferred when quality is similar.
        effective = lv.strength + len(tfs) * 4 - distance * 3
        candidates.append((effective, lv))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _candle_rejected_level(candle: pd.Series, level: float, side: str,
                           atr: float, config) -> bool:
    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    c = float(candle["close"])
    rng = max(h - l, 1e-12)
    zone_pct = float(_cfg(config, "v2_confirmation_zone_pct", 0.35)) / 100.0
    zone = max(level * zone_pct, atr * 0.25)
    deep_break = max(zone * 1.5, atr * 0.75)

    if side == "long":
        touched = l <= level + zone
        not_destroyed = l >= level - deep_break
        closed_back = c >= level + atr * 0.05
        bullish_body = c > o
        strong_close = (c - l) / rng >= 0.60
        return touched and not_destroyed and closed_back and bullish_body and strong_close

    touched = h >= level - zone
    not_destroyed = h <= level + deep_break
    closed_back = c <= level - atr * 0.05
    bearish_body = c < o
    strong_close = (h - c) / rng >= 0.60
    return touched and not_destroyed and closed_back and bearish_body and strong_close


def _structural_stop(entry: float, level: float, side: str, atr: float, config) -> float | None:
    atr_mult = float(_cfg(config, "v2_sl_atr_mult", 0.75))
    zone_pct = float(_cfg(config, "v2_sl_zone_buffer_pct", 0.35)) / 100.0
    minimum_pct = float(_cfg(config, "v2_min_stop_distance_pct", 0.60)) / 100.0
    maximum_pct = float(_cfg(config, "v2_max_stop_distance_pct", 2.50)) / 100.0
    buffer_abs = max(atr * atr_mult, level * zone_pct)

    if side == "long":
        stop = level - buffer_abs
        minimum_stop = entry * (1 - minimum_pct)
        stop = min(stop, minimum_stop)
        distance = (entry - stop) / entry
    else:
        stop = level + buffer_abs
        minimum_stop = entry * (1 + minimum_pct)
        stop = max(stop, minimum_stop)
        distance = (stop - entry) / entry

    if distance <= 0 or distance > maximum_pct:
        return None
    return float(stop)


def _take_profit(entry: float, stop: float, side: str, levels: list[Level], config) -> float | None:
    target_rr = float(_cfg(config, "v2_target_risk_reward", 1.50))
    tp_buffer_pct = float(_cfg(config, "v2_tp_zone_buffer_pct", 0.15)) / 100.0
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    if side == "long":
        rr_target = entry + risk * target_rr
        resistances = sorted(l.price for l in levels if l.kind == "resistance" and l.price > entry)
        if resistances:
            front = resistances[0] * (1 - tp_buffer_pct)
            # If resistance arrives before the required R:R, the trade is not worth taking.
            if front < rr_target:
                return None
        return float(rr_target)

    rr_target = entry - risk * target_rr
    supports = sorted((l.price for l in levels if l.kind == "support" and l.price < entry), reverse=True)
    if supports:
        front = supports[0] * (1 + tp_buffer_pct)
        if front > rr_target:
            return None
    return float(rr_target)


def build_signal(exchange, config, symbol: str, current_price: float | None = None,
                 now: float | None = None) -> tuple[StrategySignal | None, list[Level], Regime]:
    """Return a confirmed 30m pullback signal or None."""
    now = now or time.time()
    if current_price is None:
        current_price = float(exchange.get_ticker_price())

    regime = compute_regime(exchange, config, now=now)
    levels = detect_htf_levels(exchange, config, current_price, now=now)
    if regime.direction == "neutral":
        return None, levels, regime

    level = _pick_level(levels, regime, current_price, config)
    if level is None:
        return None, levels, regime

    df30 = _fetch_closed(exchange, "30m", 100, now=now)
    if len(df30) < 20:
        return None, levels, regime
    atr = compute_atr(df30)
    if atr <= 0:
        return None, levels, regime

    side = "long" if regime.direction == "bullish" else "short"
    candle = df30.iloc[-1]
    if not _candle_rejected_level(candle, level.price, side, atr, config):
        return None, levels, regime

    pullback_atr = float(_cfg(config, "v2_entry_pullback_atr", 0.15))
    if side == "long":
        entry = level.price + atr * pullback_atr
        # Must remain a passive buy below market after the confirmation close.
        if entry >= current_price:
            return None, levels, regime
    else:
        entry = level.price - atr * pullback_atr
        # Must remain a passive sell above market after the confirmation close.
        if entry <= current_price:
            return None, levels, regime

    stop = _structural_stop(entry, level.price, side, atr, config)
    if stop is None:
        return None, levels, regime
    tp = _take_profit(entry, stop, side, levels, config)
    if tp is None:
        return None, levels, regime

    risk = abs(entry - stop)
    reward = abs(tp - entry)
    rr = reward / risk if risk else 0.0
    candle_ts = pd.Timestamp(candle["timestamp"])
    candle_key = candle_ts.isoformat()
    signal_id = f"{symbol}:{side}:{candle_key}:{level.price:.8f}"

    # Valid only until the current 30m execution candle closes.
    bucket_seconds = 30 * 60
    valid_until = (int(now // bucket_seconds) + 1) * bucket_seconds

    return StrategySignal(
        signal_id=signal_id,
        symbol=symbol,
        side=side,
        kind=level.kind,
        level_price=float(level.price),
        entry_price=float(entry),
        stop_loss=float(stop),
        take_profit=float(tp),
        strength=float(level.strength),
        timeframes=list(level.timeframes or []),
        atr=float(atr),
        risk_reward=round(rr, 2),
        regime=regime,
        confirmation_candle=candle_key,
        created_at=now,
        valid_until=float(valid_until),
    ), levels, regime


def build_recovery_plan(exchange, config, symbol: str, entry: float, side: str,
                        current_price: float | None = None,
                        now: float | None = None) -> tuple[RecoveryPlan, list[Level]]:
    """Create conservative TP/SL for an already-open position after restart."""
    now = now or time.time()
    current_price = float(current_price if current_price is not None else exchange.get_ticker_price())
    levels = detect_htf_levels(exchange, config, current_price, now=now)
    df30 = _fetch_closed(exchange, "30m", 100, now=now)
    atr = compute_atr(df30)
    if atr <= 0:
        atr = entry * 0.01

    if side == "long":
        anchors = sorted((l for l in levels if l.kind == "support" and l.price < entry),
                         key=lambda l: l.price, reverse=True)
    else:
        anchors = sorted((l for l in levels if l.kind == "resistance" and l.price > entry),
                         key=lambda l: l.price)

    if anchors:
        anchor = anchors[0]
        level_price = anchor.price
        strength = anchor.strength
        tfs = list(anchor.timeframes or [])
    else:
        level_price = entry
        strength = 0.0
        tfs = []

    stop = _structural_stop(entry, level_price, side, atr, config)
    if stop is None:
        fallback_pct = min(float(_cfg(config, "v2_max_stop_distance_pct", 2.5)), 1.5) / 100.0
        stop = entry * (1 - fallback_pct) if side == "long" else entry * (1 + fallback_pct)

    tp = _take_profit(entry, stop, side, levels, config)
    if tp is None:
        rr = float(_cfg(config, "v2_target_risk_reward", 1.5))
        risk = abs(entry - stop)
        tp = entry + risk * rr if side == "long" else entry - risk * rr

    return RecoveryPlan(
        stop_loss=float(stop),
        take_profit=float(tp),
        level_price=float(level_price),
        strength=float(strength),
        timeframes=tfs,
        atr=float(atr),
    ), levels
