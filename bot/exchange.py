import logging
import ccxt
import pandas as pd

from bot.config import Config

logger = logging.getLogger(__name__)


class Exchange:
    """Wrapper around ccxt to fetch market data and place orders."""

    def __init__(self, config: Config):
        self.config = config
        exchange_class = getattr(ccxt, config.exchange_id)
        self.client = exchange_class({
            "apiKey": config.api_key,
            "secret": config.api_secret,
            "enableRateLimit": True,
        })

    def fetch_ohlcv(self, timeframe: str | None = None, lookback: int | None = None) -> pd.DataFrame:
        raw = self.client.fetch_ohlcv(
            self.config.symbol,
            timeframe=timeframe or self.config.timeframe,
            limit=lookback or self.config.lookback_candles,
        )
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    def get_ticker_price(self) -> float:
        ticker = self.client.fetch_ticker(self.config.symbol)
        return float(ticker["last"])

    def place_market_buy(self, amount_quote: float) -> dict:
        price = self.get_ticker_price()
        quantity = amount_quote / price

        if self.config.paper_trade:
            order = {
                "id": "paper-trade",
                "symbol": self.config.symbol,
                "side": "buy",
                "type": "market",
                "amount": quantity,
                "price": price,
                "cost": amount_quote,
                "status": "filled",
                "paper": True,
            }
            logger.info(f"[PAPER] Long {quantity:.6f} @ {price:.2f}")
            return order

        order = self.client.create_market_buy_order(self.config.symbol, quantity)
        logger.info(f"[LIVE] Long order placed: {order['id']}")
        return order

    def place_market_short(self, amount_quote: float) -> dict:
        price = self.get_ticker_price()
        quantity = amount_quote / price

        if self.config.paper_trade:
            order = {
                "id": "paper-trade",
                "symbol": self.config.symbol,
                "side": "sell",
                "type": "market",
                "amount": quantity,
                "price": price,
                "cost": amount_quote,
                "status": "filled",
                "paper": True,
            }
            logger.info(f"[PAPER] Short {quantity:.6f} @ {price:.2f}")
            return order

        order = self.client.create_market_sell_order(self.config.symbol, quantity)
        logger.info(f"[LIVE] Short order placed: {order['id']}")
        return order

    def place_market_close(self, quantity: float, side: str = "long") -> dict:
        price = self.get_ticker_price()

        if self.config.paper_trade:
            close_side = "sell" if side == "long" else "buy"
            order = {
                "id": "paper-trade",
                "symbol": self.config.symbol,
                "side": close_side,
                "type": "market",
                "amount": quantity,
                "price": price,
                "status": "filled",
                "paper": True,
            }
            logger.info(f"[PAPER] Close {side} {quantity:.6f} @ {price:.2f}")
            return order

        if side == "long":
            order = self.client.create_market_sell_order(self.config.symbol, quantity)
        else:
            order = self.client.create_market_buy_order(self.config.symbol, quantity)
        logger.info(f"[LIVE] Close {side} placed: {order['id']}")
        return order

    def place_limit_buy(self, price: float, amount_quote: float) -> dict:
        quantity = amount_quote / price

        if self.config.paper_trade:
            order = {
                "id": "paper-trade",
                "symbol": self.config.symbol,
                "side": "buy",
                "type": "limit",
                "amount": quantity,
                "price": price,
                "cost": amount_quote,
                "status": "open",
                "paper": True,
            }
            logger.info(f"[PAPER] Limit buy {quantity:.6f} @ {price:.2f}")
            return order

        order = self.client.create_limit_buy_order(self.config.symbol, quantity, price)
        logger.info(f"[LIVE] Limit buy placed: {order['id']} @ {price:.2f}")
        return order
