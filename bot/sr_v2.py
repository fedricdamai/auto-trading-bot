"""Reaction-based support/resistance detection for Trader V2.

The detector is deliberately timeframe-native:
- 30m levels are built only from 30m candle reactions.
- 1h levels are built only from 1h candle reactions.
- Support comes from repeated swing-low / lower-rejection reactions.
- Resistance comes from repeated swing-high / upper-rejection reactions.
- A level never changes type merely because current price moved above/below it.
- Cross-timeframe confluence merges only levels of the SAME type.

The goal is to match discretionary chart reading: repeated defended lows create
support, repeated rejected highs create resistance.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from bot.levels import Level


@dataclass(frozen=True)
class SRParams:
    lookback: int
    pivot_left: int
    pivot_right: int
    zone_half_width_atr: float
    min_valid_touches: int
    strong_touches: int
    min_reaction_atr: float
    reaction_window: int
    touch_separation: int
    timeframe_weight: float


# These are the deterministic parameters agreed for the first reaction-based
# implementation. They live in source control, not .env.
SR_BY_TIMEFRAME: dict[str, SRParams] = {
    "30m": SRParams(
        lookback=200,
        pivot_left=3,
        pivot_right=3,
        zone_half_width_atr=0.35,
        min_valid_touches=2,
        strong_touches=3,
        min_reaction_atr=0.60,
        reaction_window=5,
        touch_separation=3,
        timeframe_weight=1.00,
    ),
    "1h": SRParams(
        lookback=200,
        pivot_left=3,
        pivot_right=3,
        zone_half_width_atr=0.40,
        min_valid_touches=2,
        strong_touches=3,
        min_reaction_atr=0.75,
        reaction_window=4,
        touch_separation=2,
        timeframe_weight=1.35,
    ),
}


def true_range(df: pd.DataFrame) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.rolling(period, min_periods=max(3, period // 2)).mean()


def latest_atr(df: pd.DataFrame, period: int = 14) -> float:
    if df is None or len(df) < 3:
        return 0.0
    s = atr_series(df, period).dropna()
    if s.empty:
        return 0.0
    value = float(s.iloc[-1])
    return value if math.isfinite(value) and value > 0 else 0.0


def _pivot_candidates(df: pd.DataFrame, params: SRParams) -> list[dict]:
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    atrs = atr_series(df).to_numpy()
    n = len(df)
    out: list[dict] = []

    for i in range(params.pivot_left, n - params.pivot_right):
        atr = float(atrs[i]) if i < len(atrs) and math.isfinite(float(atrs[i])) else 0.0
        if atr <= 0:
            continue

        lo_window = lows[i - params.pivot_left : i + params.pivot_right + 1]
        hi_window = highs[i - params.pivot_left : i + params.pivot_right + 1]
        neighbors_low = np.delete(lo_window, params.pivot_left)
        neighbors_high = np.delete(hi_window, params.pivot_left)

        if lows[i] < float(np.min(neighbors_low)):
            out.append({"kind": "support", "price": float(lows[i]), "idx": i, "atr": atr})
        if highs[i] > float(np.max(neighbors_high)):
            out.append({"kind": "resistance", "price": float(highs[i]), "idx": i, "atr": atr})

    return out


def _cluster_candidates(candidates: list[dict], params: SRParams) -> list[list[dict]]:
    """Cluster same-kind pivots by an ATR-scaled price distance."""
    clusters: list[list[dict]] = []
    for item in sorted(candidates, key=lambda x: (x["kind"], x["price"])):
        matched = None
        for cluster in clusters:
            if cluster[0]["kind"] != item["kind"]:
                continue
            center = float(np.median([x["price"] for x in cluster]))
            cluster_atr = float(np.median([x["atr"] for x in cluster]))
            tol = params.zone_half_width_atr * max(cluster_atr, item["atr"])
            if abs(item["price"] - center) <= tol:
                matched = cluster
                break
        if matched is None:
            clusters.append([item])
        else:
            matched.append(item)
    return clusters


def _wick_ratio(row: pd.Series, kind: str) -> float:
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])
    rng = max(h - l, 1e-12)
    if kind == "support":
        wick = min(o, c) - l
    else:
        wick = h - max(o, c)
    return max(0.0, wick / rng)


def _reaction_atr(df: pd.DataFrame, idx: int, kind: str, atr: float, window: int) -> float:
    if atr <= 0:
        return 0.0
    end = min(len(df), idx + 1 + window)
    future = df.iloc[idx + 1 : end]
    if future.empty:
        return 0.0

    if kind == "support":
        touch = float(df.iloc[idx]["low"])
        move = float(future["high"].astype(float).max()) - touch
    else:
        touch = float(df.iloc[idx]["high"])
        move = touch - float(future["low"].astype(float).min())
    return max(0.0, move / atr)


def _approach_ok(df: pd.DataFrame, idx: int, center: float, half_width: float, kind: str) -> bool:
    if idx <= 0:
        return False
    prev_close = float(df.iloc[idx - 1]["close"])
    if kind == "support":
        return prev_close >= center - half_width
    return prev_close <= center + half_width


def _analyze_cluster(
    df: pd.DataFrame,
    cluster: list[dict],
    timeframe: str,
    params: SRParams,
) -> Level | None:
    kind = cluster[0]["kind"]
    center = float(np.median([x["price"] for x in cluster]))
    n = len(df)

    valid: list[dict] = []
    last_accepted_idx = -10_000

    for item in sorted(cluster, key=lambda x: x["idx"]):
        idx = int(item["idx"])
        atr = float(item["atr"])
        half_width = params.zone_half_width_atr * atr

        if idx - last_accepted_idx < params.touch_separation:
            continue
        if not _approach_ok(df, idx, center, half_width, kind):
            continue

        reaction = _reaction_atr(df, idx, kind, atr, params.reaction_window)
        if reaction < params.min_reaction_atr:
            continue

        row = df.iloc[idx]
        close = float(row["close"])
        if kind == "support" and close < center - half_width:
            continue
        if kind == "resistance" and close > center + half_width:
            continue

        valid.append(
            {
                "idx": idx,
                "reaction": reaction,
                "wick": _wick_ratio(row, kind),
                "volume": float(row.get("volume", 0.0)),
            }
        )
        last_accepted_idx = idx

    if len(valid) < params.min_valid_touches:
        return None

    touches = len(valid)
    avg_reaction = float(np.mean([x["reaction"] for x in valid]))
    avg_wick = float(np.mean([x["wick"] for x in valid]))
    avg_volume = float(np.mean([x["volume"] for x in valid]))
    last_touch = max(x["idx"] for x in valid)

    recency = max(0.0, 1.0 - (n - 1 - last_touch) / max(params.lookback, 1))
    touch_score = min(touches / max(params.strong_touches + 1, 1), 1.0) * 40.0
    reaction_score = min(avg_reaction / 1.50, 1.0) * 30.0
    recency_score = recency * 15.0
    wick_score = min(avg_wick / 0.45, 1.0) * 15.0
    strength = min(100.0, touch_score + reaction_score + recency_score + wick_score)

    return Level(
        price=round(center, 8),
        kind=kind,
        touches=touches,
        strength=round(strength, 1),
        volume_avg=round(avg_volume, 4),
        last_touch_idx=last_touch,
        timeframes=[timeframe],
        fib_ratio=None,
    )


def detect_timeframe_levels(df: pd.DataFrame, timeframe: str) -> list[Level]:
    """Detect support/resistance from reactions on one exact timeframe."""
    if timeframe not in SR_BY_TIMEFRAME:
        raise ValueError(f"Unsupported S/R timeframe: {timeframe}")
    params = SR_BY_TIMEFRAME[timeframe]
    if df is None or df.empty:
        return []

    data = df.tail(params.lookback).reset_index(drop=True)
    minimum = params.pivot_left + params.pivot_right + params.reaction_window + 5
    if len(data) < minimum:
        return []

    candidates = _pivot_candidates(data, params)
    clusters = _cluster_candidates(candidates, params)
    levels: list[Level] = []
    for cluster in clusters:
        lv = _analyze_cluster(data, cluster, timeframe, params)
        if lv is not None:
            levels.append(lv)

    levels.sort(key=lambda x: (x.strength, x.touches), reverse=True)
    return levels


def merge_timeframe_levels(
    raw_levels: list[Level],
    atr_by_tf: dict[str, float],
) -> list[Level]:
    """Merge only same-type 30m/1h zones that materially overlap."""
    groups: list[list[Level]] = []

    def width_for(level: Level) -> float:
        widths = []
        for tf in level.timeframes or []:
            p = SR_BY_TIMEFRAME.get(tf)
            atr = atr_by_tf.get(tf, 0.0)
            if p and atr > 0:
                widths.append(p.zone_half_width_atr * atr)
        return max(widths, default=max(abs(level.price) * 0.0025, 1e-8))

    for lv in sorted(raw_levels, key=lambda x: (x.kind, x.price)):
        matched = None
        for group in groups:
            if group[0].kind != lv.kind:
                continue
            group_center = float(np.average(
                [g.price for g in group],
                weights=[max(g.strength, 1.0) for g in group],
            ))
            group_width = max(width_for(g) for g in group)
            if abs(lv.price - group_center) <= max(group_width, width_for(lv)):
                matched = group
                break
        if matched is None:
            groups.append([lv])
        else:
            matched.append(lv)

    merged: list[Level] = []
    for group in groups:
        weights = []
        for lv in group:
            tf_weight = max(
                (SR_BY_TIMEFRAME.get(tf, SR_BY_TIMEFRAME["30m"]).timeframe_weight for tf in (lv.timeframes or ["30m"])),
                default=1.0,
            )
            weights.append(max(lv.strength, 1.0) * tf_weight)

        price = float(np.average([lv.price for lv in group], weights=weights))
        tfs = sorted({tf for lv in group for tf in (lv.timeframes or [])}, reverse=True)
        base = max(lv.strength for lv in group)
        confluence_bonus = 12.0 if len(tfs) > 1 else 0.0
        strength = min(100.0, base + confluence_bonus)
        touches = sum(lv.touches for lv in group)
        volume = max((lv.volume_avg for lv in group), default=0.0)
        last_touch = max((lv.last_touch_idx for lv in group), default=0)

        merged.append(
            Level(
                price=round(price, 8),
                kind=group[0].kind,
                touches=touches,
                strength=round(strength, 1),
                volume_avg=round(float(volume), 4),
                last_touch_idx=last_touch,
                timeframes=tfs,
                fib_ratio=None,
            )
        )

    merged.sort(key=lambda x: (x.strength, x.touches), reverse=True)
    return merged


def zone_half_width(level: Level, atr_by_tf: dict[str, float]) -> float:
    widths = []
    for tf in level.timeframes or []:
        params = SR_BY_TIMEFRAME.get(tf)
        atr = atr_by_tf.get(tf, 0.0)
        if params and atr > 0:
            widths.append(params.zone_half_width_atr * atr)
    if widths:
        return max(widths)
    return max(abs(level.price) * 0.0025, 1e-8)
