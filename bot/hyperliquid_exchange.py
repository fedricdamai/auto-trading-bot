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

    def get_open_orders(self) -> list[dict]:
        """Return open orders for the configured symbol."""
        if self.config.paper_trade:
            return []
        try:
            orders = self.info.open_orders(self.address)
            return [o for o in orders if o.get("coin") == self.config.hl_symbol]
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    def get_position(self) -> dict | None:
        """Return current position for the configured symbol, or None."""
        try:
            state = self.info.user_state(self.address)
            for pos in state.get("assetPositions", []):
                item = pos.get("position", {})
                if item.get("coin") == self.config.hl_symbol:
                    szi = float(item.get("szi", "0"))
                    if szi != 0:
                        return {
                            "coin": item["coin"],
                            "size": abs(szi),
                            "side": "long" if szi > 0 else "short",
                            "entry_price": float(item.get("entryPx", "0")),
                            "unrealized_pnl": float(item.get("unrealizedPnl", "0")),
                        }
        except Exception as e:
            logger.error(f"Failed to get position: {e}")
        return None

    def _parse_order_result(self, result: dict, expected_sz: float, mid_price: float) -> tuple[float, float]:
        """Extract fill price and size from Hyperliquid order response."""
        try:
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if not statuses:
                raise ValueError(f"No order statuses in response: {result}")

            status = statuses[0]

            if "error" in status:
                raise RuntimeError(f"Order rejected: {status['error']}")

            if "filled" in status:
                fill = status["filled"]
                return float(fill.get("avgPx", mid_price)), float(fill.get("totalSz", expected_sz))

            if "resting" in status:
                return mid_price, expected_sz

        except (AttributeError, TypeError, KeyError):
            pass

        logger.warning(f"Could not parse fill from response, using mid price: {result}")
        return mid_price, expected_sz

    def _parse_limit_result(self, result: dict, price: float, quantity: float) -> dict:
        """Parse a limit order response, returning oid/status/fill info."""
        oid = None
        status = "open"
        fill_price = price
        fill_sz = quantity

        if isinstance(result, dict) and "response" in result:
            statuses = result["response"].get("data", {}).get("statuses", [])
            if statuses:
                if "resting" in statuses[0]:
                    oid = statuses[0]["resting"]["oid"]
                elif "filled" in statuses[0]:
                    fill = statuses[0]["filled"]
                    fill_price = float(fill.get("avgPx", price))
                    fill_sz = float(fill.get("totalSz", quantity))
                    status = "filled"
                elif "error" in statuses[0]:
                    raise RuntimeError(f"Order rejected: {statuses[0]['error']}")

        return {
            "oid": oid,
            "status": status,
            "fill_price": fill_price,
            "fill_sz": fill_sz,
        }

    def place_market_buy(self, amount_quote: float) -> dict:
        price = self.get_ticker_price()
        quantity = round(amount_quote / price, 4)

        if self.config.paper_trade:
            logger.info(f"[PAPER] Long {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return {
                "id": "paper-trade", "symbol": self.config.hl_symbol,
                "side": "buy", "type": "market", "amount": quantity,
                "price": price, "cost": amount_quote, "status": "filled", "paper": True,
            }

        result = self.exchange.market_open(
            name=self.config.hl_symbol, is_buy=True, sz=quantity, slippage=0.01,
        )
        fill_price, fill_sz = self._parse_order_result(result, quantity, price)
        logger.info(f"[LIVE] Market long filled: {fill_sz} @ {fill_price:.2f}")
        return {
            "id": str(result), "symbol": self.config.hl_symbol,
            "side": "buy", "type": "market", "amount": fill_sz,
            "price": fill_price, "cost": fill_sz * fill_price,
            "status": "filled", "paper": False, "raw": result,
        }

    def place_market_short(self, amount_quote: float) -> dict:
        price = self.get_ticker_price()
        quantity = round(amount_quote / price, 4)

        if self.config.paper_trade:
            logger.info(f"[PAPER] Short {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return {
                "id": "paper-trade", "symbol": self.config.hl_symbol,
                "side": "sell", "type": "market", "amount": quantity,
                "price": price, "cost": amount_quote, "status": "filled", "paper": True,
            }

        result = self.exchange.market_open(
            name=self.config.hl_symbol, is_buy=False, sz=quantity, slippage=0.01,
        )
        fill_price, fill_sz = self._parse_order_result(result, quantity, price)
        logger.info(f"[LIVE] Market short filled: {fill_sz} @ {fill_price:.2f}")
        return {
            "id": str(result), "symbol": self.config.hl_symbol,
            "side": "sell", "type": "market", "amount": fill_sz,
            "price": fill_price, "cost": fill_sz * fill_price,
            "status": "filled", "paper": False, "raw": result,
        }

    def place_market_close(self, quantity: float, side: str = "long") -> dict:
        price = self.get_ticker_price()

        if self.config.paper_trade:
            close_side = "sell" if side == "long" else "buy"
            logger.info(f"[PAPER] Close {side} {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return {
                "id": "paper-trade", "symbol": self.config.hl_symbol,
                "side": close_side, "type": "market", "amount": quantity,
                "price": price, "status": "filled", "paper": True,
            }

        result = self.exchange.market_close(
            coin=self.config.hl_symbol, sz=quantity, slippage=0.01,
        )
        fill_price, fill_sz = self._parse_order_result(result, quantity, price)
        logger.info(f"[LIVE] Market close {side} filled: {fill_sz} @ {fill_price:.2f}")
        return {
            "id": str(result), "symbol": self.config.hl_symbol,
            "side": "sell" if side == "long" else "buy", "type": "market",
            "amount": fill_sz, "price": fill_price,
            "status": "filled", "paper": False, "raw": result,
        }

    def place_limit_buy(self, price: float, amount_quote: float) -> dict:
        """Place a limit buy (long entry) order."""
        quantity = round(amount_quote / price, 4)

        if self.config.paper_trade:
            logger.info(f"[PAPER] Limit buy {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return {
                "id": f"paper-limit-{int(time.time())}", "oid": None,
                "symbol": self.config.hl_symbol, "side": "buy", "type": "limit",
                "amount": quantity, "price": price, "cost": amount_quote,
                "status": "open", "paper": True,
            }

        result = self.exchange.order(
            name=self.config.hl_symbol, is_buy=True, sz=quantity,
            limit_px=price, order_type={"limit": {"tif": "Gtc"}},
        )
        parsed = self._parse_limit_result(result, price, quantity)
        logger.info(f"[LIVE] Limit buy @ {price:.2f}: oid={parsed['oid']} status={parsed['status']}")
        return {
            "id": str(result), "oid": parsed["oid"],
            "symbol": self.config.hl_symbol, "side": "buy", "type": "limit",
            "amount": parsed["fill_sz"], "price": parsed["fill_price"],
            "cost": amount_quote, "status": parsed["status"],
            "paper": False, "raw": result,
        }

    def place_limit_sell(self, price: float, amount_quote: float) -> dict:
        """Place a limit sell (short entry) order."""
        quantity = round(amount_quote / price, 4)

        if self.config.paper_trade:
            logger.info(f"[PAPER] Limit sell {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return {
                "id": f"paper-limit-{int(time.time())}", "oid": None,
                "symbol": self.config.hl_symbol, "side": "sell", "type": "limit",
                "amount": quantity, "price": price, "cost": amount_quote,
                "status": "open", "paper": True,
            }

        result = self.exchange.order(
            name=self.config.hl_symbol, is_buy=False, sz=quantity,
            limit_px=price, order_type={"limit": {"tif": "Gtc"}},
        )
        parsed = self._parse_limit_result(result, price, quantity)
        logger.info(f"[LIVE] Limit sell @ {price:.2f}: oid={parsed['oid']} status={parsed['status']}")
        return {
            "id": str(result), "oid": parsed["oid"],
            "symbol": self.config.hl_symbol, "side": "sell", "type": "limit",
            "amount": parsed["fill_sz"], "price": parsed["fill_price"],
            "cost": amount_quote, "status": parsed["status"],
            "paper": False, "raw": result,
        }

    def cancel_order(self, price: float, oid: int) -> dict:
        if self.config.paper_trade:
            return {"status": "cancelled", "paper": True}

        result = self.exchange.cancel(name=self.config.hl_symbol, oid=int(oid))
        logger.info(f"[LIVE] Order cancelled: oid={oid}")
        return result

    def place_market_sell(self, quantity: float) -> dict:
        return self.place_market_close(quantity, side="long")

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        units = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
        return units.get(interval, 3_600_000)
