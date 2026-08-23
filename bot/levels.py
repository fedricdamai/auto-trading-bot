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


def detect_levels(df: pd.DataFrame, tolerance_pct: float, min_touches: int) -> list[Level]:
    """Detect and score support/resistance levels from 4H OHLCV data.

    Scoring combines:
      - Touch count (more reactions = stronger)
      - Volume at touches (high volume reactions = institutional interest)
      - Recency (recent levels matter more)
      - Rejection strength (how far price bounced from the level)
    """
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
            min(touches / 6, 1.0) * 30          # touch count (max 30 pts)
            + recency * 25                       # recency (max 25 pts)
            + min(rejection_score / 3, 1.0) * 25 # bounce strength (max 25 pts)
            + _volume_score(vol_at_touches, volumes) * 20  # volume (max 20 pts)
        )

        kind = "support" if cluster_price < current_price else "resistance"

        levels.append(Level(
            price=round(cluster_price, 2),
            kind=kind,
            touches=touches,
            strength=round(strength, 1),
            volume_avg=round(vol_at_touches, 2),
            last_touch_idx=last_touch,
        ))

    levels.sort(key=lambda l: l.strength, reverse=True)
    return levels


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

    # Also add prominent wicks — candles where the wick is large vs the body
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

            # Measure rejection: how far did the next candle move away?
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


def get_limit_order_prices(levels: list[Level], current_price: float, max_orders: int = 5) -> list[dict]:
    """Pick the best levels to place limit orders at.

    For support: place limit buy slightly above the level (catch the bounce).
    For resistance: place limit buy slightly above (catch the breakout).

    Returns list of {price, kind, strength, stop_loss_pct, take_profit_pct}.
    """
    orders = []
    used_prices = set()

    for level in levels:
        if len(orders) >= max_orders:
            break

        # Skip levels too close to an already-selected one
        too_close = any(abs(level.price - p) / current_price < 0.005 for p in used_prices)
        if too_close:
            continue

        if level.kind == "support":
            # Buy at support: place limit at the level price (waiting for price to drop to it)
            entry = level.price
            # Tighter SL for strong levels, wider for weak
            sl_pct = 1.5 if level.strength > 60 else 2.5
            tp_pct = sl_pct * 2  # 2:1 reward-to-risk minimum
        else:
            # Buy at resistance breakout: place limit just above resistance
            entry = round(level.price * 1.002, 2)
            sl_pct = 2.0 if level.strength > 60 else 3.0
            tp_pct = sl_pct * 2

        distance_pct = abs(current_price - entry) / current_price * 100
        if distance_pct > 10:
            continue

        orders.append({
            "price": entry,
            "kind": level.kind,
            "strength": level.strength,
            "touches": level.touches,
            "sl_pct": sl_pct,
            "tp_pct": tp_pct,
        })
        used_prices.add(level.price)

    return orders
