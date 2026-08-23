# Auto Trading Bot - Support & Resistance Levels

An automated trading bot that detects and scores support/resistance levels from 4H candle data, places limit orders at the strongest levels, and manages positions with dynamic risk parameters.

Supports **Hyperliquid** (decentralized perps, 1x leverage) and **ccxt** exchanges (Binance, etc.), with optional **Telegram** alerts.

## How it works

1. **Detects levels** from 4H candles using swing-point analysis, wick detection, and price clustering
2. **Scores each level (0-100)** based on touch count, volume, recency, and rejection strength
3. **Places limit buy orders** at the strongest levels (support bounce + resistance breakout)
4. **Dynamic risk**: tighter SL on stronger levels, minimum 2:1 reward-to-risk
5. **Self-cleans**: cancels stale orders and orders at invalidated levels
6. **Telegram alerts** on every trade, exit, and error (optional)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your settings
```

### Hyperliquid setup

1. Get your wallet address and private key (MetaMask or similar)
2. Set `EXCHANGE_BACKEND=hyperliquid` in `.env`
3. Set `HL_WALLET_ADDRESS` and `HL_PRIVATE_KEY`
4. Start with `HL_MAINNET=false` (testnet) and `PAPER_TRADE=true`

### Telegram setup (optional)

1. Message [@BotFather](https://t.me/BotFather) on Telegram, create a bot, copy the token
2. Message [@userinfobot](https://t.me/userinfobot) to get your chat ID
3. Set `TG_BOT_TOKEN` and `TG_CHAT_ID` in `.env`

## Configuration

All settings are in `.env` (see `.env.example`):

| Variable | Description | Default |
|---|---|---|
| `EXCHANGE_BACKEND` | `hyperliquid` or `ccxt` | `hyperliquid` |
| `HL_SYMBOL` | Hyperliquid asset name | `BTC` |
| `HL_LEVERAGE` | Leverage multiplier | `1` |
| `TIMEFRAME` | Candle timeframe | `4h` |
| `ORDER_SIZE` | Buy size in quote currency | `50` |
| `MAX_OPEN_ORDERS` | Max simultaneous limit orders | `5` |
| `ORDER_TTL_HOURS` | Cancel unfilled orders after | `24` |
| `PAPER_TRADE` | Simulate orders | `true` |

## Deploy on a VPS

```bash
git clone https://github.com/fedricdamai/auto-trading-bot.git
cd auto-trading-bot
bash setup.sh
nano .env       # fill in your keys
sudo systemctl start tradingbot
journalctl -u tradingbot -f
```

## Tests

```bash
pytest tests/ -v
```

## Disclaimer

This bot is for educational purposes. Trading involves risk. Use paper trading mode first. Never trade with money you can't afford to lose.
