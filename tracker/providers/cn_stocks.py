"""A股/B股 provider: 沪深北交易所股票, akshare 优先, yfinance 兜底.

代码规范: .SS (沪) / .SZ (深) / .BJ (北交所); B 股 (900xxx.SS / 200xxx.SZ)
同属本域, 数据源与 A 股一致。与全球股票域靠后缀严格区分。
"""

from __future__ import annotations

from ..symbols import Market, ParsedSymbol
from .base import Provider, SymbolEntry
from .em_suggest import (
    em_code_to_yahoo,
    normalize_suggest_row,
    parse_symbol_safe,
    suggest_merged,
)
from .global_stocks import (
    _akshare_history,
    _akshare_quote,
    _yahoo_history,
    _yahoo_quote,
)


class CNStocksProvider(Provider):
    """A股/B股/北交所域: akshare (东财) 固定优先, yfinance 兜底."""

    name = "cn"

    def quote_sources(self, p: ParsedSymbol, prefer_first: bool = False) -> list:
        # 本域 akshare 永远在前 (prefer_first 仅影响全球域的港股顺序)
        return [_akshare_quote, _yahoo_quote]

    def history_sources(
        self,
        p: ParsedSymbol,
        start_date: str,
        end_date: str | None,
        prefer_first: bool = False,
    ) -> list:
        return [
            lambda: _akshare_history(p, start_date, end_date),
            lambda: _yahoo_history(p, start_date, end_date),
        ]

    # 本域在东财 suggest 中对应的市场 (SecurityTypeName)
    _EM_TYPES = ("沪A", "深A", "科创版", "科创板", "京A", "沪B", "深B")

    def search(self, query: str, limit: int = 10) -> list[SymbolEntry]:
        """A股域搜索: 东财 suggest (沪/深/京 A股 B股) + 合法代码直查兜底。"""
        q = query.strip()
        if not q:
            return []

        def _local_fallback() -> list[SymbolEntry]:
            from ..search import _fallback_match

            return [
                SymbolEntry(r["code"], r["name"], r["market"], r["type"])
                for r in _fallback_match(q, limit)
                if r["type"] == "cn"
            ]

        merged = suggest_merged(q)
        if merged is None:
            # 东财不可达: 仍走本地代码直查 (规范 A股代码无需网络),
            # 不能因为在线源失败就把 600519.SS 这类合法输入也判为搜不到
            return _local_fallback()
        rows, _failed = merged
        out: list[SymbolEntry] = []
        seen: set[str] = set()
        for row in rows:
            norm = normalize_suggest_row(row)
            if norm is None or norm[1] not in self._EM_TYPES:
                continue
            code, stype = norm
            p = parse_symbol_safe(em_code_to_yahoo(code, stype))
            if p is None or p.yahoo in seen or p.type != "cn":
                continue
            seen.add(p.yahoo)
            out.append(SymbolEntry(p.yahoo, row["Name"], p.market_label, p.type))
        if not out:
            return _local_fallback()
        return out[:limit]


def is_cn_market(market) -> bool:
    m = market.value if hasattr(market, "value") else str(market).upper()
    return m in (Market.CN.value, Market.BJ.value)
