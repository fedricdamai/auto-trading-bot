import logging
import time
import pandas as pd
from eth_account import Account

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.utils import constants

from bot.config import Config

logger = logging.getLogger(__name__)


class HyperliquidExchange:
    """Hyperliquid perpetual futures exchange adapter."""

    def __init__(self, config: Config):
        self.config = config
        self.address = config.hl_wallet_address
        base_url = constants.MAINNET_API_URL if config.hl_mainnet else constants.TESTNET_API_URL

        self.info = Info(base_url, skip_ws=True)

        if config.paper_trade:
            self.exchange = None
            logger.info("Paper trade mode — no exchange client initialized")
        else:
            account = Account.from_key(config.hl_private_key)
            self.exchange = HLExchange(account, base_url)
            self._set_leverage()

    def _set_leverage(self):
        if self.exchange is None:
            return
        try:
            self.exchange.update_leverage(
                leverage=self.config.hl_leverage,
                name=self.config.hl_symbol,
                is_cross=True,
            )
            logger.info(f"Leverage set to {self.config.hl_leverage}x on {self.config.hl_symbol}")
        except Exception as e:
            logger.error(f"Failed to set leverage: {e}")

    def fetch_ohlcv(self, timeframe: str | None = None, lookback: int | None = None) -> pd.DataFrame:
        interval = timeframe or self.config.timeframe
        candles = lookback or self.config.lookback_candles
        now_ms = int(time.time() * 1000)
        interval_ms = self._interval_to_ms(interval)
        start_ms = now_ms - (candles * interval_ms)

        raw = self.info.candles_snapshot(
            name=self.config.hl_symbol,
            interval=interval,
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
            raise ValueError(f"No candle data returned for {self.config.hl_symbol}")
        return df

    def get_ticker_price(self) -> float:
        mids = self.info.all_mids()
        price = mids.get(self.config.hl_symbol)
        if price is None:
            raise ValueError(f"No price found for {self.config.hl_symbol}")
        return float(price)

    def get_account_state(self) -> dict:
        return self.info.user_state(self.address)

    def place_market_buy(self, amount_quote: float) -> dict:
        price = self.get_ticker_price()
        quantity = round(amount_quote / price, 4)

        if self.config.paper_trade:
            order = {
                "id": "paper-trade",
                "symbol": self.config.hl_symbol,
                "side": "buy",
                "type": "market",
                "amount": quantity,
                "price": price,
                "cost": amount_quote,
                "status": "filled",
                "paper": True,
            }
            logger.info(f"[PAPER] Long {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return order

        result = self.exchange.market_open(
            name=self.config.hl_symbol,
            is_buy=True,
            sz=quantity,
            slippage=0.01,
        )
        logger.info(f"[LIVE] Market long placed: {result}")
        return {
            "id": str(result),
            "symbol": self.config.hl_symbol,
            "side": "buy",
            "type": "market",
            "amount": quantity,
            "price": price,
            "cost": amount_quote,
            "status": "filled",
            "paper": False,
            "raw": result,
        }

    def place_market_short(self, amount_quote: float) -> dict:
        price = self.get_ticker_price()
        quantity = round(amount_quote / price, 4)

        if self.config.paper_trade:
            order = {
                "id": "paper-trade",
                "symbol": self.config.hl_symbol,
                "side": "sell",
                "type": "market",
                "amount": quantity,
                "price": price,
                "cost": amount_quote,
                "status": "filled",
                "paper": True,
            }
            logger.info(f"[PAPER] Short {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return order

        result = self.exchange.market_open(
            name=self.config.hl_symbol,
            is_buy=False,
            sz=quantity,
            slippage=0.01,
        )
        logger.info(f"[LIVE] Market short placed: {result}")
        return {
            "id": str(result),
            "symbol": self.config.hl_symbol,
            "side": "sell",
            "type": "market",
            "amount": quantity,
            "price": price,
            "cost": amount_quote,
            "status": "filled",
            "paper": False,
            "raw": result,
        }

    def place_market_close(self, quantity: float, side: str = "long") -> dict:
        price = self.get_ticker_price()

        if self.config.paper_trade:
            close_side = "sell" if side == "long" else "buy"
            order = {
                "id": "paper-trade",
                "symbol": self.config.hl_symbol,
                "side": close_side,
                "type": "market",
                "amount": quantity,
                "price": price,
                "status": "filled",
                "paper": True,
            }
            logger.info(f"[PAPER] Close {side} {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return order

        result = self.exchange.market_close(
            coin=self.config.hl_symbol,
            sz=quantity,
            slippage=0.01,
        )
        logger.info(f"[LIVE] Market close {side} placed: {result}")
        return {
            "id": str(result),
            "symbol": self.config.hl_symbol,
            "side": "sell" if side == "long" else "buy",
            "type": "market",
            "amount": quantity,
            "price": price,
            "status": "filled",
            "paper": False,
            "raw": result,
        }

    def place_limit_buy(self, price: float, amount_quote: float) -> dict:
        quantity = round(amount_quote / price, 4)

        if self.config.paper_trade:
            order = {
                "id": f"paper-limit-{int(time.time())}",
                "oid": None,
                "symbol": self.config.hl_symbol,
                "side": "buy",
                "type": "limit",
                "amount": quantity,
                "price": price,
                "cost": amount_quote,
                "status": "open",
                "paper": True,
            }
            logger.info(f"[PAPER] Limit buy {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return order

        result = self.exchange.order(
            name=self.config.hl_symbol,
            is_buy=True,
            sz=quantity,
            limit_px=price,
            order_type={"limit": {"tif": "Gtc"}},
        )
        oid = None
        if isinstance(result, dict) and "response" in result:
            statuses = result["response"].get("data", {}).get("statuses", [])
            if statuses and "resting" in statuses[0]:
                oid = statuses[0]["resting"]["oid"]

        logger.info(f"[LIVE] Limit buy placed @ {price:.2f}: oid={oid}")
        return {
            "id": str(result),
            "oid": oid,
            "symbol": self.config.hl_symbol,
            "side": "buy",
            "type": "limit",
            "amount": quantity,
            "price": price,
            "cost": amount_quote,
            "status": "open",
            "paper": False,
            "raw": result,
        }

    def cancel_order(self, price: float, oid: int) -> dict:
        if self.config.paper_trade:
            return {"status": "cancelled", "paper": True}

        result = self.exchange.cancel(name=self.config.hl_symbol, oid=oid)
        logger.info(f"[LIVE] Order cancelled: oid={oid}")
        return result

    # Keep backward compat
    def place_market_sell(self, quantity: float) -> dict:
        return self.place_market_close(quantity, side="long")

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        units = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
        return units.get(interval, 3_600_000)
