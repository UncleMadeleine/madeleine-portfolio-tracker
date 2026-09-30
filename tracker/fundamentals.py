"""基本面取数门面: 每股净资产 (book value per share) 的批量获取与磁盘缓存.

数据源: OpenBB equity.fundamental.metrics (provider=yfinance), 覆盖
美股/港股/A股/德英加澳等 yfinance 支持的股权标的; crypto 无此数据。
结果缓存 6h (book_value 为低频报表数据); 无有效值同样落库 (负缓存),
TTL 内不再重复请求。

PB (市净率) 本身不落库: pb = price / book_value, 现价实时变化, 由
services.watchlist 在组装视图时即时计算 (rules.metric_value_for_quote)。
"""

from __future__ import annotations

from threading import Lock

from . import cache as cache_mod

_METRICS_TTL = 21600  # 秒 (6h); book_value 属低频报表数据

_lock = Lock()
_inflight: dict[str, object] = {}


def _fund_metrics(symbols: list[str]) -> dict[str, float | None]:
    """OpenBB fundamental.metrics 批量拉取 → {symbol: book_value | None}."""
    from .providers.global_stocks import _obb

    ob = _obb()
    out: dict[str, float | None] = {s: None for s in symbols}
    res = ob.equity.fundamental.metrics(
        symbol=symbols, provider="yfinance"
    )
    for item in res.results:
        d = item.model_dump()
        sym = str(d.get("symbol") or "").strip()
        if sym not in out:
            continue
        bv = d.get("book_value")
        try:
            f = float(bv) if bv is not None else None
        except (TypeError, ValueError):
            f = None
        out[sym] = f if f is not None and f == f else None
    return out


def fetch_book_values(symbols: list[str]) -> dict[str, float | None]:
    """批量获取每股净资产 → {symbol: book_value | None}.

    磁盘缓存优先 (6h TTL), 未命中的批量走 OpenBB (单次调用), 失败静默
    返回 None (调用方按「PB 缺失」降级, 不抛异常)。
    """
    out: dict[str, float | None] = {}
    missing: list[str] = []
    for s in dict.fromkeys(symbols):
        hit = cache_mod.get_fundamental_cached(s, ttl=_METRICS_TTL)
        if hit is not None:
            out[s] = hit["book_value"]
        else:
            missing.append(s)
    if not missing:
        return out
    try:
        fresh = _fund_metrics(missing)
    except Exception:  # noqa: BLE001 - 基本面失败不阻断快照/提醒主流程
        fresh = {s: None for s in missing}
    for s in missing:
        bv = fresh.get(s)
        cache_mod.set_fundamental_cached(s, bv)
        out[s] = bv
    return out


def book_value_for(symbol: str) -> float | None:
    """单代码每股净资产 (并发去重: 同代码 in-flight 只发一次请求)."""
    with _lock:
        gate = _inflight.setdefault(symbol, Lock())
    acquired = gate.acquire(blocking=False)
    try:
        if acquired:
            return fetch_book_values([symbol]).get(symbol)
        gate.acquire()
        gate.release()
    finally:
        if acquired:
            with _lock:
                if _inflight.get(symbol) is gate:
                    _inflight.pop(symbol, None)
            gate.release()
    return fetch_book_values([symbol]).get(symbol)
