import numpy as np
import pandas as pd
import pytest

from bot.levels import (
    detect_levels, _find_swing_points,
    _cluster_levels, _analyze_level, compute_tp_sl, detect_doji,
)


def _make_candles(prices: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(prices)
    return pd.DataFrame({
        "open": prices,
        "high": [p + 1 for p in prices],
        "low": [p - 1 for p in prices],
        "close": prices,
        "volume": volumes if volumes else [100] * n,
    })


class TestFindSwingPoints:
    def test_detects_local_max(self):
        highs = np.array([10, 12, 15, 12, 10, 8, 6, 8, 10, 12, 14])
        lows = np.array([9, 11, 14, 11, 9, 7, 5, 7, 9, 11, 13])
        closes = np.array([9.5, 11.5, 14.5, 11.5, 9.5, 7.5, 5.5, 7.5, 9.5, 11.5, 13.5])
        points = _find_swing_points(highs, lows, closes, window=2)
        assert 15 in points

    def test_detects_local_min(self):
        highs = np.array([10, 8, 6, 8, 10, 12, 14, 12, 10, 8, 6])
        lows = np.array([9, 7, 5, 7, 9, 11, 13, 11, 9, 7, 5])
        closes = np.array([9.5, 7.5, 5.5, 7.5, 9.5, 11.5, 13.5, 11.5, 9.5, 7.5, 5.5])
        points = _find_swing_points(highs, lows, closes, window=2)
        assert 5 in points


class TestClusterLevels:
    def test_groups_nearby_prices(self):
        prices = [100.0, 100.3, 100.5, 110.0, 110.2]
        result = _cluster_levels(prices, tolerance=0.01)
        assert len(result) == 2

    def test_empty_input(self):
        assert _cluster_levels([], tolerance=0.01) == []

    def test_single_price(self):
        result = _cluster_levels([50.0], tolerance=0.01)
        assert result == [50.0]


class TestAnalyzeLevel:
    def test_counts_touches(self):
        highs = np.array([105, 102, 108, 101, 103])
        lows = np.array([95, 98, 96, 99, 97])
        closes = np.array([100, 100, 100, 100, 100])
        volumes = np.array([100, 200, 150, 100, 300])
        touches, vol, last, rejection = _analyze_level(100.0, highs, lows, closes, volumes, 0.02)
        assert touches == 5
        assert last == 4

    def test_no_touches_when_far(self):
        highs = np.array([200, 210, 205])
        lows = np.array([190, 195, 198])
        closes = np.array([195, 200, 200])
        volumes = np.array([100, 100, 100])
        touches, vol, last, rejection = _analyze_level(100.0, highs, lows, closes, volumes, 0.01)
        assert touches == 0


class TestDetectLevels:
    def test_returns_scored_levels(self):
        prices = (
            [100] * 10
            + list(range(100, 120))
            + [120] * 10
            + list(range(120, 100, -1))
            + [100] * 10
            + list(range(100, 115))
            + [115] * 10
            + list(range(115, 105, -1))
            + [105] * 10
        )
        df = _make_candles(prices)
        levels = detect_levels(df, tolerance_pct=1.0, min_touches=2)
        assert len(levels) > 0
        assert all(hasattr(l, "strength") for l in levels)
        assert levels[0].strength >= levels[-1].strength


class TestComputeTpSl:
    def test_long_1pct_target_3x_leverage(self):
        result = compute_tp_sl(entry=100000, leverage=3, target_pnl_pct=1.0, max_loss_pct=1.0, side="long")
        assert result["tp_price"] > 100000
        assert result["sl_price"] < 100000
        assert result["sl_price"] > result["liq_price"]
        expected_tp_move = 1.0 / 3
        assert abs(result["tp_move_pct"] - expected_tp_move) < 0.01

    def test_short_1pct_target_3x_leverage(self):
        result = compute_tp_sl(entry=100000, leverage=3, target_pnl_pct=1.0, max_loss_pct=1.0, side="short")
        assert result["tp_price"] < 100000
        assert result["sl_price"] > 100000
        assert result["sl_price"] < result["liq_price"]

    def test_sl_above_liquidation_long(self):
        result = compute_tp_sl(entry=100000, leverage=5, target_pnl_pct=1.0, max_loss_pct=50.0, side="long")
        assert result["sl_price"] > result["liq_price"]

    def test_sl_below_liquidation_short(self):
        result = compute_tp_sl(entry=100000, leverage=5, target_pnl_pct=1.0, max_loss_pct=50.0, side="short")
        assert result["sl_price"] < result["liq_price"]

    def test_higher_leverage_tighter_moves(self):
        r1 = compute_tp_sl(entry=100000, leverage=1, target_pnl_pct=1.0, max_loss_pct=1.0)
        r5 = compute_tp_sl(entry=100000, leverage=5, target_pnl_pct=1.0, max_loss_pct=1.0)
        assert r5["tp_move_pct"] < r1["tp_move_pct"]
        assert r5["sl_move_pct"] < r1["sl_move_pct"]

    def test_default_side_is_long(self):
        r_default = compute_tp_sl(entry=100000, leverage=3, target_pnl_pct=1.0, max_loss_pct=1.0)
        r_long = compute_tp_sl(entry=100000, leverage=3, target_pnl_pct=1.0, max_loss_pct=1.0, side="long")
        assert r_default == r_long


class TestDetectDoji:
    def test_no_doji_on_normal_candles(self):
        prices = list(range(100, 120))
        df = _make_candles(prices)
        assert detect_doji(df) is None

    def test_detects_doji_with_big_range(self):
        n = 10
        base = [100.0] * n
        df_data = {
            "open": base + [100.0, 100.0],
            "high": [p + 1 for p in base] + [106.0, 101.0],
            "low": [p - 1 for p in base] + [94.0, 99.0],
            "close": base + [100.1, 100.0],
            "volume": [100] * (n + 2),
        }
        df = pd.DataFrame(df_data)
        signal = detect_doji(df)
        assert signal is not None
        assert signal.signal in ("bullish", "bearish", "neutral")
        assert signal.strength > 0

    def test_too_few_candles(self):
        df = _make_candles([100, 101, 102])
        assert detect_doji(df) is None
