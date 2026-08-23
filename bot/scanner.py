import logging
import time
import pandas as pd
import numpy as np
from dataclasses import dataclass

from bot.levels import (
    detect_levels_multi_tf, detect_levels, compute_trend_bias,
    check_breakout_fakeout, Level, TrendBias, BreakoutCheck,
)

logger = logging.getLogger(__name__)


@dataclass
class SymbolOpportunity:
    symbol: str
    level: Level
    effective_strength: float
    trend: TrendBias
    breakout: BreakoutCheck
    current_price: float
    score: float
    distance_pct: float
    volume_24h: float = 0.0


class SymbolExchangeProxy:
    """Lightweight exchange proxy for scanning a single symbol's data.

    Shares the Info connection with the main exchange but targets
    a specific symbol for candle/price fetching.
    """

    def __init__(self, info, symbol: str):
        self.info = info
        self.symbol = symbol

    def fetch_ohlcv(self, timeframe: str = "5m", lookback: int = 200) -> pd.DataFrame:
        now_ms = int(time.time() * 1000)
        interval_ms = self._interval_to_ms(timeframe)
        start_ms = now_ms - (lookback * interval_ms)

        raw = self.info.candles_snapshot(
            name=self.symbol,
            interval=timeframe,
            startTime=start_ms,
            endTime=now_ms,
        )

        rows = []
        for c in raw:
            rows.append({
                "timestamp": pd.to_datetime(c["t"], unit="ms"),
                "open": float(c["o"]),
                "high": float(c["h"]),
                "low": float(c["l"]),
                "close": float(c["c"]),
                "volume": float(c["v"]),
            })

        df = pd.DataFrame(rows)
        if df.empty:
            raise ValueError(f"No candle data for {self.symbol}")
        return df

    def get_ticker_price(self) -> float:
        mids = self.info.all_mids()
        price = mids.get(self.symbol)
        if price is None:
            raise ValueError(f"No price for {self.symbol}")
        return float(price)

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        units = {
            "1m": 60_000, "5m": 300_000, "15m": 900_000,
            "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
        }
        return units.get(interval, 3_600_000)


class MarketScanner:
    """Scans multiple Hyperliquid perpetual markets and ranks opportunities."""

    def __init__(self, info, config):
        self.info = info
        self.config = config
        self._symbols_cache: list[str] = []
        self._cache_time: float = 0

    def get_top_symbols(self, n: int = 10) -> list[str]:
        if self._symbols_cache and time.time() - self._cache_time < 3600:
            return self._symbols_cache[:n]

        try:
            meta = self.info.meta()
            universe = meta.get("universe", [])
            symbols = [a["name"] for a in universe]

            mids = self.info.all_mids()
            volumes = {}
            for sym in symbols:
                try:
                    proxy = SymbolExchangeProxy(self.info, sym)
                    df = proxy.fetch_ohlcv(timeframe="1d", lookback=1)
                    if not df.empty:
                        price = float(mids.get(sym, 0))
                        vol = df.iloc[-1]["volume"] * price
                        volumes[sym] = vol
                except Exception:
                    continue

            sorted_syms = sorted(volumes.keys(), key=lambda s: volumes[s], reverse=True)
            self._symbols_cache = sorted_syms
            self._cache_time = time.time()

            logger.info(f"Top {n} by volume: {sorted_syms[:n]}")
            return sorted_syms[:n]

        except Exception as e:
            logger.error(f"Failed to get top symbols: {e}")
            return self._symbols_cache[:n] if self._symbols_cache else ["BTC", "ETH", "SOL"]

    def scan_symbol(self, symbol: str) -> SymbolOpportunity | None:
        proxy = SymbolExchangeProxy(self.info, symbol)

        try:
            current_price = proxy.get_ticker_price()
        except Exception as e:
            logger.debug(f"Skip {symbol}: no price ({e})")
            return None

        try:
            df = proxy.fetch_ohlcv(timeframe="5m", lookback=500)
            levels = detect_levels(
                df,
                tolerance_pct=self.config.level_tolerance_pct,
                min_touches=self.config.min_touches,
            )
        except Exception as e:
            logger.debug(f"Skip {symbol}: level detection failed ({e})")
            return None

        if not levels:
            return None

        try:
            trend = compute_trend_bias(proxy)
        except Exception:
            trend = TrendBias(direction="neutral", confidence=0)

        best_level = None
        best_strength = 0.0

        for level in levels:
            if trend.confidence >= 55:
                if trend.direction == "bullish" and level.kind == "resistance":
                    continue
                if trend.direction == "bearish" and level.kind == "support":
                    continue

            distance_pct = abs(current_price - level.price) / level.price * 100
            if distance_pct > 10 or distance_pct < 0.3:
                continue

            proximity = 1.0 - (distance_pct / 15.0)
            eff = level.strength * proximity
            if eff > best_strength:
                best_strength = eff
                best_level = level

        if not best_level:
            return None

        try:
            bo_check = check_breakout_fakeout(proxy, best_level, current_price)
        except Exception:
            bo_check = BreakoutCheck(
                is_breakout=False, is_fakeout=False, confidence=0,
                volume_confirmed=False, momentum_confirmed=False,
                trend_aligned=False, retest_seen=False,
            )

        if bo_check.is_fakeout:
            return None

        distance_pct = abs(current_price - best_level.price) / best_level.price * 100

        score = best_strength
        if trend.confidence >= 55 and trend.direction != "neutral":
            score *= 1.2
        if bo_check.volume_confirmed:
            score *= 1.15
        if bo_check.momentum_confirmed:
            score *= 1.1
        if best_level.fib_ratio:
            score *= 1.05

        return SymbolOpportunity(
            symbol=symbol,
            level=best_level,
            effective_strength=round(best_strength, 1),
            trend=trend,
            breakout=bo_check,
            current_price=current_price,
            score=round(score, 1),
            distance_pct=round(distance_pct, 2),
        )

    def scan_all(self, symbols: list[str] | None = None, top_n: int = 10) -> list[SymbolOpportunity]:
        if symbols is None:
            symbols = self.get_top_symbols(top_n)

        opportunities = []
        for sym in symbols:
            try:
                opp = self.scan_symbol(sym)
                if opp:
                    opportunities.append(opp)
                    logger.info(
                        f"  {sym}: score={opp.score:.1f} | {opp.level.kind} @ {opp.level.price:.2f} | "
                        f"trend={opp.trend.direction} ({opp.trend.confidence:.0f}%) | "
                        f"dist={opp.distance_pct:.2f}%"
                    )
            except Exception as e:
                logger.debug(f"Scan failed for {sym}: {e}")
                continue

        opportunities.sort(key=lambda o: o.score, reverse=True)

        if opportunities:
            best = opportunities[0]
            logger.info(
                f"Best opportunity: {best.symbol} | score={best.score:.1f} | "
                f"{best.level.kind} @ {best.level.price:.2f} ({best.trend.direction})"
            )

        return opportunities
