import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

JOURNAL_FILE = "trade_journal.jsonl"


@dataclass
class TradeRecord:
    entry_price: float
    exit_price: float
    entry_time: float
    exit_time: float
    side: str               # "long" or "short"
    kind: str               # "support" or "resistance"
    exit_reason: str        # "take_profit" or "stop_loss"
    level_strength: float
    timeframes: list[str]
    leverage: int
    price_pnl_pct: float
    margin_pnl_pct: float
    quantity: float
    pnl_usd: float
    sl_trailed: bool
    tp_extended: bool
    hold_duration_h: float


class TradeJournal:
    """Append-only trade journal stored as JSON lines."""

    def __init__(self, path: str | None = None):
        self.path = Path(path or JOURNAL_FILE)

    def record(self, trade: TradeRecord):
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(asdict(trade)) + "\n")
            logger.info(f"Trade recorded: {trade.side} {trade.exit_reason} {trade.kind} PnL={trade.margin_pnl_pct:+.2f}%")
        except Exception as e:
            logger.error(f"Failed to write trade journal: {e}")

    def load_all(self) -> list[TradeRecord]:
        if not self.path.exists():
            return []
        records = []
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if "side" not in data:
                        data["side"] = "long"
                    records.append(TradeRecord(**data))
        except Exception as e:
            logger.error(f"Failed to read trade journal: {e}")
        return records

    def load_recent(self, n: int = 50) -> list[TradeRecord]:
        all_trades = self.load_all()
        return all_trades[-n:]

    def stats(self) -> dict:
        trades = self.load_all()
        if not trades:
            return {"total": 0}

        wins = [t for t in trades if t.margin_pnl_pct > 0]
        losses = [t for t in trades if t.margin_pnl_pct <= 0]

        total_pnl = sum(t.pnl_usd for t in trades)
        avg_win = sum(t.margin_pnl_pct for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.margin_pnl_pct for t in losses) / len(losses) if losses else 0

        support_trades = [t for t in trades if t.kind == "support"]
        resistance_trades = [t for t in trades if t.kind == "resistance"]
        support_wins = [t for t in support_trades if t.margin_pnl_pct > 0]
        resistance_wins = [t for t in resistance_trades if t.margin_pnl_pct > 0]

        long_trades = [t for t in trades if t.side == "long"]
        short_trades = [t for t in trades if t.side == "short"]
        long_wins = [t for t in long_trades if t.margin_pnl_pct > 0]
        short_wins = [t for t in short_trades if t.margin_pnl_pct > 0]

        multi_tf = [t for t in trades if len(t.timeframes) >= 2]
        single_tf = [t for t in trades if len(t.timeframes) < 2]
        multi_tf_wins = [t for t in multi_tf if t.margin_pnl_pct > 0]
        single_tf_wins = [t for t in single_tf if t.margin_pnl_pct > 0]

        strong = [t for t in trades if t.level_strength >= 60]
        weak = [t for t in trades if t.level_strength < 60]
        strong_wins = [t for t in strong if t.margin_pnl_pct > 0]
        weak_wins = [t for t in weak if t.margin_pnl_pct > 0]

        trailed = [t for t in trades if t.sl_trailed]
        trailed_wins = [t for t in trailed if t.margin_pnl_pct > 0]

        return {
            "total": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) * 100,
            "total_pnl_usd": round(total_pnl, 2),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "support_total": len(support_trades),
            "support_win_rate": len(support_wins) / len(support_trades) * 100 if support_trades else 0,
            "resistance_total": len(resistance_trades),
            "resistance_win_rate": len(resistance_wins) / len(resistance_trades) * 100 if resistance_trades else 0,
            "long_total": len(long_trades),
            "long_win_rate": len(long_wins) / len(long_trades) * 100 if long_trades else 0,
            "short_total": len(short_trades),
            "short_win_rate": len(short_wins) / len(short_trades) * 100 if short_trades else 0,
            "multi_tf_total": len(multi_tf),
            "multi_tf_win_rate": len(multi_tf_wins) / len(multi_tf) * 100 if multi_tf else 0,
            "single_tf_total": len(single_tf),
            "single_tf_win_rate": len(single_tf_wins) / len(single_tf) * 100 if single_tf else 0,
            "strong_total": len(strong),
            "strong_win_rate": len(strong_wins) / len(strong) * 100 if strong else 0,
            "weak_total": len(weak),
            "weak_win_rate": len(weak_wins) / len(weak) * 100 if weak else 0,
            "trailed_total": len(trailed),
            "trailed_win_rate": len(trailed_wins) / len(trailed) * 100 if trailed else 0,
        }
