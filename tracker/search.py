"""代码搜索聚合层: 三域 provider 各自提供搜索接口 (封装在线搜索 API + 归一化).

UI/CLI 调用 search_grouped() / search_symbols() → 路由到各 provider.search():
- GlobalStocksProvider.search: IBKR reqMatchingSymbols (可选) → yfinance Search
- CNStocksProvider.search:     东财 suggest (沪/深/京 A股 B股)
- CryptoProvider.search:       yfinance Search (CRYPTOCURRENCY 结果)

无本地目录、无缓存快照: 每个域只封装自己的在线搜索接口并归一化为规范代码;
provider 搜索失败时返回 [] (空结果), 最终由各域的合法代码直查兜底
(_fallback_match, 经 symbols.parse 校验)。
域隔离不变: 结果的权威 type 由 symbols.parse 推导, 不信任接口返回。
"""
from __future__ import annotations

from dataclasses import asdict

from .providers import PROVIDERS
from .providers.base import SymbolEntry

# 聚合搜索的固定域顺序 (UI 下拉分组展示顺序)
_DOMAINS = ("cn", "global", "crypto")

__all__ = ["SymbolEntry", "search_symbols", "search_grouped"]


def search_grouped(query: str, limit_per_domain: int = 5) -> dict[str, list[dict]]:
    """按域分组搜索: {type: [{code, name, market, type}]}.

    每个域独立调用对应 provider.search(), 互不影响 (任一域失败返回空组)。
    UI 用本方法展示三块独立下拉; type 为权威域标记 (cn/global/crypto)。
    """
    q = (query or "").strip()
    if not q:
        return {t: [] for t in _DOMAINS}
    grouped: dict[str, list[dict]] = {}
    for t in _DOMAINS:
        try:
            entries = PROVIDERS[t].search(q, limit=limit_per_domain)
        except NotImplementedError:
            entries = []
        except Exception:
            entries = []
        grouped[t] = [asdict(e) for e in entries]
    return grouped


def search_symbols(
    query: str, limit: int = 10, entries: list[SymbolEntry] | None = None
) -> list[dict]:
    """跨域聚合搜索 (平铺列表, 域间按 cn → global → crypto 排序).

    每域取 ceil(limit/2) 条保证三域都有露出; limit 较小时按序截断。
    entries 参数仅为兼容保留 (忽略): 搜索完全走 provider.search()。
    """
    q = (query or "").strip()
    if not q:
        return []
    per = max(2, (limit + 1) // 2)
    grouped = search_grouped(q, limit_per_domain=per)
    out: list[dict] = []
    for t in _DOMAINS:
        out.extend(grouped.get(t, []))
    return out[:limit]


def _fallback_match(query: str, limit: int) -> list[dict]:
    """合法代码直查兜底: 把查询当作代码, 经 parse 校验合法后原样返回."""
    from .symbols import parse

    q = query.strip().upper()
    if not q:
        return []
    try:
        p = parse(q)
    except ValueError:
        return []
    return [
        {"code": p.yahoo, "name": q, "market": p.market_label, "type": p.type}
    ][:limit]
