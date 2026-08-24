"""Persistent trade IDs and append-only lifecycle events for Trader V2."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
import uuid

logger = logging.getLogger(__name__)


DEFAULT_LIFECYCLE_FILE = Path("trade_lifecycle.jsonl")


def generate_trade_id(symbol: str, side: str, now: float | None = None) -> str:
    """Return a readable unique ID for one trade lifecycle.

    Example: ``BTC-L-20260824-184205-A3F7``.
    UTC is used so IDs are unambiguous across server/user timezones.
    """
    ts = float(now if now is not None else time.time())
    stamp = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    side_code = "L" if str(side).lower() == "long" else "S"
    suffix = uuid.uuid4().hex[:4].upper()
    return f"{str(symbol).upper()}-{side_code}-{stamp}-{suffix}"


class TradeLifecycleLog:
    """Append-only structured event log keyed by ``trade_id``."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or DEFAULT_LIFECYCLE_FILE)

    def event(
        self,
        trade_id: str,
        event: str,
        symbol: str,
        side: str = "",
        **details,
    ) -> dict:
        ts = time.time()
        row = {
            "timestamp": ts,
            "timestamp_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "trade_id": str(trade_id),
            "event": str(event),
            "symbol": str(symbol),
            "side": str(side),
            "details": details,
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        except Exception as exc:
            logger.warning("Could not write trade lifecycle event: %s", exc)
        return row

    def load_trade(self, trade_id: str) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        try:
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    if row.get("trade_id") == trade_id:
                        out.append(row)
        except Exception as exc:
            logger.warning("Could not read trade lifecycle events: %s", exc)
        return out
