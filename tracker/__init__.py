"""Madeleine Portfolio Tracker — 本地多市场投资组合追踪 (多数据源 + Streamlit/CLI 双前端)."""

from __future__ import annotations

import importlib
from typing import Any

#: 单一版本来源 (pyproject.toml 动态读取; CLI --version 与此一致)
__version__ = "1.1.0"

# 惰性再导出: 包导入本身不拉 pandas / providers / OpenBB。
# CLI 的 --help / -v / init 等轻量路径因此无需付出 ~0.6s 的 pandas 冷启动;
# `from tracker import get_quote` 等既有 API 仍在首次访问时透明加载。
_LAZY_ATTRS: dict[str, str] = {
    "build_view": ".analytics",
    "summarize": ".analytics",
    "get_fx_rates": ".fx",
    "get_rate": ".fx",
    "Quote": ".prices",
    "get_history": ".prices",
    "get_quote": ".prices",
    "get_quotes": ".prices",
    "MARKET_META": ".symbols",
    "Market": ".symbols",
    "normalize": ".symbols",
    "parse": ".symbols",
    "sort_watchlist": ".services.watchlist",
}

__all__ = [
    "MARKET_META",
    "Market",
    "Quote",
    "build_view",
    "get_fx_rates",
    "get_history",
    "get_quote",
    "get_quotes",
    "get_rate",
    "normalize",
    "parse",
    "sort_watchlist",
    "summarize",
]


def __getattr__(name: str) -> Any:
    """PEP 562: 首次访问时才导入对应子模块 (保持原 re-export API 不变)."""
    module = _LAZY_ATTRS.get(name)
    if module is not None:
        value = getattr(importlib.import_module(module, __name__), name)
        globals()[name] = value  # 缓存, 后续访问不再走 __getattr__
        return value
    # 兼容 `import tracker; tracker.storage` 这类子模块属性访问:
    # 旧版 __init__ 的 eager import 会顺带把 storage/prices 等挂到包上。
    if not name.startswith("_"):
        try:
            value = importlib.import_module(f".{name}", __name__)
        except ModuleNotFoundError:
            pass
        else:
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_ATTRS))
