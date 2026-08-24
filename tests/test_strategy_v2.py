import pandas as pd

from bot.config import Config
from bot.levels import Level
from bot.strategy_v2 import (
    _candle_rejected_level,
    _structural_stop,
    _take_profit,
    compute_atr,
)


def test_structural_stop_is_not_leverage_derived():
    c = Config()
    c.v2_sl_atr_mult = 0.75
    c.v2_sl_zone_buffer_pct = 0.35
    c.v2_min_stop_distance_pct = 0.60
    c.v2_max_stop_distance_pct = 2.50

    stop = _structural_stop(entry=101.0, level=100.0, side="long", atr=1.0, config=c)
    assert stop is not None
    assert stop < 100.0
    # The stop is outside the support level, not a tiny 0.15% 10x-derived stop.
    assert (101.0 - stop) / 101.0 > 0.006


def test_long_confirmation_requires_rejection_close():
    c = Config()
    good = pd.Series({"open": 100.5, "high": 102.0, "low": 99.8, "close": 101.7})
    bad = pd.Series({"open": 100.5, "high": 100.8, "low": 98.0, "close": 98.5})

    assert _candle_rejected_level(good, level=100.0, side="long", atr=1.0, config=c)
    assert not _candle_rejected_level(bad, level=100.0, side="long", atr=1.0, config=c)


def test_short_confirmation_requires_rejection_close():
    c = Config()
    good = pd.Series({"open": 99.5, "high": 100.2, "low": 98.0, "close": 98.3})
    bad = pd.Series({"open": 99.5, "high": 102.0, "low": 99.2, "close": 101.5})

    assert _candle_rejected_level(good, level=100.0, side="short", atr=1.0, config=c)
    assert not _candle_rejected_level(bad, level=100.0, side="short", atr=1.0, config=c)


def test_tp_skips_trade_when_opposing_structure_is_too_close():
    c = Config()
    c.v2_target_risk_reward = 1.5
    entry = 100.0
    stop = 99.0
    levels = [
        Level(
            price=101.0,
            kind="resistance",
            touches=3,
            strength=80,
            volume_avg=1,
            last_touch_idx=1,
            timeframes=["1h", "4h"],
        )
    ]
    assert _take_profit(entry, stop, "long", levels, c) is None


def test_atr_uses_recent_true_ranges():
    rows = []
    price = 100.0
    for i in range(20):
        rows.append({
            "timestamp": pd.Timestamp("2026-01-01") + pd.Timedelta(minutes=30 * i),
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price + 0.2,
            "volume": 10,
        })
        price += 0.1
    df = pd.DataFrame(rows)
    atr = compute_atr(df, period=14)
    assert 1.9 <= atr <= 2.1
