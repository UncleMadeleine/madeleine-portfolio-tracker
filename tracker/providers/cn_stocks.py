"""A股/B股 provider: 沪深北交易所股票, akshare 优先, yfinance 兜底.

代码规范: .SS (沪) / .SZ (深) / .BJ (北交所); B 股 (900xxx.SS / 200xxx.SZ)
同属本域, 数据源与 A 股一致。与全球股票域靠后缀严格区分。
"""
from __future__ import annotations

from ..symbols import Market, ParsedSymbol
from .base import Provider
from .global_stocks import _akshare_history, _akshare_quote, _yahoo_history, _yahoo_quote


class CNStocksProvider(Provider):
    """A股/B股/北交所域: akshare (东财) 固定优先, yfinance 兜底."""

    name = "cn"

    def quote_sources(self, p: ParsedSymbol, prefer_first: bool = False) -> list:
        # 本域 akshare 永远在前 (prefer_first 仅影响全球域的港股顺序)
        return [_akshare_quote, _yahoo_quote]

    def history_sources(self, p: ParsedSymbol, start_date: str, end_date: str | None, prefer_first: bool = False) -> list:
        return [
            lambda: _akshare_history(p, start_date, end_date),
            lambda: _yahoo_history(p, start_date, end_date),
        ]


def is_cn_market(market) -> bool:
    m = market.value if hasattr(market, "value") else str(market).upper()
    return m in (Market.CN.value, Market.BJ.value)
