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

from . import cache as cache_mod

_METRICS_TTL = 21600  # 秒 (6h); book_value 属低频报表数据

def _fund_metrics(symbols: list[str]) -> dict[str, dict[str, float | None]]:
    """OpenBB fundamental.metrics 批量拉取 → {symbol: {book_value, market_cap}}."""
    from .providers.global_stocks import _obb

    ob = _obb()
    out: dict[str, dict[str, float | None]] = {}
    wanted = set(symbols)
    res = ob.equity.fundamental.metrics(symbol=symbols, provider="yfinance")
    for item in res.results:
        d = item.model_dump()
        sym = str(d.get("symbol") or "").strip()
        if sym not in wanted or sym in out:
            continue
        vals: dict[str, float | None] = {}
        for key in ("book_value", "market_cap"):
            v = d.get(key)
            try:
                f = float(v) if v is not None else None
            except (TypeError, ValueError):
                f = None
            vals[key] = f if f is not None and math.isfinite(f) else None
        out[sym] = vals
    return out


def _cache_covers_field(hit: dict, field: str, other: str) -> bool:
    """缓存行是否已覆盖本次请求的指标.

    已确认缺失 (*_checked) 算覆盖 (负缓存); 另一个指标有值而本指标为空,
    说明是旧调用方只写过一半, 需要补齐。旧库行没有 checked 标记时, 两个
    指标都为空按历史负缓存处理。
    """
    if hit.get(field) is not None or hit.get(f"{field}_checked"):
        return True
    legacy_both_empty = (
        not hit.get("book_value_checked")
        and not hit.get("market_cap_checked")
        and hit.get(other) is None
    )
    return legacy_both_empty


def _load_cached(
    symbols: list[str], refresh: bool = False, field: str = "book_value"
) -> tuple[dict[str, dict], list[str]]:
    """按 symbol 顺序读缓存; 返回 (命中项, 未命中列表).

    refresh=True: 跳过缓存读取, 全部视为未命中 —— 供用户显式「强制刷新」
    穿透 6h 负缓存 (失败结果同样落库, 但手动刷新不应被上一次失败锁住)。

    ``field`` 是本次真正要取的指标: 缓存行只有另一个指标时不能算命中,
    否则只写过 BVPS 的旧条目会挡住后续市值取数 (反之亦然)。已确认缺失
    (``*_checked``) 仍然命中, 负缓存语义不变。
    """
    unique = list(dict.fromkeys(symbols))
    if refresh:
        return {}, unique
    # 批量查: 逐条 get_fundamental_cached 在自选 600+ 时要付 600 次
    # 连接/建表检查, 一次 IN 查询即可。
    cached = cache_mod.get_fundamentals_meta(unique, ttl=_METRICS_TTL)
    other = "market_cap" if field == "book_value" else "book_value"
    out: dict[str, dict] = {}
    missing: list[str] = []
    for s in unique:
        hit = cached.get(s)
        if hit is not None and _cache_covers_field(hit, field, other):
            out[s] = {
                "book_value": hit["book_value"],
                "market_cap": hit["market_cap"],
            }
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

    out: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for sym, data in pool.map(_one, targets):
            if not data:
                # 请求过但失败: None 表示「未确认」而不是「确认没有」,
                # 调用方据此避免把临时故障写进 6h 负缓存。
                out[sym] = None
                continue
            vals = {
                "book_value": data.get("book_value"),
                "market_cap": data.get("market_cap"),
            }
            if vals["book_value"] is not None or vals["market_cap"] is not None:
                out[sym] = vals
            else:
                # 请求成功但两个字段都缺失: 是「确认没有」, 不是故障。
                out[sym] = {}
    return out


def _fill_fundamentals(out: dict[str, dict], missing: list[str]) -> None:
    """为缓存未命中的 symbol 取数并写缓存 (EODHD + OpenBB 按配置组合)."""
    if not missing:
        return
    from . import eodhd as eodhd_mod

    mode = eodhd_mod.fundamentals_mode()
    fresh: dict[str, dict] = {}
    # EODHD 请求过但返回失败的代码: 它缺失的字段不能标记为「已确认缺失」,
    # 否则一次临时网络故障会让该代码 6 小时内不再重试 EODHD。
    unconfirmed: set[str] = set()
    pending = list(missing)
    if mode == "primary":
        eodhd = _eodhd_metrics(pending)
        unconfirmed.update(s for s, v in eodhd.items() if v is None)
        fresh.update({s: v for s, v in eodhd.items() if v is not None})
        pending = [s for s in pending if not _complete(fresh.get(s))]

    if pending:
        try:
            ob = _fund_metrics(pending)
        except Exception:  # noqa: BLE001 - 基本面失败不阻断快照/提醒主流程
            # 整批失败 (网络/某个不支持的代码让 OpenBB 整批抛错) 时不能给
            # 每个 pending 都写负缓存: 一只坏代码会让同批其它标的 6 小时内
            # 都不再重试。留空 (fresh 无该键) 即「未确认」, 下轮仍会取。
            ob = {}
        for s in pending:
            if s in ob:
                fresh[s] = _merge_fill(fresh.get(s), ob.get(s))

    if mode == "fallback":
        need = [s for s in missing if not _complete(fresh.get(s))]
        if need:
            eodhd = _eodhd_metrics(need)
            unconfirmed.update(s for s, v in eodhd.items() if v is None)
            for s, vals in eodhd.items():
                if vals is not None:
                    fresh[s] = _merge_fill(fresh.get(s), vals)

    for s in missing:
        vals = fresh.get(s)
        if vals is not None:
            # 本轮已合并全部配置源: 有值时必然可确认; 只有仍为 None 且
            # 某个源请求失败时才保留「未确认」, 下轮继续重试该源。
            bv_checked = not (s in unconfirmed and vals.get("book_value") is None)
            mc_checked = not (s in unconfirmed and vals.get("market_cap") is None)
            cache_mod.set_fundamental_cached(
                s,
                vals.get("book_value"),
                market_cap=vals.get("market_cap"),
                # 显式标记后, 刷新才能清掉旧值, 另一个指标也不会被半行缓存挡住。
                book_value_checked=bv_checked,
                market_cap_checked=mc_checked,
            )
            out[s] = (
                cache_mod.get_fundamental_cached(s, ttl=_METRICS_TTL)
                or _empty_metrics()
            )
        else:
            # 未拿到结果 ≠ 确认无数据: 不落负缓存, 只给本次调用返回空指标
            out[s] = _empty_metrics()


def fetch_book_values(
    symbols: list[str], refresh: bool = False
) -> dict[str, float | None]:
    """批量获取每股净资产 → {symbol: book_value | None}.

    磁盘缓存优先 (6h TTL), 未命中的按配置走 EODHD/OpenBB (见模块说明),
    失败静默返回 None (调用方按「PB 缺失」降级, 不抛异常)。
    refresh=True 跳过缓存读取 (显式强制刷新, 穿透负缓存)。
    """
    out, missing = _load_cached(symbols, refresh=refresh, field="book_value")
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
    out, missing = _load_cached(symbols, refresh=refresh, field="market_cap")
    _fill_fundamentals(out, missing)
    return {s: out.get(s, {}).get("market_cap") for s in dict.fromkeys(symbols)}
