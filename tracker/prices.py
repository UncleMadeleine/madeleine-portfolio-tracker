"""行情门面 (facade): 全部取数路由到 tracker.providers 数据层.

本模块只保留稳定的历史 API (get_quote / get_quotes / get_history / get_ohlc 与
Quote 数据结构), 供 CLI/页面/测试使用; 路由、降级与市场域隔离逻辑见 providers 包:
  - GlobalStocksProvider: 美股/港股/全球 (IBKR 优先 → yfinance → 港股 akshare 兜底)
  - CNStocksProvider: A股/B股 (.SS/.SZ/.BJ) 固定 akshare 优先 → yfinance
  - CryptoProvider: 加密货币 (BASE-QUOTE) 走 Binance → yfinance
"""

from __future__ import annotations


import pandas as pd

from .providers.base import Quote  # noqa: F401 - 再导出, 既有导入路径保持不变
from .providers.crypto import CryptoProvider  # noqa: F401
from .providers.cn_stocks import CNStocksProvider  # noqa: F401
from .providers.global_stocks import (  # noqa: F401 - 供测试 monkeypatch
    GlobalStocksProvider,
    _akshare_history,
    _akshare_quote,
    _yahoo_batch,
    _yahoo_history,
    _yahoo_quote,
)
from .providers.orchestration import get_history, get_quotes
from .symbols import parse
from . import cache as cache_mod

__all__ = [
    "Quote",
    "get_quote",
    "get_quotes",
    "get_history",
    "get_ohlc",
    "get_index_history",
]


def get_quote(symbol: str, prefer_akshare: bool = False) -> Quote:
    """单代码实时行情 (prefer_akshare 仅影响港股源顺序).

    与批量路径共用进程内负缓存: TTL 内全部源失败过的代码不再发起网络
    (单代码 OpenBB/yfinance 一次失败 8s+, CLI 循环多码查询会串行卡死)。
    """
    from .providers import orchestration as orch
    from .providers.base import resolve

    p = parse(symbol)
    if orch.neg_cached(p.yahoo):
        raise RuntimeError(
            f"{p.yahoo}: 近期全部数据源失败 (负缓存), 稍后自动重试"
        )
    try:
        return resolve(p.type).fetch_quote(p, prefer_first=prefer_akshare)
    except Exception:
        orch.neg_mark(p.yahoo)
        raise


def get_ohlc(
    symbol: str,
    months: int = 12,
    prefer_akshare: bool = False,
    refresh: bool = False,
    use_ibkr: bool = False,
    use_longport: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """K线日线数据 (date/open/high/low/close/volume, 升序), 带磁盘缓存与清洗.

    惰性导入 charting (plotly 较重), 避免拖慢其它子命令.
    """
    from .charting import clean_ohlc

    p = parse(symbol)
    # 只要指定了任一端日期就是「区间查询」: 结果可能被截断, 不能当作完整
    # months 窗口读写 (key 只有 symbol+months), 否则会把截断数据污染进缓存
    is_range = start_date is not None or end_date is not None
    if not refresh and not is_range:
        cached = cache_mod.get_ohlc_cached(p.yahoo, months)
        if cached is not None:
            return cached
    df = clean_ohlc(
        get_history(
            symbol,
            months=months,
            start_date=start_date,
            end_date=end_date,
            prefer_akshare=prefer_akshare,
            use_ibkr=use_ibkr,
            use_longport=use_longport,
        )
    )
    if not df.empty and not is_range:
        cache_mod.set_ohlc_cached(p.yahoo, months, df)
    return df


def get_index_history(
    symbol: str,
    months: int = 12,
    refresh: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """指数历史K线 (IX.<KEY>): 独立于股票 get_ohlc, 缓存键加 index: 前缀隔离.

    返回数据的**原生粒度**由数据源决定 (水泥网 CEMPI 端点为周K, 其余日K),
    调用方用 charting.infer_native_period / resolve_period 兜底判定,
    不得假设恒为日K。指数域不参与 IBKR 批量行情, 故无 use_ibkr /
    prefer_akshare 参数; 中国指数固定 akshare 优先, 其余 yfinance 主源
    (源链见 providers/index.py)。
    """
    from .charting import clean_ohlc

    p = parse(symbol)
    if p.type != "index":
        raise ValueError(f"{p.yahoo}: 不是指数代码 (指数代码规范 IX.<KEY>, 如 IX.DXY)")
    key = f"index:{p.yahoo}"
    is_range = start_date is not None or end_date is not None
    if not refresh and not is_range:
        cached = cache_mod.get_ohlc_cached(key, months)
        if cached is not None:
            return cached
    df = clean_ohlc(
        get_history(symbol, months=months, start_date=start_date, end_date=end_date)
    )
    if not df.empty and not is_range:
        cache_mod.set_ohlc_cached(key, months, df)
    return df
