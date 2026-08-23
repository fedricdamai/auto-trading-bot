import numpy as np
import pandas as pd
from dataclasses import dataclass


@dataclass
class Level:
    price: float
    kind: str          # "support" or "resistance"
    touches: int       # how many times price reacted here
    strength: float    # 0-100 composite score
    volume_avg: float  # average volume at touches
    last_touch_idx: int  # recency — how many candles ago
    timeframes: list[str] | None = None  # which timeframes confirmed this level


# Timeframe weights — higher timeframe = more significant level
TF_WEIGHTS = {"1d": 1.5, "4h": 1.0, "1h": 0.6}

MULTI_TF_CONFIGS = [
    {"timeframe": "1d", "lookback": 120},
    {"timeframe": "4h", "lookback": 200},
    {"timeframe": "1h", "lookback": 200},
]


def detect_levels(df: pd.DataFrame, tolerance_pct: float, min_touches: int) -> list[Level]:
    """Detect and score support/resistance levels from OHLCV data (single timeframe).

    Scoring combines:
      - Touch count (more reactions = stronger)
      - Volume at touches (high volume reactions = institutional interest)
      - Recency (recent levels matter more)
      - Rejection strength (how far price bounced from the level)
    """
    return _detect_from_df(df, tolerance_pct, min_touches, tf_label=None)


def detect_levels_multi_tf(
    exchange,
    tolerance_pct: float,
    min_touches: int,
) -> list[Level]:
    """Detect S/R levels across 1D, 4H, and 1H timeframes.

    Levels confirmed on multiple timeframes get a strength bonus.
    Higher timeframe levels are weighted more heavily.
    """
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

    current_price = all_raw_levels[0][0].price  # placeholder, recalculated below
    for lv, _ in all_raw_levels:
        if lv.last_touch_idx >= 0:
            current_price = lv.price  # just need any reference
            break

    return _merge_multi_tf_levels(all_raw_levels, tolerance_pct)


def _detect_from_df(
    df: pd.DataFrame, tolerance_pct: float, min_touches: int, tf_label: str | None,
) -> list[Level]:
    """Core detection from a single DataFrame."""
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

    levels.sort(key=lambda l: l.strength, reverse=True)
    return levels


def _merge_multi_tf_levels(
    raw: list[tuple[Level, str]], tolerance_pct: float,
) -> list[Level]:
    """Merge levels from different timeframes. Confluence boosts strength."""
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

        # Timeframe weight bonus: higher TFs boost the score
        tf_bonus = sum(TF_WEIGHTS.get(t, 1.0) for t in tfs)

        # Multi-TF confluence bonus: confirmed on 2 TFs = +15, all 3 = +25
        confluence_bonus = 0
        if len(tfs) >= 3:
            confluence_bonus = 25
        elif len(tfs) >= 2:
            confluence_bonus = 15

        total_touches = sum(l.touches for l in lvs)
        strength = min(base_strength * (tf_bonus / len(tfs)) + confluence_bonus, 100)

        result.append(Level(
            price=price,
            kind=best.kind,
            touches=total_touches,
            strength=round(strength, 1),
            volume_avg=round(max(l.volume_avg for l in lvs), 2),
            last_touch_idx=best.last_touch_idx,
            timeframes=sorted(tfs),
        ))

    result.sort(key=lambda l: l.strength, reverse=True)
    return result


def _find_swing_points(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, window: int,
) -> list[float]:
    """Identify swing highs and swing lows using multiple window sizes."""
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
    """Group nearby price points into single levels using weighted mean."""
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
    """Analyze how price interacts with a level.

    Returns (touch_count, avg_volume_at_touches, last_touch_index, avg_rejection_pct).
    """
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
    """Score 0-1 based on how the volume at this level compares to average."""
    avg = np.mean(all_volumes)
    if avg == 0:
        return 0
    ratio = vol_at_level / avg
    return min(ratio / 2, 1.0)


def compute_tp_sl(
    entry: float, leverage: int, target_pnl_pct: float, max_loss_pct: float,
) -> dict:
    """Calculate TP/SL prices for a leveraged long position.

    Args:
        entry: entry price
        leverage: position leverage (1-5)
        target_pnl_pct: desired profit % on margin (e.g. 1.0 = 1%)
        max_loss_pct: max loss % on margin (e.g. 1.0 = 1%)

    Returns dict with tp_price, sl_price, tp_move_pct, sl_move_pct, liq_price.
    """
    # Price move needed: margin_pnl% = price_move% × leverage
    tp_move = target_pnl_pct / leverage  # price % move for target
    sl_move = max_loss_pct / leverage    # price % move for stop

    # Liquidation price (simplified): entry × (1 - 1/leverage)
    # Add 2% buffer for fees/funding
    liq_price = entry * (1 - (1 / leverage) + 0.02)

    # SL must stay above liquidation with safety margin
    max_sl_move = (1 / leverage) * 0.5 * 100  # 50% of liquidation distance
    sl_move = min(sl_move, max_sl_move)

    tp_price = round(entry * (1 + tp_move / 100), 2)
    sl_price = round(entry * (1 - sl_move / 100), 2)

    # Hard floor: SL must be above liquidation
    if sl_price <= liq_price:
        sl_price = round(liq_price * 1.02, 2)

    return {
        "tp_price": tp_price,
        "sl_price": sl_price,
        "tp_move_pct": round(tp_move, 4),
        "sl_move_pct": round(sl_move, 4),
        "liq_price": round(liq_price, 2),
    }


def get_limit_order_prices(
    levels: list[Level],
    current_price: float,
    leverage: int = 1,
    target_pnl_pct: float = 1.0,
    max_loss_pct: float = 1.0,
    max_orders: int = 5,
) -> list[dict]:
    """Pick the best levels to place limit orders at.

    TP/SL are calculated based on leverage so each trade targets
    the specified margin PnL % while staying safe from liquidation.
    """
    orders = []
    used_prices = set()

    for level in levels:
        if len(orders) >= max_orders:
            break

        too_close = any(abs(level.price - p) / current_price < 0.005 for p in used_prices)
        if too_close:
            continue

        if level.kind == "support":
            entry = level.price
        else:
            entry = round(level.price * 1.002, 2)

        distance_pct = abs(current_price - entry) / current_price * 100
        if distance_pct > 10:
            continue

        tpsl = compute_tp_sl(entry, leverage, target_pnl_pct, max_loss_pct)

        orders.append({
            "price": entry,
            "kind": level.kind,
            "strength": level.strength,
            "touches": level.touches,
            "timeframes": level.timeframes,
            "tp_price": tpsl["tp_price"],
            "sl_price": tpsl["sl_price"],
            "tp_move_pct": tpsl["tp_move_pct"],
            "sl_move_pct": tpsl["sl_move_pct"],
            "liq_price": tpsl["liq_price"],
        })
        used_prices.add(level.price)

    return orders
