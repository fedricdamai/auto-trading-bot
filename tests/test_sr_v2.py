import pandas as pd
import pytest

from bot.config import Config
from bot.levels import Level
from bot.sr_v2 import (
    SR_BY_TIMEFRAME,
    _pivot_candidates,
    detect_timeframe_levels,
    merge_timeframe_levels,
)
from bot.strategy_v2 import Regime, _pick_trade_level, _take_profit


def _oscillating_df(timeframe="30m", periods=200):
    step = pd.Timedelta(minutes=30) if timeframe == "30m" else pd.Timedelta(hours=1)
    pattern = [105, 103, 101, 100, 101.5, 104, 107, 109, 110, 108, 106, 105]
    prices = (pattern * ((periods // len(pattern)) + 2))[:periods]
    rows = []
    start = pd.Timestamp("2026-01-01")
    for i, p in enumerate(prices):
        o = p + (0.2 if i % 2 == 0 else -0.2)
        c = p + (-0.1 if i % 3 == 0 else 0.1)
        high = max(o, c) + 0.6
        low = min(o, c) - 0.6
        if p == 100:
            low = 99.5
            o = 101.2
            c = 100.8
        if p == 110:
            high = 110.5
            o = 108.8
            c = 109.2
        rows.append({
            "timestamp": start + step * i,
            "open": o,
            "high": high,
            "low": low,
            "close": c,
            "volume": 100 + i % 10,
        })
    return pd.DataFrame(rows)


def _new_pivot_df(timeframe: str, right_candles: int, kind: str) -> pd.DataFrame:
    """Build a fresh pivot at index 8 with only N candles closed to its right."""
    step = pd.Timedelta(minutes=30) if timeframe == "30m" else pd.Timedelta(hours=1)
    pivot_idx = 8
    periods = pivot_idx + 1 + right_candles
    rows = []
    start = pd.Timestamp("2026-02-01")

    for i in range(periods):
        # Normal candles stay well away from the prospective pivot extreme.
        o = 100.0
        c = 100.2
        high = 101.0
        low = 99.0

        if i == pivot_idx:
            if kind == "support":
                low = 90.0
                o = 96.0
                c = 97.0
                high = 101.0
            else:
                high = 110.0
                o = 104.0
                c = 103.0
                low = 99.0

        rows.append({
            "timestamp": start + step * i,
            "open": o,
            "high": high,
            "low": low,
            "close": c,
            "volume": 100.0,
        })

    return pd.DataFrame(rows)


def test_timeframe_reactions_define_support_and_resistance():
    levels = detect_timeframe_levels(_oscillating_df("30m"), "30m")

    supports = [lv for lv in levels if lv.kind == "support"]
    resistances = [lv for lv in levels if lv.kind == "resistance"]

    assert supports
    assert resistances
    assert any(abs(lv.price - 99.5) < 0.5 and lv.touches >= 3 for lv in supports)
    assert any(abs(lv.price - 110.5) < 0.5 and lv.touches >= 3 for lv in resistances)


@pytest.mark.parametrize("timeframe", ["30m", "1h"])
@pytest.mark.parametrize("kind", ["support", "resistance"])
def test_new_pivot_requires_all_three_right_candles(timeframe, kind):
    """A fresh high/low is not a confirmed pivot until +1, +2 and +3 close."""
    params = SR_BY_TIMEFRAME[timeframe]
    assert params.pivot_left == 3
    assert params.pivot_right == 3

    pivot_idx = 8

    only_two_right = _new_pivot_df(timeframe, right_candles=2, kind=kind)
    candidates_before_confirmation = _pivot_candidates(only_two_right, params)
    assert not any(
        c["idx"] == pivot_idx and c["kind"] == kind
        for c in candidates_before_confirmation
    )

    three_right = _new_pivot_df(timeframe, right_candles=3, kind=kind)
    candidates_after_confirmation = _pivot_candidates(three_right, params)
    assert any(
        c["idx"] == pivot_idx and c["kind"] == kind
        for c in candidates_after_confirmation
    )


def test_level_type_is_not_relabelled_from_current_price():
    df = _oscillating_df("1h")
    # Finish above the old resistance. The historical repeated high is still a
    # resistance level until a separate role-reversal pattern proves otherwise.
    df.loc[df.index[-1], "close"] = 112.0
    df.loc[df.index[-1], "high"] = 112.5

    levels = detect_timeframe_levels(df, "1h")
    assert any(lv.kind == "resistance" and abs(lv.price - 110.5) < 0.7 for lv in levels)


def test_cross_timeframe_merge_never_mixes_support_and_resistance():
    raw = [
        Level(100.0, "support", 3, 80, 1, 10, ["30m"]),
        Level(100.1, "resistance", 3, 85, 1, 12, ["1h"]),
    ]
    merged = merge_timeframe_levels(raw, {"30m": 2.0, "1h": 3.0})

    assert len(merged) == 2
    assert {lv.kind for lv in merged} == {"support", "resistance"}


def test_near_resistance_can_create_short_even_in_bullish_regime():
    c = Config()
    c.v2_min_level_strength = 65
    c.v2_max_level_distance_pct = 3.0
    levels = [
        Level(100.0, "support", 3, 82, 1, 10, ["1h"]),
        Level(110.0, "resistance", 3, 88, 1, 20, ["1h"]),
    ]

    picked = _pick_trade_level(
        levels,
        current_price=109.0,
        regime=Regime("bullish", 100.0, {}),
        config=c,
    )

    assert picked is not None
    level, side = picked
    assert side == "short"
    assert level.kind == "resistance"
    assert level.price == 110.0


def test_short_tp_targets_next_support_zone():
    c = Config()
    c.v2_target_risk_reward = 1.5
    c.v2_tp_zone_buffer_pct = 0.15
    levels = [
        Level(100.0, "support", 3, 85, 1, 10, ["1h"]),
        Level(110.0, "resistance", 3, 90, 1, 20, ["1h"]),
    ]

    tp = _take_profit(
        entry=110.0,
        stop=112.0,
        side="short",
        levels=levels,
        config=c,
        atr_by_tf={"1h": 3.0},
    )

    assert tp is not None
    assert 100.0 < tp < 101.0
