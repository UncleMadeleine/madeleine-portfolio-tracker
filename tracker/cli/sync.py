"""sync 子命令: 从 IB Gateway 账户同步持仓 (--mode paper|live)."""
from __future__ import annotations


def cmd_sync(args) -> None:
    """从 IBKR 同步持仓, 复用 tracker.ibkr_sync.run_sync."""
    from .. import ibkr_sync

    ibkr_sync.run_sync(args)
