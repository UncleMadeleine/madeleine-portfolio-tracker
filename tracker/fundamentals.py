"""基本面取数门面: 每股净资产 (book value per share) 与总市值的批量获取与磁盘缓存.

数据源: OpenBB equity.fundamental.metrics (provider=yfinance), 覆盖
美股/港股/A股/德英加澳等 yfinance 支持的股权标的; crypto 无此数据。
EODHD (BYOK, 可选) 可补/替换全球股票域:
  - fundamentals=primary: 先用 EODHD (BookValue / MarketCapitalization),
    EODHD 缺失的字段再由 OpenBB 补齐
  - fundamentals=fallback (默认): 先走 OpenBB, 只对仍缺失的字段调用 EODHD
结果缓存 6h (book_value/market_cap 为低频报表数据); 无有效值同样落库 (负缓存),
TTL 内不再重复请求。

PB (市净率) 本身不落库: pb = price / book_value, 现价实时变化, 由
services.watchlist 在组装视图时即时计算 (rules.metric_value_for_quote)。
市值 (market_cap) 是数据源快照口径, 同样由视图层按指标基准取用。
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from . import cache as cache_mod

_METRICS_TTL = 21600  # 秒 (6h); book_value 属低频报表数据

_lock = Lock()
_inflight: dict[str, object] = {}


def _fund_metrics(symbols: list[str]) -> dict[str, dict[str, float | None]]:
    """OpenBB fundamental.metrics 批量拉取 → {symbol: {book_value, market_cap}}."""
    from .providers.global_stocks import _obb

    ob = _obb()
    out: dict[str, dict[str, float | None]] = {
        s: {"book_value": None, "market_cap": None} for s in symbols
    }
    res = ob.equity.fundamental.metrics(symbol=symbols, provider="yfinance")
    for item in res.results:
        d = item.model_dump()
        sym = str(d.get("symbol") or "").strip()
        if sym not in out:
            continue
        for key in ("book_value", "market_cap"):
            v = d.get(key)
            try:
                f = float(v) if v is not None else None
            except (TypeError, ValueError):
                f = None
            out[sym][key] = f if f is not None and math.isfinite(f) else None
    return out


def _load_cached(
    symbols: list[str], refresh: bool = False
) -> tuple[dict[str, dict], list[str]]:
    """按 symbol 顺序读缓存; 返回 (命中项, 未命中列表).

    refresh=True: 跳过缓存读取, 全部视为未命中 —— 供用户显式「强制刷新」
    穿透 6h 负缓存 (失败结果同样落库, 但手动刷新不应被上一次失败锁住)。
    """
    if refresh:
        return {}, list(dict.fromkeys(symbols))
    out: dict[str, dict] = {}
    missing: list[str] = []
    for s in dict.fromkeys(symbols):
        hit = cache_mod.get_fundamental_cached(s, ttl=_METRICS_TTL)
        if hit is not None:
            out[s] = hit
        else:
            missing.append(s)
    return out, missing


def _empty_metrics() -> dict:
    return {"book_value": None, "market_cap": None}


def _complete(vals: dict | None) -> bool:
    """两个指标都有值才算完整 (用于判断是否需要下一数据源补齐)."""
    return bool(
        vals
        and vals.get("book_value") is not None
        and vals.get("market_cap") is not None
    )


def _merge_fill(primary: dict | None, secondary: dict | None) -> dict:
    """以 primary 为准, 只把 secondary 中 primary 缺失的字段补进来."""
    out = _empty_metrics()
    primary = primary or {}
    secondary = secondary or {}
    for key in ("book_value", "market_cap"):
        out[key] = primary.get(key)
        if out[key] is None:
            out[key] = secondary.get(key)
    return out


def _eodhd_metrics(symbols: list[str]) -> dict[str, dict]:
    """EODHD 基本面 (可选 BYOK) → {symbol: {book_value, market_cap}}.

    只处理全球股票域且 token 可用的代码; 单代码失败静默跳过, 不阻断
    yfinance/长桥等既有链路。并发 4 路, 避免自选 PB/市值提醒串行等待。
    """
    from . import eodhd as eodhd_mod

    cfg = eodhd_mod.load_config()
    if eodhd_mod.fundamentals_mode(cfg) == "off":
        return {}
    targets = [s for s in symbols if eodhd_mod.yahoo_to_eodhd(s)]
    if not targets:
        return {}

    def _one(sym: str):
        try:
            return sym, eodhd_mod.fundamentals(sym)
        except Exception:  # noqa: BLE001 - 单代码失败只跳过该代码
            return sym, None

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for sym, data in pool.map(_one, targets):
            if not data:
                continue
            vals = {
                "book_value": data.get("book_value"),
                "market_cap": data.get("market_cap"),
            }
            if vals["book_value"] is not None or vals["market_cap"] is not None:
                out[sym] = vals
    return out


def _fill_fundamentals(out: dict[str, dict], missing: list[str]) -> None:
    """为缓存未命中的 symbol 取数并写缓存 (EODHD + OpenBB 按配置组合)."""
    if not missing:
        return
    from . import eodhd as eodhd_mod

    mode = eodhd_mod.fundamentals_mode()
    fresh: dict[str, dict] = {}
    pending = list(missing)
    if mode == "primary":
        fresh.update(_eodhd_metrics(pending))
        pending = [s for s in pending if not _complete(fresh.get(s))]

    if pending:
        try:
            ob = _fund_metrics(pending)
        except Exception:  # noqa: BLE001 - 基本面失败不阻断快照/提醒主流程
            ob = {s: _empty_metrics() for s in pending}
        for s in pending:
            fresh[s] = _merge_fill(fresh.get(s), ob.get(s))

    if mode == "fallback":
        need = [s for s in missing if not _complete(fresh.get(s))]
        if need:
            for s, vals in _eodhd_metrics(need).items():
                fresh[s] = _merge_fill(fresh.get(s), vals)

    for s in missing:
        vals = fresh.get(s) or _empty_metrics()
        cache_mod.set_fundamental_cached(
            s,
            vals.get("book_value"),
            market_cap=vals.get("market_cap"),
        )
        out[s] = (
            cache_mod.get_fundamental_cached(s, ttl=_METRICS_TTL) or _empty_metrics()
        )


def fetch_book_values(
    symbols: list[str], refresh: bool = False
) -> dict[str, float | None]:
    """批量获取每股净资产 → {symbol: book_value | None}.

    磁盘缓存优先 (6h TTL), 未命中的按配置走 EODHD/OpenBB (见模块说明),
    失败静默返回 None (调用方按「PB 缺失」降级, 不抛异常)。
    refresh=True 跳过缓存读取 (显式强制刷新, 穿透负缓存)。
    """
    out, missing = _load_cached(symbols, refresh=refresh)
    _fill_fundamentals(out, missing)
    return {s: out.get(s, {}).get("book_value") for s in dict.fromkeys(symbols)}


def fetch_market_caps(
    symbols: list[str], refresh: bool = False
) -> dict[str, float | None]:
    """批量获取总市值 (当地货币) → {symbol: market_cap | None}.

    与 PB 共用同一缓存行与外层取数调用 (OpenBB/EODHD 一次返回两个指标);
    crypto 与无数据源标的静默返回 None (调用方按「市值缺失」降级)。
    refresh=True 跳过缓存读取 (显式强制刷新, 穿透负缓存)。
    """
    out, missing = _load_cached(symbols, refresh=refresh)
    _fill_fundamentals(out, missing)
    return {s: out.get(s, {}).get("market_cap") for s in dict.fromkeys(symbols)}


def book_value_for(symbol: str, refresh: bool = False) -> float | None:
    """单代码每股净资产 (并发去重: 同代码 in-flight 只发一次请求)."""
    with _lock:
        gate = _inflight.setdefault(symbol, Lock())
    acquired = gate.acquire(blocking=False)
    try:
        if acquired:
            if refresh:
                return fetch_book_values([symbol], refresh=True).get(symbol)
            return fetch_book_values([symbol]).get(symbol)
        gate.acquire()
        gate.release()
    finally:
        if acquired:
            with _lock:
                if _inflight.get(symbol) is gate:
                    _inflight.pop(symbol, None)
            gate.release()
    if refresh:
        return fetch_book_values([symbol], refresh=True).get(symbol)
    return fetch_book_values([symbol]).get(symbol)
