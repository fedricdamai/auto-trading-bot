import numpy as np
import pandas as pd
import pytest

from bot.levels import detect_levels, get_limit_order_prices, _find_swing_points, _cluster_levels, _analyze_level


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


class TestGetLimitOrderPrices:
    def test_returns_order_targets(self):
        from bot.levels import Level
        levels = [
            Level(price=60000, kind="support", touches=5, strength=80, volume_avg=1000, last_touch_idx=190),
            Level(price=65000, kind="resistance", touches=3, strength=60, volume_avg=800, last_touch_idx=195),
        ]
        orders = get_limit_order_prices(levels, current_price=62000, leverage=3, target_pnl_pct=1.0, max_loss_pct=1.0, max_orders=5)
        assert len(orders) > 0
        assert all("sl_price" in o and "tp_price" in o for o in orders)
        for o in orders:
            assert o["tp_price"] > o["price"]
            assert o["sl_price"] < o["price"]
            assert o["sl_price"] > o["liq_price"]


class TestComputeTpSl:
    def test_1pct_target_3x_leverage(self):
        from bot.levels import compute_tp_sl
        result = compute_tp_sl(entry=100000, leverage=3, target_pnl_pct=1.0, max_loss_pct=1.0)
        assert result["tp_price"] > 100000
        assert result["sl_price"] < 100000
        assert result["sl_price"] > result["liq_price"]
        expected_tp_move = 1.0 / 3  # ~0.333%
        assert abs(result["tp_move_pct"] - expected_tp_move) < 0.01

    def test_sl_above_liquidation(self):
        from bot.levels import compute_tp_sl
        result = compute_tp_sl(entry=100000, leverage=5, target_pnl_pct=1.0, max_loss_pct=50.0)
        assert result["sl_price"] > result["liq_price"]

    def test_higher_leverage_tighter_moves(self):
        from bot.levels import compute_tp_sl
        r1 = compute_tp_sl(entry=100000, leverage=1, target_pnl_pct=1.0, max_loss_pct=1.0)
        r5 = compute_tp_sl(entry=100000, leverage=5, target_pnl_pct=1.0, max_loss_pct=1.0)
        assert r5["tp_move_pct"] < r1["tp_move_pct"]
        assert r5["sl_move_pct"] < r1["sl_move_pct"]
