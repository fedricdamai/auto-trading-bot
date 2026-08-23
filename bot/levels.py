import numpy as np
import pandas as pd
from dataclasses import dataclass, field


@dataclass
class Level:
    price: float
    kind: str          # "support" or "resistance"
    touches: int
    strength: float    # 0-100 composite score
    volume_avg: float
    last_touch_idx: int
    timeframes: list[str] | None = None
    fib_ratio: float | None = None


@dataclass
class DojiSignal:
    signal: str        # "bullish", "bearish", or "neutral"
    strength: float    # 0-100 how significant the doji is
    price: float       # price where the doji formed
    candle_range: float  # total range of the doji candle


@dataclass
class TrendBias:
    direction: str     # "bullish", "bearish", or "neutral"
    confidence: float  # 0-100
    tf_details: dict = field(default_factory=dict)


@dataclass
class BreakoutCheck:
    is_breakout: bool
    is_fakeout: bool
    confidence: float          # 0-100 how confident we are
    volume_confirmed: bool
    momentum_confirmed: bool
    trend_aligned: bool
    retest_seen: bool
    details: str = ""


TF_WEIGHTS = {"1d": 1.5, "4h": 1.0, "1h": 0.6, "5m": 0.3}

FIB_RATIOS = {
    0.236: 30,
    0.382: 35,
    0.500: 40,
    0.618: 45,
    0.786: 38,
}

MULTI_TF_CONFIGS = [
    {"timeframe": "1d", "lookback": 120},
    {"timeframe": "4h", "lookback": 200},
    {"timeframe": "1h", "lookback": 500},
    {"timeframe": "5m", "lookback": 500},
]


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2 / (period + 1)
    ema = np.empty_like(values, dtype=float)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
    return ema


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains) if len(gains) else 0
    avg_loss = np.mean(losses) if len(losses) else 1e-10
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(closes: np.ndarray) -> tuple[float, float]:
    if len(closes) < 26:
        return 0.0, 0.0
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = ema12 - ema26
    signal_line = _ema(macd_line[-9:], 9) if len(macd_line) >= 9 else macd_line[-1:]
    return float(macd_line[-1]), float(signal_line[-1])


def compute_trend_bias(exchange) -> TrendBias:
    tf_configs = [
        {"timeframe": "1m", "lookback": 100, "weight": 0.2},
        {"timeframe": "5m", "lookback": 100, "weight": 0.35},
        {"timeframe": "15m", "lookback": 100, "weight": 0.45},
    ]

    bullish_score = 0.0
    bearish_score = 0.0
    tf_details = {}

    for cfg in tf_configs:
        tf = cfg["timeframe"]
        try:
            df = exchange.fetch_ohlcv(timeframe=tf, lookback=cfg["lookback"])
            closes = df["close"].values
            if len(closes) < 21:
                continue

            ema9 = _ema(closes, 9)
            ema21 = _ema(closes, 21)

            ema9_now = ema9[-1]
            ema21_now = ema21[-1]
            price_now = closes[-1]

            ema_cross = "bullish" if ema9_now > ema21_now else "bearish"
            price_vs_ema = "above" if price_now > ema21_now else "below"

            ema9_prev = ema9[-3]
            ema21_prev = ema21[-3]
            ema9_slope = (ema9_now - ema9_prev) / ema9_prev * 100
            ema21_slope = (ema21_now - ema21_prev) / ema21_prev * 100

            tf_score = 0
            if ema_cross == "bullish":
                tf_score += 1
            else:
                tf_score -= 1
            if price_vs_ema == "above":
                tf_score += 1
            else:
                tf_score -= 1
            if ema9_slope > 0 and ema21_slope > 0:
                tf_score += 0.5
            elif ema9_slope < 0 and ema21_slope < 0:
                tf_score -= 0.5

            w = cfg["weight"]
            if tf_score > 0:
                bullish_score += abs(tf_score) * w
            else:
                bearish_score += abs(tf_score) * w

            tf_details[tf] = {
                "ema_cross": ema_cross,
                "price_vs_ema21": price_vs_ema,
                "ema9": round(ema9_now, 2),
                "ema21": round(ema21_now, 2),
                "price": round(price_now, 2),
                "ema9_slope": round(ema9_slope, 4),
                "score": round(tf_score, 2),
            }
        except Exception as e:
            tf_details[tf] = {"error": str(e)}
            continue

    total = bullish_score + bearish_score
    if total == 0:
        return TrendBias(direction="neutral", confidence=0, tf_details=tf_details)

    if bullish_score > bearish_score:
        direction = "bullish"
        confidence = (bullish_score / total) * 100
    elif bearish_score > bullish_score:
        direction = "bearish"
        confidence = (bearish_score / total) * 100
    else:
        direction = "neutral"
        confidence = 50

    return TrendBias(
        direction=direction,
        confidence=round(min(confidence, 100), 1),
        tf_details=tf_details,
    )


def check_breakout_fakeout(
    exchange, level: "Level", current_price: float,
) -> BreakoutCheck:
    try:
        df = exchange.fetch_ohlcv(timeframe="5m", lookback=60)
    except Exception:
        return BreakoutCheck(
            is_breakout=False, is_fakeout=False, confidence=0,
            volume_confirmed=False, momentum_confirmed=False,
            trend_aligned=False, retest_seen=False,
            details="Could not fetch candle data",
        )

    closes = df["close"].values
    volumes = df["volume"].values
    highs = df["high"].values
    lows = df["low"].values

    distance_pct = abs(current_price - level.price) / level.price * 100
    broke_through = (
        (level.kind == "resistance" and current_price > level.price)
        or (level.kind == "support" and current_price < level.price)
    )

    if not broke_through and distance_pct > 0.5:
        return BreakoutCheck(
            is_breakout=False, is_fakeout=False, confidence=0,
            volume_confirmed=False, momentum_confirmed=False,
            trend_aligned=False, retest_seen=False,
            details="Price has not reached the level yet",
        )

    recent_vol = np.mean(volumes[-5:]) if len(volumes) >= 5 else np.mean(volumes)
    avg_vol = np.mean(volumes[-30:]) if len(volumes) >= 30 else np.mean(volumes)
    volume_confirmed = recent_vol > avg_vol * 1.5

    rsi_val = _rsi(closes)
    macd_val, macd_signal = _macd(closes)

    if level.kind == "resistance":
        momentum_confirmed = rsi_val > 55 and macd_val > macd_signal
    else:
        momentum_confirmed = rsi_val < 45 and macd_val < macd_signal

    try:
        trend = compute_trend_bias(exchange)
        if level.kind == "resistance":
            trend_aligned = trend.direction == "bullish" and trend.confidence >= 55
        else:
            trend_aligned = trend.direction == "bearish" and trend.confidence >= 55
    except Exception:
        trend_aligned = False
        trend = None

    retest_seen = False
    if broke_through and len(closes) >= 10:
        last_10_lows = lows[-10:]
        last_10_highs = highs[-10:]
        tol = level.price * 0.003

        if level.kind == "resistance":
            retouched = any(abs(lo - level.price) <= tol for lo in last_10_lows)
            held_above = closes[-1] > level.price
            retest_seen = retouched and held_above
        else:
            retouched = any(abs(hi - level.price) <= tol for hi in last_10_highs)
            held_below = closes[-1] < level.price
            retest_seen = retouched and held_below

    score = 0
    checks_passed = 0
    total_checks = 4

    if volume_confirmed:
        score += 30
        checks_passed += 1
    if momentum_confirmed:
        score += 25
        checks_passed += 1
    if trend_aligned:
        score += 25
        checks_passed += 1
    if retest_seen:
        score += 20
        checks_passed += 1

    is_breakout = broke_through and checks_passed >= 3
    is_fakeout = broke_through and checks_passed <= 1

    details_parts = []
    details_parts.append(f"Vol: {'OK' if volume_confirmed else 'LOW'} ({recent_vol:.0f} vs avg {avg_vol:.0f})")
    details_parts.append(f"RSI: {rsi_val:.1f} MACD: {macd_val:.4f}/{macd_signal:.4f}")
    details_parts.append(f"Trend: {trend.direction if trend else '?'} ({trend.confidence if trend else 0:.0f}%)")
    details_parts.append(f"Retest: {'YES' if retest_seen else 'NO'}")
    details_parts.append(f"Score: {checks_passed}/{total_checks} checks passed")

    return BreakoutCheck(
        is_breakout=is_breakout,
        is_fakeout=is_fakeout,
        confidence=round(min(score, 100), 1),
        volume_confirmed=volume_confirmed,
        momentum_confirmed=momentum_confirmed,
        trend_aligned=trend_aligned,
        retest_seen=retest_seen,
        details=" | ".join(details_parts),
    )


def detect_levels(df: pd.DataFrame, tolerance_pct: float, min_touches: int) -> list[Level]:
    return _detect_from_df(df, tolerance_pct, min_touches, tf_label=None)


def detect_levels_multi_tf(
    exchange,
    tolerance_pct: float,
    min_touches: int,
) -> list[Level]:
    all_raw_levels: list[tuple[Level, str]] = []

    for cfg in MULTI_TF_CONFIGS:
        tf = cfg["timeframe"]
        try:
            df = exchange.fetch_ohlcv(timeframe=tf, lookback=cfg["lookback"])
            levels = _detect_from_df(df, tolerance_pct, min_touches, tf_label=tf)
            for lv in levels:
                all_raw_levels.append((lv, tf))
        except Exception:
            continue

    if not all_raw_levels:
        return []

    return _merge_multi_tf_levels(all_raw_levels, tolerance_pct)


def detect_doji(df: pd.DataFrame, lookback: int = 5) -> DojiSignal | None:
    if len(df) < lookback + 1:
        return None

    recent = df.iloc[-(lookback + 1):-1]
    avg_range = (recent["high"] - recent["low"]).mean()
    if avg_range == 0:
        return None

    candle = df.iloc[-2]
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o)
    full_range = h - l

    if full_range == 0:
        return None

    body_ratio = body / full_range
    range_ratio = full_range / avg_range

    if body_ratio > 0.15:
        return None
    if range_ratio < 1.2:
        return None

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    strength = min(100.0, range_ratio * 30 + (1 - body_ratio) * 20)

    if lower_wick > upper_wick * 2.5:
        signal = "bullish"
        strength += 15
    elif upper_wick > lower_wick * 2.5:
        signal = "bearish"
        strength += 15
    else:
        signal = "neutral"

    return DojiSignal(
        signal=signal,
        strength=min(strength, 100),
        price=round((h + l) / 2, 2),
        candle_range=round(full_range, 2),
    )


def _detect_from_df(
    df: pd.DataFrame, tolerance_pct: float, min_touches: int, tf_label: str | None,
) -> list[Level]:
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    volumes = df["volume"].values
    current_price = closes[-1]
    tolerance = tolerance_pct / 100.0
    n = len(closes)

    swing_points = _find_swing_points(highs, lows, closes, window=5)
    clusters = _cluster_levels(swing_points, tolerance)

    levels = []
    for cluster_price in clusters:
        touches, vol_at_touches, last_touch, rejection_score = _analyze_level(
            cluster_price, highs, lows, closes, volumes, tolerance,
        )
        if touches < min_touches:
            continue

        recency = max(0, 1 - (n - 1 - last_touch) / n) if last_touch >= 0 else 0

        strength = (
            min(touches / 6, 1.0) * 30
            + recency * 25
            + min(rejection_score / 3, 1.0) * 25
            + _volume_score(vol_at_touches, volumes) * 20
        )

        kind = "support" if cluster_price < current_price else "resistance"
        tfs = [tf_label] if tf_label else None

        levels.append(Level(
            price=round(cluster_price, 2),
            kind=kind,
            touches=touches,
            strength=round(strength, 1),
            volume_avg=round(vol_at_touches, 2),
            last_touch_idx=last_touch,
            timeframes=tfs,
        ))

    fib_levels = _compute_fib_levels(highs, lows, closes, volumes, tolerance, n, tf_label)
    for fib_lv in fib_levels:
        duplicate = any(
            abs(fib_lv.price - lv.price) / lv.price <= tolerance
            for lv in levels
        )
        if not duplicate:
            levels.append(fib_lv)

    levels.sort(key=lambda l: l.strength, reverse=True)
    return levels


def _compute_fib_levels(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
    volumes: np.ndarray, tolerance: float, n: int, tf_label: str | None,
) -> list[Level]:
    highest = float(np.max(highs))
    lowest = float(np.min(lows))
    price_range = highest - lowest
    if price_range <= 0:
        return []

    current_price = closes[-1]
    tfs = [tf_label] if tf_label else None
    levels = []

    for ratio, base_strength in FIB_RATIOS.items():
        fib_price = round(lowest + price_range * ratio, 2)

        touches, vol_at_touches, last_touch, rejection_score = _analyze_level(
            fib_price, highs, lows, closes, volumes, tolerance,
        )

        recency = max(0, 1 - (n - 1 - last_touch) / n) if last_touch >= 0 else 0

        strength = (
            base_strength
            + min(touches / 4, 1.0) * 20
            + recency * 15
            + _volume_score(vol_at_touches, volumes) * 10
        )

        kind = "support" if fib_price < current_price else "resistance"

        levels.append(Level(
            price=fib_price,
            kind=kind,
            touches=touches,
            strength=round(min(strength, 100), 1),
            volume_avg=round(vol_at_touches, 2),
            last_touch_idx=last_touch,
            timeframes=tfs,
            fib_ratio=ratio,
        ))

    return levels


def _merge_multi_tf_levels(
    raw: list[tuple[Level, str]], tolerance_pct: float,
) -> list[Level]:
    tolerance = tolerance_pct / 100.0
    merged: list[dict] = []

    for level, tf in raw:
        matched = False
        for group in merged:
            ref = group["price"]
            if abs(level.price - ref) / ref <= tolerance:
                group["levels"].append(level)
                group["tfs"].add(tf)
                group["price"] = np.mean([l.price for l in group["levels"]])
                matched = True
                break

        if not matched:
            merged.append({
                "price": level.price,
                "levels": [level],
                "tfs": {tf},
            })

    result = []
    for group in merged:
        lvs = group["levels"]
        tfs = group["tfs"]
        price = round(group["price"], 2)

        base_strength = max(l.strength for l in lvs)
        best = max(lvs, key=lambda l: l.strength)

        tf_bonus = sum(TF_WEIGHTS.get(t, 1.0) for t in tfs)

        confluence_bonus = 0
        if len(tfs) >= 4:
            confluence_bonus = 30
        elif len(tfs) >= 3:
            confluence_bonus = 25
        elif len(tfs) >= 2:
            confluence_bonus = 15

        total_touches = sum(l.touches for l in lvs)
        strength = min(base_strength * (tf_bonus / len(tfs)) + confluence_bonus, 100)

        fib = next((l.fib_ratio for l in lvs if l.fib_ratio is not None), None)

        result.append(Level(
            price=price,
            kind=best.kind,
            touches=total_touches,
            strength=round(strength, 1),
            volume_avg=round(max(l.volume_avg for l in lvs), 2),
            last_touch_idx=best.last_touch_idx,
            timeframes=sorted(tfs),
            fib_ratio=fib,
        ))

    result.sort(key=lambda l: l.strength, reverse=True)
    return result


def _find_swing_points(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, window: int,
) -> list[float]:
    points = []
    n = len(highs)

    for w in [window, window * 2]:
        for i in range(w, n - w):
            if highs[i] == max(highs[i - w : i + w + 1]):
                points.append(highs[i])
            if lows[i] == min(lows[i - w : i + w + 1]):
                points.append(lows[i])

    for i in range(n):
        body = abs(closes[i] - closes[max(0, i - 1)])
        upper_wick = highs[i] - max(closes[i], closes[max(0, i - 1)])
        lower_wick = min(closes[i], closes[max(0, i - 1)]) - lows[i]
        if body > 0:
            if upper_wick / body > 2:
                points.append(highs[i])
            if lower_wick / body > 2:
                points.append(lows[i])

    return points


def _cluster_levels(prices: list[float], tolerance: float) -> list[float]:
    if not prices:
        return []

    sorted_prices = sorted(prices)
    clusters: list[list[float]] = [[sorted_prices[0]]]

    for price in sorted_prices[1:]:
        cluster_mean = np.mean(clusters[-1])
        if abs(price - cluster_mean) / cluster_mean <= tolerance:
            clusters[-1].append(price)
        else:
            clusters.append([price])

    return [round(float(np.mean(c)), 2) for c in clusters]


def _analyze_level(
    level: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    tolerance: float,
) -> tuple[int, float, int, float]:
    band_low = level * (1 - tolerance)
    band_high = level * (1 + tolerance)

    touches = 0
    total_vol = 0.0
    last_touch = -1
    rejection_sum = 0.0

    for i in range(len(highs)):
        if lows[i] <= band_high and highs[i] >= band_low:
            touches += 1
            total_vol += volumes[i]
            last_touch = i

            if i + 1 < len(closes):
                move = abs(closes[i + 1] - level) / level * 100
                rejection_sum += move

    avg_vol = total_vol / touches if touches > 0 else 0
    avg_rejection = rejection_sum / touches if touches > 0 else 0

    return touches, avg_vol, last_touch, avg_rejection


def _volume_score(vol_at_level: float, all_volumes: np.ndarray) -> float:
    avg = np.mean(all_volumes)
    if avg == 0:
        return 0
    ratio = vol_at_level / avg
    return min(ratio / 2, 1.0)


def compute_tp_sl(
    entry: float, leverage: int, target_pnl_pct: float, max_loss_pct: float,
    side: str = "long",
) -> dict:
    tp_move = target_pnl_pct / leverage
    sl_move = max_loss_pct / leverage

    if side == "long":
        liq_price = entry * (1 - (1 / leverage) + 0.02)
        max_sl_move = (1 / leverage) * 0.5 * 100
        sl_move = min(sl_move, max_sl_move)

        tp_price = round(entry * (1 + tp_move / 100), 2)
        sl_price = round(entry * (1 - sl_move / 100), 2)

        if sl_price <= liq_price:
            sl_price = round(liq_price * 1.02, 2)
    else:
        liq_price = entry * (1 + (1 / leverage) - 0.02)
        max_sl_move = (1 / leverage) * 0.5 * 100
        sl_move = min(sl_move, max_sl_move)

        tp_price = round(entry * (1 - tp_move / 100), 2)
        sl_price = round(entry * (1 + sl_move / 100), 2)

        if sl_price >= liq_price:
            sl_price = round(liq_price * 0.98, 2)

    return {
        "tp_price": tp_price,
        "sl_price": sl_price,
        "tp_move_pct": round(tp_move, 4),
        "sl_move_pct": round(sl_move, 4),
        "liq_price": round(liq_price, 2),
    }


