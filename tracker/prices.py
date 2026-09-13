"""行情门面 (facade): 全部取数路由到 tracker.providers 数据层.

本模块只保留稳定的历史 API (get_quote / get_quotes / get_history / get_ohlc 与
Quote 数据结构), 供 CLI/页面/测试使用; 路由、降级与市场域隔离逻辑见 providers 包:
  - GlobalStocksProvider: 美股/港股/全球 (IBKR 优先 → yfinance → 港股 akshare 兜底)
  - CNStocksProvider: A股/B股 (.SS/.SZ/.BJ) 固定 akshare 优先 → yfinance
  - CryptoProvider: 加密货币 (BASE-QUOTE) 走 Binance → yfinance
"""
from __future__ import annotations

from dataclasses import dataclass

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
from .symbols import ParsedSymbol, parse
from . import cache as cache_mod

__all__ = [
    "Quote",
    "get_quote",
    "get_quotes",
    "get_history",
    "get_ohlc",
    "parse",
]


def get_quote(symbol: str, prefer_akshare: bool = False) -> Quote:
    """单代码实时行情 (prefer_akshare 仅影响港股源顺序)."""
    from .providers.base import resolve

    p = parse(symbol)
    return resolve(p.type).fetch_quote(p, prefer_first=prefer_akshare)


def get_ohlc(
    symbol: str,
    months: int = 12,
    prefer_akshare: bool = False,
    refresh: bool = False,
    use_ibkr: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """K线日线数据 (date/open/high/low/close/volume, 升序), 带磁盘缓存与清洗.

    惰性导入 charting (plotly 较重), 避免拖慢其它子命令.
    """
    from .charting import clean_ohlc

    p = parse(symbol)
    is_range = start_date is not None
    if not refresh and not is_range:
        cached = cache_mod.get_ohlc_cached(p.yahoo, months)
        if cached is not None:
            return cached
    df = clean_ohlc(get_history(symbol, months=months, start_date=start_date, end_date=end_date, prefer_akshare=prefer_akshare, use_ibkr=use_ibkr))
    if not df.empty and not is_range:
        cache_mod.set_ohlc_cached(p.yahoo, months, df)
    return df
