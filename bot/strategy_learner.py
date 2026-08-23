import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path

from bot.trade_journal import TradeJournal

logger = logging.getLogger(__name__)

MIN_TRADES_TO_LEARN = 5
ADJUSTMENTS_FILE = "strategy_adjustments.json"


@dataclass
class StrategyParams:
    """Parameters the learner can adjust."""
    min_strength: float = 30.0       # minimum level strength to trade
    min_timeframes: int = 1          # require N timeframes confirming
    support_weight: float = 1.0      # multiplier for support orders (0.0-1.5)
    resistance_weight: float = 1.0   # multiplier for resistance orders (0.0-1.5)
    target_pnl_pct: float = 1.0      # margin profit target
    max_loss_pct: float = 1.0        # margin loss limit
    confidence_scale: float = 1.0    # order size multiplier (0.5-1.5)


class StrategyLearner:
    """Analyzes trade history and adjusts strategy parameters."""

    def __init__(self, journal: TradeJournal, path: str | None = None):
        self.journal = journal
        self.path = Path(path or ADJUSTMENTS_FILE)
        self.params = self._load_params()
        self.insights: list[str] = []

    def _load_params(self) -> StrategyParams:
        if self.path.exists():
            try:
                with open(self.path) as f:
                    data = json.load(f)
                return StrategyParams(**data.get("params", {}))
            except Exception:
                pass
        return StrategyParams()

    def _save_params(self):
        try:
            with open(self.path, "w") as f:
                json.dump({
                    "params": asdict(self.params),
                    "insights": self.insights[-10:],
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save strategy adjustments: {e}")

    def learn(self) -> StrategyParams:
        """Analyze trade journal and return adjusted parameters."""
        stats = self.journal.stats()
        self.insights = []

        if stats["total"] < MIN_TRADES_TO_LEARN:
            self.insights.append(f"Need {MIN_TRADES_TO_LEARN - stats['total']} more trades before adjusting")
            return self.params

        logger.info(f"Learning from {stats['total']} trades (win rate: {stats['win_rate']:.1f}%)")

        self._adjust_level_type_weights(stats)
        self._adjust_timeframe_requirements(stats)
        self._adjust_strength_threshold(stats)
        self._adjust_tp_sl(stats)
        self._adjust_confidence(stats)

        self._save_params()
        for insight in self.insights:
            logger.info(f"  LEARN: {insight}")

        return self.params

    def _adjust_level_type_weights(self, stats: dict):
        """Favor support or resistance based on win rates."""
        if stats["support_total"] >= 3 and stats["resistance_total"] >= 3:
            sup_wr = stats["support_win_rate"]
            res_wr = stats["resistance_win_rate"]

            if sup_wr > res_wr + 15:
                self.params.support_weight = min(1.5, self.params.support_weight + 0.1)
                self.params.resistance_weight = max(0.3, self.params.resistance_weight - 0.1)
                self.insights.append(f"Support outperforms ({sup_wr:.0f}% vs {res_wr:.0f}%) → boosting support weight to {self.params.support_weight:.1f}")
            elif res_wr > sup_wr + 15:
                self.params.resistance_weight = min(1.5, self.params.resistance_weight + 0.1)
                self.params.support_weight = max(0.3, self.params.support_weight - 0.1)
                self.insights.append(f"Resistance outperforms ({res_wr:.0f}% vs {sup_wr:.0f}%) → boosting resistance weight to {self.params.resistance_weight:.1f}")
            else:
                self.params.support_weight = _nudge_toward(self.params.support_weight, 1.0, 0.05)
                self.params.resistance_weight = _nudge_toward(self.params.resistance_weight, 1.0, 0.05)

    def _adjust_timeframe_requirements(self, stats: dict):
        """Require multi-TF confirmation if it wins more."""
        if stats["multi_tf_total"] >= 3 and stats["single_tf_total"] >= 3:
            multi_wr = stats["multi_tf_win_rate"]
            single_wr = stats["single_tf_win_rate"]

            if multi_wr > single_wr + 20 and single_wr < 40:
                self.params.min_timeframes = 2
                self.insights.append(f"Multi-TF much better ({multi_wr:.0f}% vs {single_wr:.0f}%) → requiring 2+ timeframes")
            elif single_wr >= 50:
                self.params.min_timeframes = 1
                self.insights.append(f"Single-TF profitable ({single_wr:.0f}%) → keeping min_timeframes=1")

    def _adjust_strength_threshold(self, stats: dict):
        """Raise min strength if weak levels lose more."""
        if stats["strong_total"] >= 3 and stats["weak_total"] >= 3:
            strong_wr = stats["strong_win_rate"]
            weak_wr = stats["weak_win_rate"]

            if weak_wr < 35 and strong_wr > 50:
                new_min = min(60.0, self.params.min_strength + 5)
                if new_min != self.params.min_strength:
                    self.params.min_strength = new_min
                    self.insights.append(f"Weak levels losing ({weak_wr:.0f}% win rate) → raising min strength to {new_min}")
            elif weak_wr >= 50:
                new_min = max(20.0, self.params.min_strength - 5)
                if new_min != self.params.min_strength:
                    self.params.min_strength = new_min
                    self.insights.append(f"Weak levels profitable ({weak_wr:.0f}%) → lowering min strength to {new_min}")

    def _adjust_tp_sl(self, stats: dict):
        """Fine-tune TP/SL based on actual outcomes."""
        trades = self.journal.load_all()
        if len(trades) < MIN_TRADES_TO_LEARN:
            return

        wins = [t for t in trades if t.margin_pnl_pct > 0]
        losses = [t for t in trades if t.margin_pnl_pct <= 0]

        if wins:
            avg_win = sum(t.margin_pnl_pct for t in wins) / len(wins)
            if avg_win > self.params.target_pnl_pct * 1.5 and stats["win_rate"] > 55:
                self.insights.append(f"Avg win ({avg_win:.2f}%) > target → keeping current TP (letting winners run)")

        if losses:
            avg_loss = abs(sum(t.margin_pnl_pct for t in losses) / len(losses))
            if avg_loss > self.params.max_loss_pct * 1.3:
                new_sl = max(0.3, self.params.max_loss_pct - 0.1)
                if new_sl != self.params.max_loss_pct:
                    self.params.max_loss_pct = round(new_sl, 1)
                    self.insights.append(f"Avg loss ({avg_loss:.2f}%) too high → tightening max loss to {new_sl}%")

        if stats["win_rate"] < 35 and stats["total"] >= 10:
            new_tp = min(3.0, self.params.target_pnl_pct + 0.1)
            if new_tp != self.params.target_pnl_pct:
                self.params.target_pnl_pct = round(new_tp, 1)
                self.insights.append(f"Win rate low ({stats['win_rate']:.0f}%) → widening target to {new_tp}%")

    def _adjust_confidence(self, stats: dict):
        """Scale order size based on overall performance."""
        if stats["total"] < 10:
            return

        win_rate = stats["win_rate"]
        if win_rate >= 60:
            self.params.confidence_scale = min(1.5, self.params.confidence_scale + 0.05)
            self.insights.append(f"High win rate ({win_rate:.0f}%) → scaling up size to {self.params.confidence_scale:.2f}x")
        elif win_rate < 40:
            self.params.confidence_scale = max(0.5, self.params.confidence_scale - 0.05)
            self.insights.append(f"Low win rate ({win_rate:.0f}%) → scaling down size to {self.params.confidence_scale:.2f}x")
        else:
            self.params.confidence_scale = _nudge_toward(self.params.confidence_scale, 1.0, 0.02)

    def get_insights_text(self) -> str:
        """Return human-readable summary of what the bot has learned."""
        stats = self.journal.stats()
        if stats["total"] == 0:
            return "No trades yet. The bot will start learning after the first closed position."

        lines = [
            f"Trades: {stats['total']} ({stats['wins']}W / {stats['losses']}L)",
            f"Win rate: {stats['win_rate']:.1f}%",
            f"Total PnL: ${stats['total_pnl_usd']:+.2f}",
            f"Avg win: {stats['avg_win_pct']:+.2f}% | Avg loss: {stats['avg_loss_pct']:+.2f}%",
            "",
            f"Support: {stats['support_win_rate']:.0f}% win ({stats['support_total']} trades)",
            f"Resistance: {stats['resistance_win_rate']:.0f}% win ({stats['resistance_total']} trades)",
            f"Multi-TF: {stats['multi_tf_win_rate']:.0f}% win ({stats['multi_tf_total']} trades)",
            f"Single-TF: {stats['single_tf_win_rate']:.0f}% win ({stats['single_tf_total']} trades)",
            f"Strong levels: {stats['strong_win_rate']:.0f}% win ({stats['strong_total']} trades)",
            f"Weak levels: {stats['weak_win_rate']:.0f}% win ({stats['weak_total']} trades)",
        ]

        if self.insights:
            lines.append("")
            lines.append("Adjustments:")
            for ins in self.insights[-5:]:
                lines.append(f"  • {ins}")

        lines.append("")
        lines.append("Active params:")
        lines.append(f"  Min strength: {self.params.min_strength}")
        lines.append(f"  Min timeframes: {self.params.min_timeframes}")
        lines.append(f"  Support weight: {self.params.support_weight:.1f}x")
        lines.append(f"  Resistance weight: {self.params.resistance_weight:.1f}x")
        lines.append(f"  Size scale: {self.params.confidence_scale:.2f}x")

        return "\n".join(lines)


def _nudge_toward(current: float, target: float, step: float) -> float:
    if abs(current - target) < step:
        return target
    return current + step if current < target else current - step
