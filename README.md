# Auto Trading Bot

Automated support/resistance trading bot with a conservative higher-timeframe Hyperliquid engine focused on fewer, higher-quality entries and strict order cleanup.

## Strategy

The decision stack is intentionally higher timeframe:

```text
4h market regime
        ↓
1h + 4h trend agreement
        ↓
30m / 1h / 4h support-resistance
        ↓
30m touch + rejection confirmation
        ↓
pullback limit entry
        ↓
structural ATR stop + risk-sized position
```

There are no 1m or 5m inputs in the current strategy.

### Entry lifecycle

1. A completed 30m candle must reject a qualifying level.
2. A pullback limit order is armed after confirmation.
3. The entry is valid only until the next 30m candle boundary.
4. If it remains unfilled, the exact entry is cancelled and exchange state is verified.
5. A new or opposite signal cannot replace it until the previous order family is confirmed clean.
6. TP and SL triggers are created only after the exchange confirms that the entry actually filled.

### Risk model

Stops are based on market structure and volatility, not `max_loss_pct / leverage`.

```text
position_notional = risk_per_trade_usd / stop_distance_pct
```

The result is capped by maximum notional and a maximum fraction of account value allocated as isolated margin.

Default validation settings:

| Setting | Default |
|---|---:|
| Entry timeframe | 30m |
| Trend timeframes | 1h + 4h |
| Fixed leverage | 3x isolated |
| Risk per trade | $4 |
| Max notional | $500 |
| Minimum stop distance | 0.60% |
| Maximum stop distance | 2.50% |
| Target reward/risk | 1.50R |
| Pending-entry lifetime | one 30m candle |
| Symbol cooldown after exit | 30 minutes |

These are starting parameters for validation, not promises of profitability.

### Safety behavior

- one pending entry/position family per symbol
- no TP/SL triggers before entry fill
- exchange-verified cancellation before replacement signals
- cancel/fill race handling
- stale-order cleanup on startup
- exactly one TP and one SL expected for an open position
- periodic exchange-side TP/SL verification
- full-position TP/SL sizing if exchange size changes
- per-symbol circuit breaker when exchange state cannot be reconciled
- `/closeall` and `/stop` use exchange truth rather than only local memory
- adaptive strategy learning does not modify live risk parameters

## Configuration model

Trading parameters are **not stored in `.env`**.

All strategy, signal-quality, timeframe, leverage, stop, target and position-sizing parameters live in:

```text
bot/strategy_settings.py
```

That file is source-controlled so a trading-logic change is visible in Git history, reviewed together with the code, and covered by tests.

`.env` is intentionally restricted to credentials and deployment/runtime switches:

```env
EXCHANGE_BACKEND=hyperliquid
TRADER_VERSION=v2
PAPER_TRADE=true
LOG_LEVEL=INFO

HL_WALLET_ADDRESS=0xYourWalletAddress
HL_PRIVATE_KEY=your_private_key_here
HL_MAINNET=false

TG_BOT_TOKEN=
TG_CHAT_ID=
TG_ALLOWED_USERS=
```

`HL_MAINNET=false` means the bot reads Hyperliquid testnet. Keep this during isolated validation. `PAPER_TRADE=true` means the bot does not submit real exchange orders.

Never commit a real `.env` file or private key.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

For initial isolated testing keep:

```env
TRADER_VERSION=v2
PAPER_TRADE=true
HL_MAINNET=false
```

Do not switch directly from an older live process with resting orders. Flatten positions and cancel legacy orders first.

## Tests

```bash
python -m pytest -q
```

Tests cover structural stops, rejection confirmation, opposing-level filtering, fixed-risk sizing, pending-entry expiry, pre-fill trigger prevention, cancellation/fill races and exchange-sourced `/closeall` cleanup.

## VPS validation

```bash
git fetch origin
git checkout fix/trading-logic-v2
git pull --ff-only origin fix/trading-logic-v2
python -m pytest -q
```

Start manually in paper/testnet mode first and inspect decision logs before returning the service to systemd.

## Rollback

The previous engine remains temporarily available for comparison through:

```env
TRADER_VERSION=v1
```

The version switch is a deployment rollback mechanism, not a strategy parameter.

## Disclaimer

Trading involves substantial risk. Validate order behavior in paper mode and Hyperliquid testnet before using mainnet capital.
