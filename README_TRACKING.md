# V2 Trade Lifecycle Tracking

Every V2 trade gets one persistent audit ID, for example:

`BTC-L-20260824-184205-A3F7`

The same ID follows the trade through its full lifecycle:

- trigger created
- grouped opening order pending
- fill
- TP/SL active
- TP/SL repair pending / repaired
- cancellation
- restart recovery
- final exit and PnL

Structured events are appended to `trade_lifecycle.jsonl` on the VPS. The active thesis file `v2_trade_thesis.json` also stores the same `trade_id` with the exact entry, TP and SL so the identity survives restarts.

Telegram order, fill, cancel and exit notifications display the Trade ID. Use:

`/trade SYMBOL`

for the currently active trade on a symbol, or:

`/trade TRADE_ID`

for the latest lifecycle events of a specific trade.

The tracking layer does not alter support/resistance detection, direction selection, order sizing, grouped order submission, or repair-only TP/SL behavior.
