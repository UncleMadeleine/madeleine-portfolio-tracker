"""可选/重型第三方依赖的统一懒加载入口.

各业务模块不再各写一份 ``_obb`` / ``_ak`` / ``_requests``:

- 导入本身保持轻量 (测试 ``test_all_tracker_modules_import_clean`` 要求
  任何 tracker 子模块 import 时不触发重 IO / 联网);
- OpenBB 单例做成线程安全双检锁, 避免首次冷启动被并发重复触发;
- 导入失败不缓存, 下次调用仍会重试 (可选依赖未安装时回退到其它数据源).
"""

from __future__ import annotations

import threading

__all__ = ["akshare", "openbb", "requests", "yfinance"]

_obb_singleton = None
_obb_lock = threading.Lock()


def openbb():
    """OpenBB 单例 (线程安全缓存).

    首次 ``from openbb import obb`` 有冷启动开销 (本机实测 5.5s), 之后是
    进程内单例。批量/历史/汇率/crypto 路径共用一次初始化。
    """
    global _obb_singleton
    if _obb_singleton is None:
        with _obb_lock:
            if _obb_singleton is None:
                from openbb import obb

                _obb_singleton = obb
    return _obb_singleton


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
