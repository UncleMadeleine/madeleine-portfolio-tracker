"""数据 provider 层: 三大市场域各一个 provider, 行情与搜索在数据层面按域隔离.

- GlobalStocksProvider: 美股/港股/全球股票 (裸代码 · .HK · .DE · .L · .TO · .AX ...)
  优先 IBKR, 其次 yfinance (OpenBB), 港股另以 akshare 兜底
- CNStocksProvider: A股/B股/北交所 (.SS · .SZ · .BJ) 优先 akshare, 其次 yfinance
- CryptoProvider: 加密货币 (BASE-QUOTE 连字符格式) 完全独立走 Binance API

三个域只在投资组合/watchlist 聚合层汇合; 代码后缀保证互不冲突。
对外统一入口见 tracker.providers.resolve / get_quotes / get_history。
"""
from __future__ import annotations

from .base import Provider, Quote
from .crypto import CryptoProvider
from .global_stocks import GlobalStocksProvider
from .cn_stocks import CNStocksProvider

PROVIDERS: dict[str, Provider] = {
    "global": GlobalStocksProvider(),
    "cn": CNStocksProvider(),
    "crypto": CryptoProvider(),
}

def provider_for(market) -> Provider:
    """ParsedSymbol.market → 对应 provider (供 IBKR 等模块按市场路由)."""
    m = market.value if hasattr(market, "value") else str(market).upper()
    if m == "CRYPTO":
        return PROVIDERS["crypto"]
    if m in ("CN", "BJ"):
        return PROVIDERS["cn"]
    return PROVIDERS["global"]
