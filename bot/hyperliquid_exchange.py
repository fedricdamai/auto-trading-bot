import logging
import math
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
        self._sz_decimals = self._get_sz_decimals()

        if config.paper_trade:
            self.exchange = None
            logger.info("Paper trade mode — no exchange client initialized")
        else:
            account = Account.from_key(config.hl_private_key)
            self.exchange = HLExchange(account, base_url)
            self._set_leverage()

    def _get_sz_decimals(self) -> int:
        """Get size decimal places for the configured symbol from exchange metadata."""
        try:
            meta = self.info.meta()
            for asset in meta.get("universe", []):
                if asset.get("name") == self.config.hl_symbol:
                    sd = asset.get("szDecimals", 5)
                    logger.info(f"{self.config.hl_symbol} szDecimals={sd}")
                    return sd
        except Exception as e:
            logger.warning(f"Failed to get szDecimals, defaulting to 5: {e}")
        return 5

    @staticmethod
    def _round_price(price: float, sig_figs: int = 5) -> float:
        """Round price to N significant figures (Hyperliquid requires <= 5)."""
        if price == 0:
            return 0.0
        d = sig_figs - 1 - int(math.floor(math.log10(abs(price))))
        return round(price, d)

    def _round_size(self, size: float) -> float:
        """Round size to the exchange's szDecimals."""
        return round(size, self._sz_decimals)

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
        quantity = self._round_size(amount_quote / price)

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
        quantity = self._round_size(amount_quote / price)

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
        quantity = self._round_size(quantity)

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
        price = self._round_price(price)
        quantity = self._round_size(amount_quote / price)

        if self.config.paper_trade:
            logger.info(f"[PAPER] Limit buy {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return {
                "id": f"paper-limit-{int(time.time())}", "oid": None,
                "symbol": self.config.hl_symbol, "side": "buy", "type": "limit",
                "amount": quantity, "price": price, "cost": amount_quote,
                "status": "open", "paper": True,
            }

        logger.info(f"[LIVE] Submitting limit buy: price={price} sz={quantity} (szDec={self._sz_decimals})")
        result = self.exchange.order(
            name=self.config.hl_symbol, is_buy=True, sz=quantity,
            limit_px=price, order_type={"limit": {"tif": "Gtc"}},
        )
        logger.debug(f"[LIVE] Limit buy raw response: {result}")
        parsed = self._parse_limit_result(result, price, quantity)
        logger.info(f"[LIVE] Limit buy @ {price}: oid={parsed['oid']} status={parsed['status']}")
        return {
            "id": str(result), "oid": parsed["oid"],
            "symbol": self.config.hl_symbol, "side": "buy", "type": "limit",
            "amount": parsed["fill_sz"], "price": parsed["fill_price"],
            "cost": amount_quote, "status": parsed["status"],
            "paper": False, "raw": result,
        }

    def place_limit_sell(self, price: float, amount_quote: float) -> dict:
        """Place a limit sell (short entry) order."""
        price = self._round_price(price)
        quantity = self._round_size(amount_quote / price)

        if self.config.paper_trade:
            logger.info(f"[PAPER] Limit sell {quantity} {self.config.hl_symbol} @ {price:.2f}")
            return {
                "id": f"paper-limit-{int(time.time())}", "oid": None,
                "symbol": self.config.hl_symbol, "side": "sell", "type": "limit",
                "amount": quantity, "price": price, "cost": amount_quote,
                "status": "open", "paper": True,
            }

        logger.info(f"[LIVE] Submitting limit sell: price={price} sz={quantity} (szDec={self._sz_decimals})")
        result = self.exchange.order(
            name=self.config.hl_symbol, is_buy=False, sz=quantity,
            limit_px=price, order_type={"limit": {"tif": "Gtc"}},
        )
        logger.debug(f"[LIVE] Limit sell raw response: {result}")
        parsed = self._parse_limit_result(result, price, quantity)
        logger.info(f"[LIVE] Limit sell @ {price}: oid={parsed['oid']} status={parsed['status']}")
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

    def cancel_all_orders(self) -> int:
        """Cancel all open orders for the configured symbol. Returns count cancelled."""
        if self.config.paper_trade:
            return 0

        orders = self.get_open_orders()
        if not orders:
            return 0

        cancel_requests = [
            {"coin": self.config.hl_symbol, "oid": int(o["oid"])}
            for o in orders if o.get("oid") is not None
        ]
        if not cancel_requests:
            return 0

        try:
            self.exchange.bulk_cancel(cancel_requests)
            logger.info(f"[LIVE] Bulk cancelled {len(cancel_requests)} orders")
        except Exception as e:
            logger.error(f"Bulk cancel failed, cancelling individually: {e}")
            for req in cancel_requests:
                try:
                    self.exchange.cancel(name=self.config.hl_symbol, oid=req["oid"])
                except Exception as e2:
                    logger.warning(f"Individual cancel oid={req['oid']} failed: {e2}")

        return len(cancel_requests)

    def place_tp_sl_orders(self, quantity: float, side: str, tp_price: float, sl_price: float) -> dict:
        """Place TP and SL trigger orders on the exchange.

        For long positions: TP = sell trigger, SL = sell trigger
        For short positions: TP = buy trigger, SL = buy trigger
        """
        if self.config.paper_trade:
            logger.info(f"[PAPER] TP/SL: TP={tp_price:.2f} SL={sl_price:.2f} for {side}")
            return {"tp": "paper", "sl": "paper"}

        is_buy_close = side == "short"
        quantity = self._round_size(quantity)
        tp_price = self._round_price(tp_price)
        sl_price = self._round_price(sl_price)

        results = {}

        try:
            tp_result = self.exchange.order(
                name=self.config.hl_symbol,
                is_buy=is_buy_close,
                sz=quantity,
                limit_px=tp_price,
                order_type={"trigger": {"triggerPx": tp_price, "isMarket": True, "tpsl": "tp"}},
                reduce_only=True,
            )
            results["tp"] = tp_result
            logger.info(f"[LIVE] TP trigger placed: {tp_price:.2f} ({side})")
        except Exception as e:
            logger.error(f"Failed to place TP trigger at {tp_price}: {e}")
            results["tp_error"] = str(e)

        try:
            sl_result = self.exchange.order(
                name=self.config.hl_symbol,
                is_buy=is_buy_close,
                sz=quantity,
                limit_px=sl_price,
                order_type={"trigger": {"triggerPx": sl_price, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )
            results["sl"] = sl_result
            logger.info(f"[LIVE] SL trigger placed: {sl_price:.2f} ({side})")
        except Exception as e:
            logger.error(f"Failed to place SL trigger at {sl_price}: {e}")
            results["sl_error"] = str(e)

        return results

    def place_market_sell(self, quantity: float) -> dict:
        return self.place_market_close(quantity, side="long")

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        units = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
        return units.get(interval, 3_600_000)
