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

    def fetch_ohlcv(self) -> pd.DataFrame:
        """Fetch OHLCV candles and return as a DataFrame."""
        raw = self.client.fetch_ohlcv(
            self.config.symbol,
            timeframe=self.config.timeframe,
            limit=self.config.lookback_candles,
        )
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    def get_ticker_price(self) -> float:
        ticker = self.client.fetch_ticker(self.config.symbol)
        return float(ticker["last"])

    def place_market_buy(self, amount_quote: float) -> dict:
        """Place a market buy order for a given quote-currency amount."""
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
            logger.info(f"[PAPER] Buy {quantity:.6f} @ {price:.2f}")
            return order

        order = self.client.create_market_buy_order(
            self.config.symbol,
            quantity,
        )
        logger.info(f"[LIVE] Buy order placed: {order['id']}")
        return order

    def place_limit_buy(self, price: float, amount_quote: float) -> dict:
        """Place a limit buy order at a specific price."""
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

        order = self.client.create_limit_buy_order(
            self.config.symbol,
            quantity,
            price,
        )
        logger.info(f"[LIVE] Limit buy order placed: {order['id']} @ {price:.2f}")
        return order
