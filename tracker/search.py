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

__all__ = ["SymbolEntry", "search_symbols", "search_grouped", "resolve_symbol"]


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
    """跨域聚合搜索 (平铺列表).

    域间按 cn → global → crypto 排序, 每域先取 ceil(limit/2) 条,
    最后整表截断到 limit (cn 域优先占用名额, 小 limit 时后两域可能无露出 ——
    需要三域均衡露出时用 search_grouped)。
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


def resolve_symbol(query: str, *, limit: int = 1) -> list[dict]:
    """宽松输入 → 规范代码候选 (供 CLI kline/compare 自动回退).

    合法代码直接 parse 归一 (AAPL/700.HK/600519.SH); 其余 (700/腾讯/NVDA 片段)
    走在线搜索取前 limit 条。返回 [{code, name, market, type}], code 恒为规范
    Yahoo 形态、可直接喂 prices.get_ohlc / kline / compare。
    """
    q = (query or "").strip()
    if not q:
        return []
    from .symbols import parse

    # 两类 parse 会"放行"但取数必失败的形态, 先于 parse 短路走在线搜索:
    # - 裸纯数字 (parse 误判成美股; global.search 东财补零把 700 → 0700.HK)
    # - 非 ASCII (中文名, parse 误判成美股; 东财 suggest 中文召回 0700.HK)
    #   搜索无果时才回退 parse 结果, 保留对真实存在的非常规代码的兼容
    if q.isdigit() or not q.isascii():
        hits = [h for h in search_symbols(q, limit=max(limit, 5))
                if not (h["code"].isdigit() and h["type"] == "global")]
        if not hits:
            return _parse_pass(q, parse)
        if q.isdigit():
            # 数字查询: 补零/原码精确命中排最前 (700/00700 → 0700.HK);
            # 同为精确命中取代码更短者 (0700.HK 优于 000700.SZ 的 000700),
            # 子串匹配 (600700.SS 等) 最后 —— 用户输 700 想要的是"那只股"
            digits = q.lstrip("0") or "0"
            hits.sort(key=lambda h: (
                0 if h["code"].split(".")[0].lstrip("0") == digits else 1,
                len(h["code"].split(".")[0]),
            ))
        return hits[:limit]
    try:
        p = parse(q)
        return [{"code": p.yahoo, "name": q, "market": p.market_label, "type": p.type}]
    except ValueError:
        pass
    return search_symbols(q, limit=limit)


def _parse_pass(q: str, parse) -> list[dict]:
    """搜索无果时的兜底: 沿用 parse 放行结果.

    仅接受取数层能处理的形态: 带后缀 / 连字符 / 纯 ASCII 字母 ticker;
    非 ASCII 名称或裸数字 parse 会误判成美股, 原样返回取数必失败, 拒绝。
    """
    try:
        p = parse(q)
    except ValueError:
        return []
    ok = (
        "." in p.yahoo
        or "-" in p.yahoo
        or (p.yahoo.isascii() and p.yahoo.isalpha())
    )
    if not ok:
        return []
    return [{"code": p.yahoo, "name": q, "market": p.market_label, "type": p.type}]


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
