"""snapshot 子命令: 组合 + 自选快照 (持仓 / 自选提醒 / 阈值触发)."""
from __future__ import annotations


def cmd_snapshot(args) -> None:
    """组合 + 自选快照, 复用 tracker.snapshot.run_snapshot."""
    from .. import snapshot

    snapshot.run_snapshot(args)
