"""Provider 基础设施: Quote/ParsedSymbol 数据结构与 provider 注册解析."""
from __future__ import annotations

from dataclasses import dataclass

from ..symbols import Market, ParsedSymbol, parse as _parse_symbol

__all__ = [
    "Provider",
    "Quote",
    "ParsedSymbol",
    "get_history",
    "get_quote",
    "get_quotes",
    "resolve",
]


@dataclass
class Quote:
    """实时行情快照 (所有 provider 输出统一结构)."""

    symbol: str
    name: str | None
    price: float
    prev_close: float | None
    change_pct: float | None
    currency: str


class Provider:
    """单一市场域数据源: 行情 + 历史K线 + 代码目录.

    子类实现 fetch_quote / fetch_history / search_catalog;
    排序后的数据源链由 quote_sources / history_sources 给出。
    """

    name = "provider"

    def quote_sources(self, p: ParsedSymbol, prefer_first: bool = False) -> list:
        """该 symbol 的行情数据源函数链 (按优先级, 依次尝试)."""
        raise NotImplementedError

    def history_sources(self, p: ParsedSymbol, start_date: str, end_date: str | None, prefer_first: bool = False) -> list:
        """该 symbol 的历史数据源函数链 (按优先级, 依次尝试)."""
        raise NotImplementedError

    # -- 默认实现: 按源链依次尝试 --

    def fetch_quote(self, p: ParsedSymbol, prefer_first: bool = False) -> Quote:
        last_err: Exception | None = None
        for fn in self.quote_sources(p, prefer_first):
            try:
                return fn(p)
            except Exception as e:  # noqa: BLE001 - 降级到下一数据源
                last_err = e
        raise RuntimeError(f"{self.name} 行情获取失败 ({last_err})")

    def fetch_history(self, p: ParsedSymbol, start_date: str, end_date: str | None, prefer_first: bool = False):
        """返回 DataFrame (date/open/high/low/close/volume); 全部源失败抛 RuntimeError."""
        last_err: Exception | None = None
        for fn in self.history_sources(p, start_date, end_date, prefer_first):
            try:
                df = fn()
                if not df.empty:
                    return df
            except Exception as e:  # noqa: BLE001 - 降级到下一数据源
                last_err = e
        raise RuntimeError(f"{p.yahoo}: 历史数据获取失败 ({last_err})")


def resolve(type_or_market) -> Provider:
    """权威域标记 (type: global/cn/crypto) → provider 实例.

    兼容 Market 枚举 (内部兜底); 未知标记抛 ValueError 显式报错。
    """
    from . import PROVIDERS

    t = str(
        type_or_market.value if hasattr(type_or_market, "value") else type_or_market
    ).upper()
    if t == "CRYPTO":
        return PROVIDERS["crypto"]
    if t in ("CN", "BJ"):
        return PROVIDERS["cn"]
    if t in ("GLOBAL", "US", "HK", "DE", "GB", "CA", "AU"):
        return PROVIDERS["global"]
    raise ValueError(f"未知域标记: {type_or_market}")


def get_quote(symbol: str, prefer_akshare: bool = False) -> Quote:
    """单代码实时行情: 按 parse 得到的权威 type 路由."""
    p = _parse_symbol(symbol)
    return resolve(p.type).fetch_quote(p, prefer_first=prefer_akshare)


def get_quotes(
    symbols, prefer_akshare: bool = False, use_ibkr: bool = False
) -> tuple[dict[str, Quote], dict[str, str], list[str]]:
    """多代码批量行情 (按 provider 分域聚合). 见 orchestration.get_quotes."""
    from .orchestration import get_quotes as _impl

    return _impl(symbols, prefer_akshare=prefer_akshare, use_ibkr=use_ibkr)


def get_history(symbol: str, months: int = 12, start_date: str | None = None, end_date: str | None = None, prefer_akshare: bool = False, use_ibkr: bool = False):
    """单代码历史K线: 自动路由到所属 provider. 见 orchestration.get_history."""
    from .orchestration import get_history as _impl

    return _impl(symbol, months=months, start_date=start_date, end_date=end_date, prefer_akshare=prefer_akshare, use_ibkr=use_ibkr)
