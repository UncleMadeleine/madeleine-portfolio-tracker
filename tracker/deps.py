"""可选/重型第三方依赖的统一懒加载入口.

各业务模块不再各写一份 ``_ak`` / ``_requests`` / ``_yf``:

- 导入本身保持轻量 (测试 ``test_all_tracker_modules_import_clean`` 要求
  任何 tracker 子模块 import 时不触发重 IO / 联网);
- 导入失败不缓存, 下次调用仍会重试 (可选依赖未安装时回退到其它数据源).
"""

from __future__ import annotations

__all__ = ["akshare", "requests", "yfinance"]


def akshare():
    """akshare 模块 (惰性导入: 首次约 10s, 不能放在模块顶层)."""
    import akshare as ak

    return ak


def yfinance():
    """yfinance 模块 (惰性导入)."""
    import yfinance as yf

    return yf


def requests():
    """requests 模块 (惰性导入, 保持 CLI 冷启动轻量)."""
    import requests as req

    return req
