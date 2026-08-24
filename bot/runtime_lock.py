"""Single-process guard for the live trading engine."""

from __future__ import annotations

import os

_LOCK_HANDLE = None


def acquire_single_instance_lock(path: str = "/tmp/auto-trading-bot-v2.lock"):
    """Hold an exclusive process lock until the Python process exits.

    Duplicate bot processes can each maintain independent in-memory state and
    submit duplicate opening orders. On the Linux VPS we prevent that class of
    failure with a non-blocking flock.
    """
    global _LOCK_HANDLE
    if _LOCK_HANDLE is not None:
        return _LOCK_HANDLE

    import fcntl

    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(
            "another Trader V2 process already holds the runtime lock; "
            "stop the other process before starting a second instance"
        )

    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    _LOCK_HANDLE = handle
    return handle
