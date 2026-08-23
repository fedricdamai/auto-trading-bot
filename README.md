# Auto Trading Bot - Support & Resistance Levels

An automated trading bot that detects support and resistance levels from historical price data and places buy orders when price approaches those levels.

Supports **Hyperliquid** (decentralized perps, 1x leverage) and **ccxt** exchanges (Binance, etc.), with optional **Telegram** alerts.

## How it works

1. **Fetches candle data** from Hyperliquid or any ccxt exchange
2. **Detects support/resistance levels** using swing-point analysis and price clustering
3. **Places buy orders** when price is within tolerance of a confirmed level
4. **Manages positions** with configurable stop-loss and take-profit
5. **Sends Telegram alerts** on trades, exits, and errors (optional)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your settings
```

### Hyperliquid setup

1. Get your wallet address and private key from your Hyperliquid account
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
| `TIMEFRAME` | Candle timeframe | `1h` |
| `ORDER_SIZE` | Buy size in quote currency | `50` |
| `STOP_LOSS_PCT` | Stop loss % | `2.0` |
| `TAKE_PROFIT_PCT` | Take profit % | `4.0` |
| `PAPER_TRADE` | Simulate orders | `true` |

## Run

```bash
python main.py
```

## Tests

```bash
pytest tests/ -v
```

## Disclaimer

This bot is for educational purposes. Trading involves risk. Use paper trading mode first. Never trade with money you can't afford to lose.
