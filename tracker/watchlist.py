"""自选股存储门面: 只做配置读写与条目 I/O 归一。

视图组装、阈值规则与取数用例在 services 层 (services/watchlist.py +
services/rules.py); 本模块仅保留 load/save 与条目归一 (storage 委托),
保证旧导入路径兼容。
"""
from __future__ import annotations

from .storage import (
    WATCHLIST_PATH as DEFAULT_WATCHLIST,
    load_watchlist,
    normalize_watch_entry,
    parse_lists,
    save_watchlist,
)

__all__ = [
    "DEFAULT_WATCHLIST",
    "load_watchlist",
    "normalize_watch_entry",
    "parse_lists",
    "save_watchlist",
]
