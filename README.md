# Auto Trading Bot

Automated support/resistance trading bot with a conservative Hyperliquid V2 engine focused on fewer, higher-quality entries and strict order cleanup.

## Trader V2

Hyperliquid uses `TRADER_VERSION=v2` by default.

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

There are no 1m or 5m inputs in V2 trade decisions.

### Entry lifecycle

V2 does not leave a blind limit order sitting at an old support/resistance level indefinitely.

1. A completed 30m candle must reject a qualifying level.
2. A pullback limit order is armed after that confirmation.
3. The entry is valid only until the next 30m candle boundary.
4. If it is still unfilled, the exact entry is cancelled and exchange state is verified.
5. A new or opposite signal cannot replace it until the old order family is clean.
6. TP and SL triggers are created only after the exchange confirms that the entry actually filled.

This avoids stale pre-fill TP/SL orders and prevents an old thesis from surviving across multiple candles or a bot restart.

### Risk model

V2 stops are based on market structure and volatility, not `max_loss_pct / leverage`.

The stop is placed beyond the S/R level using an ATR buffer, subject to configurable minimum and maximum distances. Position notional is then calculated from the amount of USD you are willing to lose if that stop is reached:

```text
position_notional = risk_per_trade_usd / stop_distance_pct
```

The result is additionally capped by maximum notional and a maximum fraction of account value allocated as isolated margin.

Default validation settings are deliberately conservative:

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

These are starting parameters for paper/testnet validation, not promises of profitability.

### Safety behavior

V2 adds several order-lifecycle protections:

- one pending entry/position family per symbol
- no TP/SL triggers before entry fill
- exchange-verified cancellation before a new signal can be placed
- cancel/fill race handling
- cleanup of stale unfilled entries on startup
- exactly one TP and one SL expected for an open position
- periodic exchange-side TP/SL verification
- full-position TP/SL sizing if exchange position size changes
- per-symbol circuit breaker when order state cannot be reconciled safely
- adaptive strategy learning disabled for live V2 risk parameters

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your settings
```

For initial testing, keep:

```env
TRADER_VERSION=v2
PAPER_TRADE=true
HL_MAINNET=false
```

Do not switch directly from an older live version with resting orders. First inspect/cancel old orders or start V2 while flat so its startup reconciliation can establish a clean state.

### Hyperliquid

Set at minimum:

```env
EXCHANGE_BACKEND=hyperliquid
HL_WALLET_ADDRESS=0xYourWalletAddress
HL_PRIVATE_KEY=your_private_key_here
HL_SYMBOL=BTC
HL_SYMBOLS=BTC,ETH,SOL,HYPE,BNB
```

Never commit the real `.env` file or private key.

## Important V2 configuration

See `.env.example` for all settings. The main controls are:

| Variable | Purpose |
|---|---|
| `V2_LEVERAGE` | Fixed validation leverage |
| `V2_RISK_PER_TRADE_USD` | Maximum intended USD loss at initial SL |
| `V2_MAX_POSITION_NOTIONAL_USD` | Hard per-position notional cap |
| `V2_MAX_MARGIN_FRACTION` | Account-value cap for isolated margin |
| `V2_MIN_TREND_CONFIDENCE` | 4h/1h trend quality filter |
| `V2_MIN_LEVEL_STRENGTH` | Minimum S/R quality |
| `V2_MIN_LEVEL_TIMEFRAMES` | Required multi-timeframe confluence |
| `V2_ENTRY_PULLBACK_ATR` | Pullback distance after confirmation |
| `V2_SL_ATR_MULT` | ATR buffer beyond structure |
| `V2_MIN_STOP_DISTANCE_PCT` | Noise floor for initial SL |
| `V2_MAX_STOP_DISTANCE_PCT` | Reject trades requiring an excessive stop |
| `V2_TARGET_RISK_REWARD` | Initial TP multiple |
| `V2_SYMBOL_COOLDOWN_MINUTES` | Re-entry cooldown after exit |

## Tests

```bash
python -m pytest -q
```

The V2 tests cover structural stops, rejection confirmation, opposing-level filtering, fixed-risk sizing, pending-entry expiry, pre-fill trigger prevention and cancellation/fill races.

## Deploy on a VPS

After paper/testnet validation:

```bash
git fetch origin
git checkout fix/trading-logic-v2
pip install -r requirements.txt
python -m pytest -q
```

Then update `.env` and restart your service only after checking that the test suite passes and the Hyperliquid account has no unintended stale orders.

## Rollback

The old engine remains available for comparison:

```env
TRADER_VERSION=v1
```

## Disclaimer

Trading involves substantial risk. Validate order behavior in paper mode and Hyperliquid testnet before using mainnet capital.
