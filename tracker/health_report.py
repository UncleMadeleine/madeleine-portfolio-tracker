"""健康观测聚合: summary / coverage / integrity / report / probe."""

from __future__ import annotations

import math
import time

from .health_record import (
    FUNDAMENTALS_TTL,
    MAX_EVENTS,
    RETENTION_SECONDS,
    SLOW_MS,
    _MIN_ATTEMPTS_FOR_DOWN,
    flush,
    label,
)

# 聚合
# ---------------------------------------------------------------------------


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def _status(attempts: int, ok: int, p95: float | None, kind: str) -> tuple[str, str]:
    """(状态, 原因). down/degraded/ok 只针对有调用的源."""
    if attempts <= 0:
        return "idle", "无调用记录"
    rate = ok / attempts
    # 单次失败多半是限流/瞬时网络, 不足以判 down (doctor 退出码 1 会惊动
    # 告警); 连续 3 次且成功率不足一半才视为真故障。
    if attempts >= _MIN_ATTEMPTS_FOR_DOWN and rate < 0.5:
        return "down", f"成功率 {rate:.0%}"
    if rate < 0.95:
        return "degraded", f"成功率 {rate:.0%}"
    slow = SLOW_MS.get(kind)
    if slow is not None and p95 is not None and p95 > slow:
        return "degraded", f"p95 {p95 / 1000:.1f}s"
    return "ok", ""


#: 无调用记录时也要展示的来源 (让「配了但没生效」可见)
_EXPECTED = (
    "ibkr",
    "longport",
    "eodhd",
    "yfinance",
    "akshare_em",
    "akshare_sina",
    "binance",
    "gate",
    "hyperliquid",
    "cfets",
)
_KIND_ORDER = {"quote": 0, "history": 1, "fx": 2, "calendar": 3, "other": 9}
_STATUS_ORDER = {"down": 0, "degraded": 1, "ok": 2, "idle": 3}


def summary(window_hours: float = 24.0, now: float | None = None) -> dict:
    """按 (来源, 类型) 聚合健康度; 只统计窗口内事件, 最近成功可早于窗口.

    返回结构 (全部 JSON 可序列化)::

        {
          "generated_at": 1730000000.0,
          "window_hours": 24.0,
          "overall": "ok" | "degraded" | "down" | "unknown",
          "totals": {"attempts": 12, "ok": 11, "fail": 1, ...},
          "sources": [{"source", "kind", "label", "attempts", "ok", "fail",
                       "success_rate", "p50_ms", "p95_ms", "last_ok_at",
                       "last_fail_at", "last_error", "status", "reason"}],
          "recent_failures": [...]
        }
    """
    from . import cache as cache_mod
    from .source_order import canonical_source

    flush()
    now = time.time() if now is None else float(now)
    window_hours = max(0.1, float(window_hours))
    since = now - window_hours * 3600.0
    events = cache_mod.get_health_events(now - RETENTION_SECONDS, MAX_EVENTS)

    groups: dict[tuple[str, str], dict] = {}
    for ev in events:
        # 旧记录里的 akshare/sina 与新 id 归并成同一行
        source = canonical_source(ev["source"])
        key = (source, ev["kind"])
        g = groups.setdefault(
            key,
            {
                "source": key[0],
                "kind": key[1],
                "attempts": 0,
                "ok": 0,
                "latencies": [],
                "last_ok_at": None,
                "last_fail_at": None,
                "last_error": None,
            },
        )
        if ev["ts"] >= since:
            g["attempts"] += 1
            if ev["ok"]:
                g["ok"] += 1
                if ev["latency_ms"] is not None:
                    g["latencies"].append(ev["latency_ms"])
            else:
                g["last_error"] = ev["error"] or "无错误信息"
        if ev["ok"]:
            if g["last_ok_at"] is None or ev["ts"] > g["last_ok_at"]:
                g["last_ok_at"] = ev["ts"]
        else:
            if g["last_fail_at"] is None or ev["ts"] > g["last_fail_at"]:
                g["last_fail_at"] = ev["ts"]
                g["last_error"] = ev["error"] or g["last_error"]

    rows: list[dict] = []
    for g in groups.values():
        latencies = g.pop("latencies")
        p50 = _percentile(latencies, 0.5)
        p95 = _percentile(latencies, 0.95)
        status, reason = _status(g["attempts"], g["ok"], p95, g["kind"])
        fail = g["attempts"] - g["ok"]
        rows.append(
            {
                "source": g["source"],
                "kind": g["kind"],
                "label": label(g["source"]),
                "attempts": g["attempts"],
                "ok": g["ok"],
                "fail": fail,
                "success_rate": (g["ok"] / g["attempts"]) if g["attempts"] else None,
                "p50_ms": p50,
                "p95_ms": p95,
                "last_ok_at": g["last_ok_at"],
                "last_fail_at": g["last_fail_at"],
                "last_error": g["last_error"],
                "status": status,
                "reason": reason,
            }
        )

    # 补上「有配置但窗口内没调用」的来源: 让未启用的可选源也可见
    seen = {(r["source"], r["kind"]) for r in rows}
    for source in _EXPECTED:
        if source == "cfets":
            kind = "fx"
        else:
            kind = "quote"
        if (source, kind) in seen:
            continue
        rows.append(
            {
                "source": source,
                "kind": kind,
                "label": label(source),
                "attempts": 0,
                "ok": 0,
                "fail": 0,
                "success_rate": None,
                "p50_ms": None,
                "p95_ms": None,
                "last_ok_at": None,
                "last_fail_at": None,
                "last_error": None,
                "status": "idle",
                "reason": "无调用记录",
            }
        )

    rows.sort(
        key=lambda r: (
            _STATUS_ORDER.get(r["status"], 9),
            _KIND_ORDER.get(r["kind"], 9),
            -r["attempts"],
            r["source"],
        )
    )

    attempts = sum(r["attempts"] for r in rows)
    ok = sum(r["ok"] for r in rows)
    statuses = {r["status"] for r in rows}
    if "down" in statuses:
        overall = "down"
    elif "degraded" in statuses:
        overall = "degraded"
    elif attempts > 0:
        overall = "ok"
    else:
        overall = "unknown"

    failures = [
        {
            "ts": ev["ts"],
            "age_seconds": max(0.0, now - ev["ts"]),
            "source": canonical_source(ev["source"]),
            "kind": ev["kind"],
            "symbol": ev["symbol"],
            "error": ev["error"] or "无错误信息",
        }
        for ev in events
        if not ev["ok"] and ev["ts"] >= since
    ]
    failures.sort(key=lambda x: x["ts"], reverse=True)
    return {
        "generated_at": now,
        "window_hours": window_hours,
        "overall": overall,
        "totals": {
            "attempts": attempts,
            "ok": ok,
            "fail": attempts - ok,
            "success_rate": (ok / attempts) if attempts else None,
        },
        "sources": rows,
        "recent_failures": failures[:20],
    }


# ---------------------------------------------------------------------------
# 组合视角的缓存覆盖
# ---------------------------------------------------------------------------

_DAY = 86400.0


def _quote_status(age: float | None) -> str:
    if age is None:
        return "missing"
    if age <= 300:
        return "fresh"
    if age <= 1800:
        return "stale"
    if age <= _DAY:
        return "old"
    return "expired"


def coverage(now: float | None = None) -> dict:
    """当前持仓/自选的行情、K线、汇率缓存覆盖情况.

    只读磁盘缓存, 不联网。``gaps`` 里是「页面/净值曲线可能已经开始不准」
    的条目: 行情缺失/超过陈旧期、持仓无 K线缓存、汇率缺失。
    """
    from . import cache as cache_mod
    from . import storage
    from .symbols import Market, is_currency_code, parse, valuation_currency

    now = time.time() if now is None else float(now)
    result: dict = {
        "symbols": {"total": 0, "holdings": 0, "watchlist": 0,
                    "fresh": 0, "stale": 0, "old": 0, "expired": 0,
                    "missing": 0},
        "fx": {"base": None, "needed": 0, "missing": []},
        "fundamentals": {
            "total": 0,
            "snapshot_fresh": 0,
            "snapshot_stale": 0,
            "snapshot_missing": 0,
            "filings_fresh": 0,
            "filings_stale": 0,
            "filings_missing": 0,
            "gaps": [],
        },
        "gaps": [],
        "items": [],
        "errors": [],
    }

    by_symbol: dict[str, dict] = {}
    try:
        # 显式传模块当前路径: 默认参数在 import 时求值, 运行期改 TRACKER_HOME
        # (测试隔离 / 嵌入式调用) 后无参调用仍会读旧路径。
        portfolio = storage.load_portfolio(storage.PORTFOLIO_PATH)
        base = portfolio.get("base_currency") or "CNY"
        result["fx"]["base"] = base
        for h in portfolio.get("holdings") or []:
            sym = str(h.get("symbol") or "").strip()
            if not sym:
                continue
            try:
                p = parse(sym)
            except ValueError:
                result["errors"].append(f"持仓代码无法解析: {sym}")
                continue
            by_symbol.setdefault(p.yahoo, {"symbol": p.yahoo, "holding": True,
                                           "watchlist": False, "currency": p.currency,
                                           "needs_fundamentals": True})
            by_symbol[p.yahoo]["holding"] = True
    except Exception as e:  # noqa: BLE001 - 数据文件损坏也要能出报告
        result["errors"].append(f"持仓文件不可用: {e}")
        base = "CNY"

    try:
        watch = storage.load_watchlist(storage.WATCHLIST_PATH)
        for e in watch.get("watchlist") or []:
            sym = str(e.get("symbol") or "").strip()
            if not sym:
                continue
            try:
                p = parse(sym)
            except ValueError:
                result["errors"].append(f"自选代码无法解析: {sym}")
                continue
            item = by_symbol.setdefault(
                p.yahoo,
                {"symbol": p.yahoo, "holding": False, "watchlist": False,
                 "currency": p.currency},
            )
            item["watchlist"] = True
            # 只有 PB/市值阈值条目需要估值快照; 纯价格自选不预取/不报缺口
            if (
                e.get("metric") in ("pb", "mcap")
                or e.get("pb") is not None
                or e.get("mcap") is not None
            ):
                item["needs_fundamentals"] = True
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"自选文件不可用: {e}")

    symbols = list(by_symbol)
    quotes, ages = cache_mod.get_cached_with_age(symbols, ttl=RETENTION_SECONDS)
    ohlc_ages = cache_mod.ohlc_freshness(symbols, now=now)

    # 行情自报币种优先于代码后缀推断: KAP.IL 这类 .IL 证券 parse() 默认 GBP,
    # 但 Yahoo 报价是 USD; 用后缀会误报 GBP 汇率缺口, 而保温只按实际
    # quote.currency 取汇率, 于是永远补不上这个「缺口」。
    for sym, meta in by_symbol.items():
        q = quotes.get(sym)
        ccy = getattr(q, "currency", None) if q is not None else None
        if ccy and is_currency_code(valuation_currency(ccy)):
            meta["currency"] = ccy

    # 基本面只对股票有意义 (crypto/index 无 PE/PB/财报): 估值快照与财报链接
    # 都是按需取数, 这里只看磁盘缓存年龄, 不联网、不触发补数。
    stock_symbols: list[str] = []
    for sym in symbols:
        try:
            if parse(sym).market not in (Market.CRYPTO, Market.INDEX):
                stock_symbols.append(sym)
        except ValueError:
            continue
    snap_ages = cache_mod.json_cache_freshness(
        [f"valuation_snapshot:{s}" for s in stock_symbols], now=now
    )
    filing_ages = cache_mod.json_cache_freshness(
        [f"filings:{s}" for s in stock_symbols], now=now
    )

    counts = result["symbols"]
    counts["total"] = len(symbols)
    counts["holdings"] = sum(1 for v in by_symbol.values() if v["holding"])
    counts["watchlist"] = sum(1 for v in by_symbol.values() if v["watchlist"])

    items: list[dict] = []
    for sym, meta in by_symbol.items():
        age = ages.get(sym)
        q_status = _quote_status(age)
        counts[q_status] = counts.get(q_status, 0) + 1
        o_age = ohlc_ages.get(sym)
        o_status = "missing" if o_age is None else (
            "fresh" if o_age <= 1800 else "stale"
        )
        item = {
            "symbol": sym,
            "name": quotes[sym].name if sym in quotes else None,
            "kind": "持仓+自选" if meta["holding"] and meta["watchlist"]
            else ("持仓" if meta["holding"] else "自选"),
            "currency": meta["currency"],
            "quote_age_seconds": age,
            "quote_status": q_status,
            "ohlc_age_seconds": o_age,
            "ohlc_status": o_status,
            "valuation_age_seconds": snap_ages.get(
                f"valuation_snapshot:{sym}"
            ),
            "filings_age_seconds": filing_ages.get(f"filings:{sym}"),
        }
        items.append(item)
        # 自选只在「缓存存在但过期」时算缺口: 自选列表很长, 没打开过就必然
        # 没有缓存, 全算缺口会让报告变成 600 行噪音。持仓缺行情则是实打实的
        # 估值/盈亏风险; 持仓缺 K线会影响净值回填。
        reasons = []
        if meta["holding"]:
            if q_status == "missing":
                reasons.append("无行情缓存")
            elif q_status in ("old", "expired"):
                hours = (age or 0) / 3600
                reasons.append(f"行情已 {hours:.1f} 小时未更新")
            if o_status == "missing":
                reasons.append("无 K线缓存 (净值回填会跳过)")
        elif q_status in ("old", "expired"):
            hours = (age or 0) / 3600
            reasons.append(f"行情已 {hours:.1f} 小时未更新")
        if reasons:
            result["gaps"].append(
                {
                    "symbol": sym,
                    "kind": item["kind"],
                    "reasons": reasons,
                    "quote_age_seconds": age,
                }
            )

    fund = result["fundamentals"]
    fund["total"] = len(stock_symbols)
    for sym in stock_symbols:
        meta = by_symbol[sym]
        s_age = snap_ages.get(f"valuation_snapshot:{sym}")
        f_age = filing_ages.get(f"filings:{sym}")
        if s_age is None:
            fund["snapshot_missing"] += 1
        elif s_age <= FUNDAMENTALS_TTL:
            fund["snapshot_fresh"] += 1
        else:
            fund["snapshot_stale"] += 1
        if f_age is None:
            fund["filings_missing"] += 1
        elif f_age <= FUNDAMENTALS_TTL:
            fund["filings_fresh"] += 1
        else:
            fund["filings_stale"] += 1

        # 估值快照：持仓缺了会影响「贵不贵」的判断, 算缺口; 自选只对
        # PB/市值阈值条目报「已过期」, 纯价格自选不预取也不算噪音。
        f_reasons = []
        if meta["holding"]:
            if s_age is None:
                f_reasons.append("未取过估值快照")
            elif s_age > FUNDAMENTALS_TTL:
                f_reasons.append(f"估值快照已 {s_age / 3600:.1f} 小时未更新")
        elif (
            meta.get("needs_fundamentals")
            and s_age is not None
            and s_age > FUNDAMENTALS_TTL
        ):
            f_reasons.append(f"估值快照已 {s_age / 3600:.1f} 小时未更新")
        if f_reasons:
            fund["gaps"].append(
                {
                    "symbol": sym,
                    "kind": (
                        "持仓+自选"
                        if meta["holding"] and meta["watchlist"]
                        else ("持仓" if meta["holding"] else "自选")
                    ),
                    "reasons": f_reasons,
                    "snapshot_age_seconds": s_age,
                }
            )

    items.sort(
        key=lambda x: (
            {"missing": 0, "expired": 1, "old": 2, "stale": 3, "fresh": 4}.get(
                x["quote_status"], 9
            ),
            x["symbol"],
        )
    )
    result["items"] = items
    result["gaps"].sort(key=lambda x: x["symbol"])
    result["fundamentals"]["gaps"].sort(key=lambda x: x["symbol"])

    # 汇率: 持仓/自选涉及的非基础货币都需要一条汇率; 缓存里没有即缺口
    # 指数/收益率的「币种」是点/% 等占位符, 不是可兑换货币: 不参与汇率缺口
    currencies = sorted(
        {
            valuation_currency(v["currency"])
            for v in by_symbol.values()
            if v.get("currency") and is_currency_code(valuation_currency(v["currency"]))
        }
    )
    base = valuation_currency(result["fx"]["base"] or "CNY")
    needed = [c for c in currencies if c and c != base]
    result["fx"]["needed"] = len(needed)
    try:
        rates, _fx_ages = cache_mod.get_fx_cached(
            base, needed, ttl=RETENTION_SECONDS
        )
        result["fx"]["missing"] = [c for c in needed if c not in rates]
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"汇率缓存不可用: {e}")

    return result


# ---------------------------------------------------------------------------
# 本地数据完整性
# ---------------------------------------------------------------------------


def integrity() -> dict:
    """本地数据文件 / 缓存库 / 目录权限体检; 返回 {ok, checks}."""
    from . import cache as cache_mod
    from . import paths, storage

    checks: list[dict] = []

    def add(name: str, status: str, detail: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    # 数据目录可写
    try:
        home = paths.data_home()
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".health_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        add("数据目录", "ok", str(home))
    except OSError as e:
        add("数据目录", "fail", f"不可写: {e}")

    # 三个 JSON 配置
    for name, loader in (
        ("持仓文件", storage.load_portfolio),
        ("自选文件", storage.load_watchlist),
        ("设置文件", storage.load_settings),
    ):
        try:
            # 与 coverage 同口径: 显式传当前路径, 尊重运行期 TRACKER_HOME
            path = {
                "持仓文件": storage.PORTFOLIO_PATH,
                "自选文件": storage.WATCHLIST_PATH,
                "设置文件": storage.SETTINGS_PATH,
            }[name]
            data = loader(path)
            if name == "持仓文件":
                n = len(data.get("holdings") or [])
            elif name == "自选文件":
                n = len(data.get("watchlist") or [])
            else:
                n = len(data)
            add(name, "ok", f"{n} 项")
        except Exception as e:  # noqa: BLE001 - 坏文件正是要报的问题
            add(name, "fail", str(e))

    # 持仓重复代码: 不是致命错误, 但会让「按代码市值」与提醒重复计数
    try:
        portfolio = storage.load_portfolio(storage.PORTFOLIO_PATH)
        seen: set[str] = set()
        dups: list[str] = []
        for h in portfolio.get("holdings") or []:
            sym = str(h.get("symbol") or "").strip()
            if sym in seen:
                dups.append(sym)
            seen.add(sym)
        if dups:
            add("持仓去重", "warn", f"重复代码: {', '.join(sorted(set(dups))[:5])}")
        else:
            add("持仓去重", "ok", "无重复")
    except Exception:  # noqa: BLE001 - 上面已报过文件错误
        pass

    # 缓存库结构
    check = cache_mod.quick_check()
    add("缓存数据库", "ok" if check == "ok" else "fail", check)

    # 负缓存条数: 全部源失败的代码在 TTL 内不会重试, 条数异常高说明上游在抖
    try:
        from .providers.orchestration import neg_stats

        neg = neg_stats()
        status = "warn" if neg["active"] > 0 else "ok"
        add("负缓存", status, f"{neg['active']} 个代码在 {neg['ttl_seconds']}s 内不重试")
    except Exception as e:  # noqa: BLE001
        add("负缓存", "warn", f"不可读: {e}")

    ok = all(c["status"] != "fail" for c in checks)
    return {"ok": ok, "checks": checks}


# ---------------------------------------------------------------------------
# 主动探测 (唯一联网入口, 仅供 `tracker doctor --probe`)
# ---------------------------------------------------------------------------

_DEFAULT_PROBES = (
    ("AAPL", "全球股票"),
    ("600519.SS", "A股"),
    ("0700.HK", "港股"),
    ("BTC-USD", "加密货币"),
)


def report(
    window_hours: float = 24.0,
    *,
    with_probe: bool = False,
    probe_symbols=None,
) -> dict:
    """完整体检报告: 健康 + 覆盖 + 完整性 + 缓存 + 保温状态.

    CLI ``doctor`` 与「数据健康」页共用同一份结构, 避免两个入口口径漂移。
    """
    from . import cache as cache_mod
    from .services import warm
    from .source_order import canonical_source

    data = summary(window_hours)
    coverage_data = coverage()
    integrity_data = integrity()
    # 旧行情缓存 (改名前的 akshare/sina) 与新 id 合并展示, 避免同一上游两行
    cache_sources: dict[str, dict[str, int]] = {}
    for src, info in cache_mod.source_distribution().items():
        merged = cache_sources.setdefault(canonical_source(src), {"total": 0, "fresh": 0})
        merged["total"] += int(info.get("total") or 0)
        merged["fresh"] += int(info.get("fresh") or 0)
    try:
        cache_info = cache_mod.info()
    except Exception as e:  # noqa: BLE001 - 页面不能因缓存库忙而整页崩
        cache_info = {
            "db": str(cache_mod.CACHE_DB),
            "ttl_seconds": cache_mod.CACHE_TTL,
            "stale_ttl_seconds": cache_mod.STALE_TTL,
            "total": 0, "fresh": 0, "stale": 0, "usable": 0,
            "fx_ttl_seconds": cache_mod.FX_FRESH_TTL,
            "fx_stale_ttl_seconds": cache_mod.FX_STALE_TTL,
            "fx_total": 0, "fx_fresh": 0,
            "ohlc_ttl_seconds": cache_mod.OHLC_TTL,
            "ohlc_total": 0, "ohlc_fresh": 0,
            "oldest_at": 0.0, "newest_at": 0.0,
            "series_total": 0, "series_fresh": 0,
            "calendar_total": 0,
            "calendar_ttl_seconds": cache_mod.CALENDAR_TTL,
            "size_bytes": 0,
            "error": str(e),
        }
    result = {
        "generated_at": time.time(),
        "window_hours": float(window_hours),
        "health": data,
        "coverage": coverage_data,
        "integrity": integrity_data,
        "cache": cache_info,
        "cache_sources": cache_sources,
        "warm": warm.status(),
    }
    if with_probe:
        result["probe"] = probe(probe_symbols)

    overall = data.get("overall") or "unknown"
    if not integrity_data.get("ok", True):
        overall = "down"
    elif overall in ("ok", "unknown") and coverage_data.get("gaps"):
        # 数据本身有缺口: 页面还能开, 但净值/盈亏可能已经不准。
        # 窗口内没有回源记录 (unknown) 不代表数据齐全: 冷缓存同样要降级。
        overall = "degraded"
    if with_probe and not result["probe"].get("ok", True):
        overall = "down"
    result["overall"] = overall
    return result


def probe(symbols=None, fx_pairs=(("USD", "CNY"),)) -> dict:
    """主动打一次真实接口, 逐项返回 ok/耗时/错误; 慢, 需显式触发."""
    import time as _time

    from . import fx as fx_mod
    from . import prices

    targets = list(symbols) if symbols else [s for s, _ in _DEFAULT_PROBES]
    results: list[dict] = []
    for symbol in targets:
        started = _time.perf_counter()
        try:
            quote = prices.get_quote(symbol, refresh=True)
            elapsed = (_time.perf_counter() - started) * 1000
            results.append(
                {
                    "symbol": symbol,
                    "kind": "quote",
                    "ok": True,
                    "elapsed_ms": elapsed,
                    "source": getattr(quote, "source", None),
                    "detail": f"{quote.price:g} {quote.currency}",
                }
            )
        except Exception as e:  # noqa: BLE001 - 探测结果就是错误本身
            elapsed = (_time.perf_counter() - started) * 1000
            results.append(
                {
                    "symbol": symbol,
                    "kind": "quote",
                    "ok": False,
                    "elapsed_ms": elapsed,
                    "source": None,
                    "detail": str(e) or repr(e),
                }
            )

    for src, dst in fx_pairs:
        started = _time.perf_counter()
        try:
            rate = fx_mod.get_rate(src, dst)
            elapsed = (_time.perf_counter() - started) * 1000
            results.append(
                {
                    "symbol": f"{src}/{dst}",
                    "kind": "fx",
                    "ok": rate is not None,
                    "elapsed_ms": elapsed,
                    "source": None,
                    "detail": f"{rate:.6g}" if rate else "未取到汇率",
                }
            )
        except Exception as e:  # noqa: BLE001
            elapsed = (_time.perf_counter() - started) * 1000
            results.append(
                {
                    "symbol": f"{src}/{dst}",
                    "kind": "fx",
                    "ok": False,
                    "elapsed_ms": elapsed,
                    "source": None,
                    "detail": str(e) or repr(e),
                }
            )

    return {
        "ok": all(r["ok"] for r in results),
        "results": results,
    }
