"""批量编排: 按域分组调用 provider, 聚合行情/历史结果.

输入混合代码列表 → parse → 按 provider 分组 → 各域独立取数 (IBKR 只注入全球域
与 A 股域的批量行情前置; 指数域无实时行情, 直接记 errors) → 汇总 quotes/errors/notes。
任一域失败不影响其它域。
"""
from __future__ import annotations

import pandas as pd

from .. import cache as cache_mod
from ..symbols import ParsedSymbol, parse as _parse_symbol
from .base import Quote, resolve
from .crypto import CryptoProvider
from .cn_stocks import CNStocksProvider
from .global_stocks import GlobalStocksProvider, _akshare_quote, _yahoo_batch, _yahoo_history
from .index import IndexProvider

__all__ = ["get_quotes", "get_history"]


def _parse_all(symbols) -> tuple[dict[str, ParsedSymbol], dict[str, str]]:
    by_yahoo: dict[str, ParsedSymbol] = {}
    errors: dict[str, str] = {}
    for s in symbols:
        try:
            p = _parse_symbol(s)
            by_yahoo.setdefault(p.yahoo, p)
        except ValueError as e:
            errors[str(s)] = str(e)
    return by_yahoo, errors


def _route_provider(p: ParsedSymbol):
    """ParsedSymbol → provider 实例 (按权威 type 字段路由)."""
    return resolve(p.type)


def get_quotes(
    symbols, prefer_akshare: bool = False, use_ibkr: bool = False
) -> tuple[dict[str, Quote], dict[str, str], list[str]]:
    """多代码实时行情: 缓存 → IBKR 批量 (可选) → 按域 provider 取数 → 回写缓存."""
    quotes: dict[str, Quote] = {}
    notes: list[str] = []
    by_yahoo, errors = _parse_all(symbols)
    if not by_yahoo:
        return quotes, errors, notes

    # 优先读本地缓存
    cached = cache_mod.get_cached(list(by_yahoo.keys()))
    if cached:
        quotes.update(cached)
    if len(quotes) == len(by_yahoo):
        return quotes, errors, notes

    if use_ibkr:
        try:
            from .. import ibkr as ibkr_mod

            ib_quotes, reason = ibkr_mod.get_quotes_ibkr(list(by_yahoo.values()))
            quotes.update(ib_quotes)
            if reason:
                notes.append(f"IBKR 不可用: {reason} (已回退默认数据源)")
        except Exception as e:  # noqa: BLE001 - IBKR 属可选增强
            notes.append(f"IBKR 接入异常: {e}")

    # 按域分组, 各域独立取数
    groups: dict[str, list[ParsedSymbol]] = {}
    for p in by_yahoo.values():
        if p.yahoo in quotes:
            continue
        provider = _route_provider(p)
        if isinstance(provider, IndexProvider):
            # 指数域无实时行情 (仅历史K线): 显式记入 errors, 不进全球/股票源链
            errors.setdefault(p.yahoo, f"{p.yahoo}: 指数域仅提供历史K线 (index-kline), 无实时行情")
            continue
        if isinstance(provider, CryptoProvider):
            groups.setdefault("crypto", []).append(p)
        elif isinstance(provider, CNStocksProvider):
            groups.setdefault("cn", []).append(p)
        else:
            groups.setdefault("global", []).append(p)

    # 全球域: 先批量 (yfinance), 缺失再逐个走源链
    global_rest = groups.get("global", [])
    if global_rest:
        batch = _yahoo_batch(global_rest)
        quotes.update(batch)

    # 各域逐个走 provider 源链 (批量未命中的全球股 / 全部 CN / 全部 crypto)
    for domain, plist in groups.items():
        provider = resolve(
            {"global": "GLOBAL", "cn": "CN", "crypto": "CRYPTO"}[domain]
        )
        for p in plist:
            if p.yahoo in quotes:
                continue
            try:
                if domain == "global":
                    # 批量已尝试 yfinance: 这里走完整源链 (港股 akshare 兜底)
                    quotes[p.yahoo] = provider.fetch_quote(p, prefer_first=prefer_akshare)
                else:
                    quotes[p.yahoo] = provider.fetch_quote(p)
            except Exception as e:  # noqa: BLE001 - 单代码失败记录 errors
                errors[p.yahoo] = str(e)

    # 只回写本次真正取到的新鲜行情 (IBKR/akshare/yahoo), 缓存命中项不重写,
    # 否则 set_cached 会刷新其 fetched_at, TTL 被无限延长
    fresh = {
        sym: q for sym, q in quotes.items()
        if sym in by_yahoo and sym not in cached
    }
    cache_mod.set_cached(fresh)
    return quotes, errors, notes


def get_history(
    symbol: str,
    months: int = 12,
    start_date: str | None = None,
    end_date: str | None = None,
    prefer_akshare: bool = False,
    use_ibkr: bool = False,
) -> pd.DataFrame:
    """单代码历史K线: IBKR (可选) → 所属 provider 源链."""
    from datetime import date, timedelta

    p = _parse_symbol(symbol)
    if start_date is None:
        start_date = (date.today() - timedelta(days=months * 31)).isoformat()
    if end_date is None:
        end_date = date.today().isoformat()
    if use_ibkr:
        try:
            from .. import ibkr as ibkr_mod

            ib_hist, reason = ibkr_mod.get_history_ibkr([p], months)
            if p.yahoo in ib_hist:
                return ib_hist[p.yahoo]
            if reason:
                import warnings

                warnings.warn(f"IBKR 历史数据不可用: {reason} (已回退)")
        except Exception as e:  # noqa: BLE001 - IBKR 属可选增强
            import warnings

            warnings.warn(f"IBKR 历史数据异常: {e}")
    provider = _route_provider(p)
    return provider.fetch_history(p, start_date, end_date, prefer_first=prefer_akshare)
