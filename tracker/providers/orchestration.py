"""批量编排: 按域分组调用 provider, 聚合行情/历史结果.

输入混合代码列表 → parse → 按 provider 分组 → 各域独立取数 (IBKR 只注入全球域
与 A 股域的批量行情前置; 指数域无实时行情, 直接记 errors) → 汇总 quotes/errors/notes。
任一域失败不影响其它域。
逐代码源链回退并发执行 (全球域死代码单次 10s+ 耗时, 串行会拖死大批量场景);
取失败的代码进进程内负缓存, TTL 内跳过重试 (死代码每轮重烧 10s+ 无意义)。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pandas as pd

from .. import cache as cache_mod
from ..symbols import ParsedSymbol, parse as _parse_symbol
from .base import Quote, resolve
from .crypto import CryptoProvider
from .cn_stocks import CNStocksProvider
from .global_stocks import _yahoo_batch
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


# ---------- 进程内负缓存: 全部源失败的代码, TTL 内不再重试 ----------

_NEG_TTL = cache_mod.CACHE_TTL  # 与实时行情缓存同周期 (5 分钟)
_neg_cache: dict[str, float] = {}
_neg_lock = Lock()
_FALLBACK_WORKERS = 8  # 逐代码源链回退并发数


def _neg_filtered(
    plist: list[ParsedSymbol], errors: dict[str, str]
) -> list[ParsedSymbol]:
    """剔除负缓存命中项 (记入 errors), 返回真正需要取数的代码."""
    now = time.time()
    out: list[ParsedSymbol] = []
    with _neg_lock:
        expired = [k for k, ts in _neg_cache.items() if now - ts >= _NEG_TTL]
        for k in expired:
            _neg_cache.pop(k, None)
        hit = {k for k, ts in _neg_cache.items() if now - ts < _NEG_TTL}
    for p in plist:
        if p.yahoo in hit:
            errors[p.yahoo] = "近期全部数据源失败 (负缓存), 稍后自动重试"
        else:
            out.append(p)
    return out


def _fetch_domain(
    domain: str,
    plist: list[ParsedSymbol],
    prefer_akshare: bool,
    errors: dict[str, str],
) -> dict[str, Quote]:
    """一个域内并发走源链: 返回 {yahoo: Quote}, 失败写 errors + 负缓存."""
    provider = resolve({"global": "GLOBAL", "cn": "CN", "crypto": "CRYPTO"}[domain])

    def _one(p: ParsedSymbol):
        # 单代码失败捕获为 Exception 值返回: pool.map 迭代时不中断其余代码
        try:
            if domain == "global":
                # 批量已尝试 yfinance: 这里走完整源链 (港股 akshare 兜底)
                return p.yahoo, provider.fetch_quote(p, prefer_first=prefer_akshare)
            return p.yahoo, provider.fetch_quote(p)
        except Exception as e:  # noqa: BLE001 - 单代码失败记录 errors
            return p.yahoo, e

    out: dict[str, Quote] = {}
    with ThreadPoolExecutor(max_workers=_FALLBACK_WORKERS) as pool:
        for p, (yahoo, result_or_exc) in zip(
            plist, pool.map(_one, plist), strict=False
        ):
            if isinstance(result_or_exc, Exception):
                errors[yahoo] = str(result_or_exc)
                with _neg_lock:
                    _neg_cache[yahoo] = time.time()
            else:
                out[yahoo] = result_or_exc
                with _neg_lock:
                    _neg_cache.pop(yahoo, None)
    return out


def _route_provider(p: ParsedSymbol):
    """ParsedSymbol → provider 实例 (按权威 type 字段路由)."""
    return resolve(p.type)


def get_quotes(
    symbols,
    prefer_akshare: bool = False,
    use_ibkr: bool = False,
    use_longport: bool = False,
) -> tuple[dict[str, Quote], dict[str, str], list[str]]:
    """多代码实时行情: 缓存 → 长桥/IBKR 批量 (可选, IBKR 优先) → 按域 provider 取数 → 回写缓存."""
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

    # 可选批量前置: 长桥先取, IBKR 后取并覆盖 —— 两源都启用时 IBKR 优先级更高
    if use_longport:
        try:
            from .. import longport as lp_mod

            lp_quotes, reason = lp_mod.get_quotes_longport(list(by_yahoo.values()))
            quotes.update(lp_quotes)
            if reason:
                notes.append(f"长桥不可用: {reason} (已回退默认数据源)")
        except Exception as e:  # noqa: BLE001 - 长桥属可选增强
            notes.append(f"长桥接入异常: {e}")
    if use_ibkr:
        try:
            from .. import ibkr as ibkr_mod

            ib_quotes, reason = ibkr_mod.get_quotes_ibkr(list(by_yahoo.values()))
            quotes.update(ib_quotes)
            if reason:
                notes.append(f"IBKR 不可用: {reason} (已回退默认数据源)")
        except Exception as e:  # noqa: BLE001 - IBKR 属可选增强
            notes.append(f"IBKR 接入异常: {e}")
    groups: dict[str, list[ParsedSymbol]] = {}
    for p in by_yahoo.values():
        if p.yahoo in quotes:
            continue
        provider = _route_provider(p)
        if isinstance(provider, IndexProvider):
            # 指数域无实时行情 (仅历史K线): 显式记入 errors, 不进全球/股票源链
            errors.setdefault(
                p.yahoo, f"{p.yahoo}: 指数域仅提供历史K线 (index-kline), 无实时行情"
            )
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

    # 各域剩余代码并发走 provider 源链 (批量未命中的全球股 / 全部 CN / 全部 crypto)。
    # 负缓存命中项不发起网络 (TTL 内全部源失败过的代码), 直接记 errors。
    # 域间串行保持日志可读, 域内并发: 死代码单次 10s+, 串行会拖死大批量场景。
    for domain in ("global", "cn", "crypto"):
        plist = [
            p
            for p in _neg_filtered(groups.get(domain, []), errors)
            if p.yahoo not in quotes
        ]
        if plist:
            quotes.update(_fetch_domain(domain, plist, prefer_akshare, errors))

    # 只回写本次真正取到的新鲜行情 (IBKR/akshare/yahoo), 缓存命中项不重写,
    # 否则 set_cached 会刷新其 fetched_at, TTL 被无限延长
    fresh = {
        sym: q for sym, q in quotes.items() if sym in by_yahoo and sym not in cached
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
    use_longport: bool = False,
) -> pd.DataFrame:
    """单代码历史K线: 长桥/IBKR (可选) → 所属 provider 源链."""
    from datetime import date, timedelta

    p = _parse_symbol(symbol)
    if start_date is None:
        start_date = (date.today() - timedelta(days=months * 31)).isoformat()
    if end_date is None:
        end_date = date.today().isoformat()
    if use_ibkr:
        try:
            from .. import ibkr as ibkr_mod

            ib_hist, reason = ibkr_mod.get_history_ibkr(
                [p], months, start_date=start_date, end_date=end_date
            )
            if p.yahoo in ib_hist:
                return ib_hist[p.yahoo]
            if reason:
                import warnings

                warnings.warn(f"IBKR 历史数据不可用: {reason} (已回退)")
        except Exception as e:  # noqa: BLE001 - IBKR 属可选增强
            import warnings

            warnings.warn(f"IBKR 历史数据异常: {e}")
    if use_longport:
        try:
            from .. import longport as lp_mod

            lp_hist = lp_mod.get_history_longport(p, start_date, end_date)
            if lp_hist is not None:
                return lp_hist
        except Exception as e:  # noqa: BLE001 - 长桥属可选增强
            import warnings

            warnings.warn(f"长桥历史数据异常: {e}")
    provider = _route_provider(p)
    return provider.fetch_history(p, start_date, end_date, prefer_first=prefer_akshare)
