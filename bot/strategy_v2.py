"""Trader V2 strategy: reaction-based S/R first, trend second.

Key design:
- Support/resistance is defined by repeated reactions on its OWN timeframe.
- 30m and 1h levels are detected independently from 200 closed candles each.
- Support stays support because lows repeatedly rebounded there.
- Resistance stays resistance because highs repeatedly rejected there.
- Current price being above/below a level never changes its type by itself.
- Trade direction comes from the nearest/highest-quality actionable zone.
- 4h + 1h EMA regime is context and a small scoring input, not a direction gate.
- Entries are passive limits at validated S/R.
- SL sits beyond the zone.
- TP is placed before the nearest opposing S/R zone and must meet minimum R:R.

No 1m/5m signal inputs are used.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

import pandas as pd

from bot.levels import Level
from bot.sr_v2 import (
    SR_BY_TIMEFRAME,
    detect_timeframe_levels,
    latest_atr,
    merge_timeframe_levels,
    zone_half_width,
)


TF_MS = {
    "30m": 30 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}


@dataclass
class Regime:
    direction: str
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
    if df is None or df.empty:
        return pd.DataFrame()
    now_ms = int((now or time.time()) * 1000)
    interval_ms = TF_MS[timeframe]
    out = df.copy()
    ts_ms = pd.to_datetime(out["timestamp"]).astype("int64") // 1_000_000
    out = out[(ts_ms + interval_ms) <= now_ms]
    return out.reset_index(drop=True)


def _fetch_closed(exchange, timeframe: str, lookback: int, now: float | None = None) -> pd.DataFrame:
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
    """4h + 1h trend context, not a hard trade-direction gate."""
    details: dict[str, dict] = {}
    legs = []
    for tf, lookback in (("4h", 120), ("1h", 140)):
        df = _fetch_closed(exchange, tf, lookback, now=now)
        direction, confidence, detail = _trend_leg(df)
        detail["direction"] = direction
        detail["confidence"] = confidence
        details[tf] = detail
        legs.append((direction, confidence))

    if len(legs) != 2:
        return Regime("neutral", 0.0, details)
    if legs[0][0] == "neutral" or legs[1][0] == "neutral":
        return Regime("neutral", 0.0, details)
    if legs[0][0] != legs[1][0]:
        return Regime("neutral", 0.0, details)

    confidence = 0.6 * legs[0][1] + 0.4 * legs[1][1]
    return Regime(legs[0][0], round(confidence, 1), details)


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    return latest_atr(df, period)


def detect_htf_levels(
    exchange,
    config,
    current_price: float,
    now: float | None = None,
) -> list[Level]:
    """Build reaction-based S/R from 30m and 1h independently."""
    del config, current_price

    raw: list[Level] = []
    atr_by_tf: dict[str, float] = {}
    for tf in ("30m", "1h"):
        params = SR_BY_TIMEFRAME[tf]
        try:
            df = _fetch_closed(exchange, tf, params.lookback, now=now)
            atr_by_tf[tf] = latest_atr(df)
            raw.extend(detect_timeframe_levels(df, tf))
        except Exception:
            continue

    return merge_timeframe_levels(raw, atr_by_tf)


def _candle_rejected_level(
    candle: pd.Series,
    level: float,
    side: str,
    atr: float,
    config,
) -> bool:
    """Optional rejection diagnostic retained for tests and telemetry."""
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


def _reference_atr(level: Level, atr_by_tf: dict[str, float], fallback: float) -> float:
    vals = [atr_by_tf.get(tf, 0.0) for tf in (level.timeframes or [])]
    vals = [v for v in vals if v > 0]
    return max(vals, default=fallback)


def _structural_stop(
    entry: float,
    level: float,
    side: str,
    atr: float,
    config,
    zone_half_width_override: float | None = None,
) -> float | None:
    """Put SL beyond the S/R zone, never inside it."""
    if atr <= 0:
        return None

    if zone_half_width_override is None:
        zone_half_width_override = max(
            atr * 0.35,
            level * float(_cfg(config, "v2_sl_zone_buffer_pct", 0.35)) / 100.0,
        )

    extra = atr * 0.15
    distance_abs = zone_half_width_override + extra

    if side == "long":
        stop = level - distance_abs
        distance = (entry - stop) / entry
    else:
        stop = level + distance_abs
        distance = (stop - entry) / entry

    min_pct = float(_cfg(config, "v2_min_stop_distance_pct", 0.60)) / 100.0
    max_pct = float(_cfg(config, "v2_max_stop_distance_pct", 2.50)) / 100.0

    if distance < min_pct:
        stop = entry * (1 - min_pct) if side == "long" else entry * (1 + min_pct)
        distance = min_pct

    if distance <= 0 or distance > max_pct:
        return None
    return float(stop)


def _target_from_opposing_level(
    entry: float,
    stop: float,
    side: str,
    levels: list[Level],
    config,
    atr_by_tf: dict[str, float] | None = None,
) -> tuple[float | None, Level | None]:
    atr_by_tf = atr_by_tf or {}
    risk = abs(entry - stop)
    if risk <= 0:
        return None, None

    if side == "long":
        opposing = sorted(
            (lv for lv in levels if lv.kind == "resistance" and lv.price > entry),
            key=lambda lv: lv.price,
        )
    else:
        opposing = sorted(
            (lv for lv in levels if lv.kind == "support" and lv.price < entry),
            key=lambda lv: lv.price,
            reverse=True,
        )

    if not opposing:
        return None, None

    target_level = opposing[0]
    width = zone_half_width(target_level, atr_by_tf)
    pct_buffer = target_level.price * float(_cfg(config, "v2_tp_zone_buffer_pct", 0.15)) / 100.0
    front_buffer = max(width * 0.25, pct_buffer)

    if side == "long":
        tp = target_level.price - front_buffer
        reward = tp - entry
    else:
        tp = target_level.price + front_buffer
        reward = entry - tp

    if reward <= 0:
        return None, target_level

    rr = reward / risk
    minimum_rr = float(_cfg(config, "v2_target_risk_reward", 1.50))
    if rr < minimum_rr:
        return None, target_level

    return float(tp), target_level


def _take_profit(
    entry: float,
    stop: float,
    side: str,
    levels: list[Level],
    config,
    atr_by_tf: dict[str, float] | None = None,
) -> float | None:
    tp, _ = _target_from_opposing_level(entry, stop, side, levels, config, atr_by_tf)
    return tp


def _level_tradeable(level: Level, config) -> bool:
    min_strength = float(_cfg(config, "v2_min_level_strength", 65.0))
    if level.strength < min_strength:
        return False

    tfs = set(level.timeframes or [])
    if "1h" in tfs:
        return level.touches >= 2
    if "30m" in tfs:
        return level.touches >= 3 and level.strength >= max(min_strength, 70.0)
    return False


def _candidate_score(
    level: Level,
    side: str,
    current_price: float,
    regime: Regime,
    config,
) -> float | None:
    if not _level_tradeable(level, config):
        return None

    if side == "long":
        if level.kind != "support" or level.price >= current_price:
            return None
        distance_pct = (current_price - level.price) / level.price * 100
    else:
        if level.kind != "resistance" or level.price <= current_price:
            return None
        distance_pct = (level.price - current_price) / level.price * 100

    max_distance = float(_cfg(config, "v2_max_level_distance_pct", 3.0))
    if distance_pct > max_distance:
        return None

    tfs = set(level.timeframes or [])
    tf_bonus = 12.0 if {"30m", "1h"}.issubset(tfs) else (8.0 if "1h" in tfs else 3.0)
    proximity = max(0.0, 24.0 - distance_pct * 10.0)

    regime_bonus = 0.0
    if regime.direction != "neutral":
        aligned = (side == "long" and regime.direction == "bullish") or (
            side == "short" and regime.direction == "bearish"
        )
        regime_bonus = 5.0 if aligned else -3.0

    return level.strength + tf_bonus + proximity + regime_bonus


def _pick_trade_level(
    levels: list[Level],
    current_price: float,
    regime: Regime,
    config,
) -> tuple[Level, str] | None:
    candidates: list[tuple[float, Level, str]] = []
    for lv in levels:
        side = "long" if lv.kind == "support" else "short"
        score = _candidate_score(lv, side, current_price, regime, config)
        if score is not None:
            candidates.append((score, lv, side))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, level, side = candidates[0]
    return level, side


def build_signal(
    exchange,
    config,
    symbol: str,
    current_price: float | None = None,
    now: float | None = None,
) -> tuple[StrategySignal | None, list[Level], Regime]:
    """Return the best passive S/R setup, regardless of regime direction."""
    now = now or time.time()
    if current_price is None:
        current_price = float(exchange.get_ticker_price())

    regime = compute_regime(exchange, config, now=now)
    levels = detect_htf_levels(exchange, config, current_price, now=now)

    df30 = _fetch_closed(exchange, "30m", SR_BY_TIMEFRAME["30m"].lookback, now=now)
    df1h = _fetch_closed(exchange, "1h", SR_BY_TIMEFRAME["1h"].lookback, now=now)
    atr30 = latest_atr(df30)
    atr1h = latest_atr(df1h)
    if atr30 <= 0:
        return None, levels, regime
    atr_by_tf = {"30m": atr30, "1h": atr1h}

    picked = _pick_trade_level(levels, current_price, regime, config)
    if picked is None:
        return None, levels, regime

    level, side = picked
    entry = float(level.price)
    if side == "long" and entry >= current_price:
        return None, levels, regime
    if side == "short" and entry <= current_price:
        return None, levels, regime

    ref_atr = _reference_atr(level, atr_by_tf, atr30)
    width = zone_half_width(level, atr_by_tf)
    stop = _structural_stop(
        entry,
        level.price,
        side,
        ref_atr,
        config,
        zone_half_width_override=width,
    )
    if stop is None:
        return None, levels, regime

    tp = _take_profit(entry, stop, side, levels, config, atr_by_tf=atr_by_tf)
    if tp is None:
        return None, levels, regime

    risk = abs(entry - stop)
    reward = abs(tp - entry)
    rr = reward / risk if risk else 0.0

    candle = df30.iloc[-1]
    candle_key = pd.Timestamp(candle["timestamp"]).isoformat()
    signal_id = f"{symbol}:{side}:{candle_key}:{level.kind}:{level.price:.8f}"

    bucket_seconds = 30 * 60
    valid_until = (int(now // bucket_seconds) + 1) * bucket_seconds

    return StrategySignal(
        signal_id=signal_id,
        symbol=symbol,
        side=side,
        kind=level.kind,
        level_price=float(level.price),
        entry_price=entry,
        stop_loss=float(stop),
        take_profit=float(tp),
        strength=float(level.strength),
        timeframes=list(level.timeframes or []),
        atr=float(ref_atr),
        risk_reward=round(rr, 2),
        regime=regime,
        confirmation_candle=candle_key,
        created_at=now,
        valid_until=float(valid_until),
    ), levels, regime


def build_recovery_plan(
    exchange,
    config,
    symbol: str,
    entry: float,
    side: str,
    current_price: float | None = None,
    now: float | None = None,
) -> tuple[RecoveryPlan, list[Level]]:
    """Rebuild structural protection for an already-open exchange position."""
    now = now or time.time()
    current_price = float(
        current_price if current_price is not None else exchange.get_ticker_price()
    )
    levels = detect_htf_levels(exchange, config, current_price, now=now)

    df30 = _fetch_closed(exchange, "30m", SR_BY_TIMEFRAME["30m"].lookback, now=now)
    df1h = _fetch_closed(exchange, "1h", SR_BY_TIMEFRAME["1h"].lookback, now=now)
    atr30 = latest_atr(df30) or entry * 0.01
    atr1h = latest_atr(df1h)
    atr_by_tf = {"30m": atr30, "1h": atr1h}

    if side == "long":
        anchors = sorted(
            (lv for lv in levels if lv.kind == "support" and lv.price <= entry),
            key=lambda lv: lv.price,
            reverse=True,
        )
    else:
        anchors = sorted(
            (lv for lv in levels if lv.kind == "resistance" and lv.price >= entry),
            key=lambda lv: lv.price,
        )

    if anchors:
        anchor = anchors[0]
        level_price = anchor.price
        strength = anchor.strength
        tfs = list(anchor.timeframes or [])
        ref_atr = _reference_atr(anchor, atr_by_tf, atr30)
        width = zone_half_width(anchor, atr_by_tf)
    else:
        level_price = entry
        strength = 0.0
        tfs = []
        ref_atr = atr30
        width = atr30 * 0.35

    stop = _structural_stop(
        entry,
        level_price,
        side,
        ref_atr,
        config,
        zone_half_width_override=width,
    )
    if stop is None:
        fallback_pct = min(
            float(_cfg(config, "v2_max_stop_distance_pct", 2.5)),
            1.5,
        ) / 100.0
        stop = entry * (1 - fallback_pct) if side == "long" else entry * (1 + fallback_pct)

    tp = _take_profit(entry, stop, side, levels, config, atr_by_tf=atr_by_tf)
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
        atr=float(ref_atr),
    ), levels
