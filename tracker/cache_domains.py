"""磁盘缓存域读写: 行情 / K线 / 基本面 / 汇率 / 日历 / 通用序列与 JSON.

从 cache.py 拆出: 连接/建表/重试等底层原语仍在 cache.py; 本模块通过
``_cache`` 模块对象动态调用它们, 保持测试对 CACHE_DB / _read_one /
_execute_write 等 patch 点生效。公开函数由 cache.py 再导出。
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import TYPE_CHECKING

import pandas as pd

from .symbols import Market, is_currency_code, parse
from .util import finite_float as _finite_opt


class _CacheModuleProxy:
    """对 tracker.cache 的惰性引用.

    cache.py 在文件末尾从本模块再导出域读写函数. 若这里在模块导入阶段
    直接 ``from . import cache``, 两条导入路径会互相等待:
    ``import tracker.cache_domains`` → cache.py → ``from .cache_domains
    import ...`` (此时本模块尚未执行完) → ImportError.

    改为调用时才解析 cache 模块: 直接导入本模块不会触发 cache.py;
    正常 ``import tracker.cache`` 时本模块可完整加载, 末尾再导出也不成环。
    """

    def __getattr__(self, name: str):
        from . import cache

        return getattr(cache, name)


_cache = _CacheModuleProxy()

if TYPE_CHECKING:
    from .providers.base import Quote

# ---------- K线 (OHLC) 缓存 ----------


def get_ohlc_cached(
    symbol: str, months: int, ttl: int | None = None
) -> pd.DataFrame | None:
    """读取缓存的日线 OHLC; 未命中 / 过期 / 载荷损坏返回 None."""
    if ttl is None:
        ttl = _cache.OHLC_TTL
    payload = _cache._read_payload(
        "ohlc_cache",
        "symbol=? AND months=?",
        (symbol, int(months)),
        ttl,
    )
    if payload is None:
        return None
    try:
        df = pd.DataFrame(json.loads(payload))
        if df.empty:
            return None
        if not {"date", "open", "high", "low", "close"}.issubset(df.columns):
            # 旧版/损坏但仍是合法 JSON 的载荷 (只剩 date 等) 若当缓存命中,
            # 调用方会跳过网络重取并在图表/回测处 KeyError。
            return None
        df["date"] = pd.to_datetime(df["date"])
        cols = [
            c
            for c in ("date", "open", "high", "low", "close", "volume")
            if c in df.columns
        ]
        return df[cols]
    except Exception:
        return None


def set_ohlc_cached(symbol: str, months: int, df: pd.DataFrame) -> None:
    """写入日线 OHLC 缓存 (整段 JSON 载荷, 按 symbol+months 覆盖)."""
    if df is None or df.empty:
        return
    _cache._ensure_db()
    recs = df.copy()
    recs["date"] = pd.to_datetime(recs["date"]).dt.strftime("%Y-%m-%d")
    cols = [
        c
        for c in ("date", "open", "high", "low", "close", "volume")
        if c in recs.columns
    ]
    payload = json.dumps(recs[cols].to_dict(orient="records"))
    _cache._execute_write(
        """
        INSERT INTO ohlc_cache (symbol, months, payload, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol, months) DO UPDATE SET
            payload=excluded.payload, fetched_at=excluded.fetched_at
        """,
        (symbol, int(months), payload, time.time()),
    )


# ---------- 基本面 (每股净资产) 缓存 ----------


def _valid_fundamental(v) -> float | None:
    """基本面数值归一: None/非有限/≤0 → None (与写入侧 _valid 同口径)."""
    f = _finite_opt(v)
    return f if f is not None and f > 0 else None


def get_fundamentals_meta(
    symbols: list[str], ttl: int = 21600
) -> dict[str, dict]:
    """批量读取基本面缓存及「指标已确认」标记 (内部使用).

    返回 ``{symbol: {book_value, market_cap, book_value_checked,
    market_cap_checked}}``。``*_checked`` 表示该指标已由数据源确认 (包括
    确认缺失), 用于区分「没取过」和「取过但没有」。驱动层据此只补本次真正
    需要的指标, 又不会把已确认的负缓存当成未命中反复重试。
    """
    unique = list(dict.fromkeys(symbols))
    if not unique:
        return {}
    _cache._ensure_db()
    now = time.time()
    out: dict[str, dict] = {}
    for chunk in _cache._chunks(unique):
        rows = _cache._read_all(
            f"SELECT symbol, book_value, market_cap, "
            f"book_value_checked, market_cap_checked FROM fundamentals "
            f"WHERE symbol IN ({','.join('?' * len(chunk))}) AND fetched_at > ?",
            [*chunk, now - ttl],
        )
        for sym, bv, mc, bv_checked, mc_checked in rows:
            out[sym] = {
                "book_value": _valid_fundamental(bv),
                "market_cap": _valid_fundamental(mc),
                "book_value_checked": bool(bv_checked),
                "market_cap_checked": bool(mc_checked),
            }
    return out


def get_fundamentals_cached(
    symbols: list[str], ttl: int = 21600
) -> dict[str, dict]:
    """批量读取基本面缓存; 只返回未过期项 (未命中/过期不在结果里).

    与逐条 get_fundamental_cached 语义一致, 但一次查询覆盖全部代码 ——
    自选 600+ 时逐条调用要付 600 次连接/建表检查 (~0.2s)。
    负缓存行 (两个指标均为 None) 同样返回, 调用方据此避免 TTL 内重试。
    """
    return {
        sym: {
            "book_value": meta["book_value"],
            "market_cap": meta["market_cap"],
        }
        for sym, meta in get_fundamentals_meta(symbols, ttl=ttl).items()
    }


def get_fundamental_cached(symbol: str, ttl: int = 21600) -> dict | None:
    """读取缓存的基本面指标; 未命中/过期返回 None.

    负缓存 (已确认无有效值的标的) 命中返回
    {"book_value": None, "market_cap": None}, 与「未缓存」(None) 区分,
    避免 TTL 内重复网络请求。
    """
    return get_fundamentals_cached([symbol], ttl=ttl).get(symbol)


def set_fundamental_cached(
    symbol: str,
    book_value: float | None,
    market_cap: float | None = None,
    *,
    book_value_checked: bool | None = None,
    market_cap_checked: bool | None = None,
) -> None:
    """写入基本面缓存; 指标无效 (≤0/NaN) 时也落库 (负缓存, 6h 不重试).

    market_cap 为可选增量: 只补 BVPS 的旧调用方写 None, 由后续 mcap 请求覆盖。
    两个指标各自独立 COALESCE: 本次为 None 的字段保留库中旧值, 不被冲成
    NULL —— 否则 yfinance 只回 marketCap 的那次会把已缓存的 BVPS 抹掉,
    之后 6 小时 PB 提醒全部误报「PB 缺失」(且负缓存阻止重试)。

    ``book_value_checked`` / ``market_cap_checked`` 区分「本次没提供该字段」
    (False, 保留旧值) 与「数据源确认没有该字段」 (True, 写入 NULL 并标记
    已确认)。取数管线拿到完整结果时显式传 True, 手动只补一个指标的旧调用
    方保持默认 (值非 None 时视为已确认, None 时保留旧值)。
    fetched_at 刷新即整行 TTL 重新计期 (负缓存语义保留)。
    """
    _cache._ensure_db()
    now = time.time()

    # 与 storage._clean_rows 同口径: ±Infinity 同样不落库 (json 不认, 且
    # 6h 负缓存期内会在视图/snapshot --json 里炸出非法 JSON)
    bv = _valid_fundamental(book_value)
    mc = _valid_fundamental(market_cap)
    bv_checked = (
        book_value is not None if book_value_checked is None else book_value_checked
    )
    mc_checked = (
        market_cap is not None
        if market_cap_checked is None
        else market_cap_checked
    )
    _cache._execute_write(
        """
        INSERT INTO fundamentals
            (symbol, book_value, market_cap, fetched_at,
             book_value_checked, market_cap_checked)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            book_value=CASE
                WHEN excluded.book_value_checked
                THEN excluded.book_value
                ELSE COALESCE(excluded.book_value, fundamentals.book_value)
            END,
            market_cap=CASE
                WHEN excluded.market_cap_checked
                THEN excluded.market_cap
                ELSE COALESCE(excluded.market_cap, fundamentals.market_cap)
            END,
            book_value_checked=CASE
                WHEN excluded.book_value_checked THEN 1
                ELSE fundamentals.book_value_checked
            END,
            market_cap_checked=CASE
                WHEN excluded.market_cap_checked THEN 1
                ELSE fundamentals.market_cap_checked
            END,
            fetched_at=excluded.fetched_at
        """,
        (symbol, bv, mc, now, int(bool(bv_checked)), int(bool(mc_checked))),
    )


def _normalize_quote_currency(symbol: str, currency) -> str | None:
    """行情币种归一: 非法占位符回退代码推导的权威币种.

    东财 A 股 f172 返回 "-", 旧版本把 "-" 写进了行情缓存。这里读取时兜底,
    既修复历史脏数据, 也避免把 "-" 交给汇率链 (会拼出 -CNY=X 伪货币对)。
    无法从代码推导币种时返回 None, 调用方按脏数据跳过 (下次回源覆盖)。
    """
    ccy = str(currency or "").strip().upper()
    if is_currency_code(ccy):
        return ccy
    try:
        return parse(symbol).currency
    except ValueError:
        return None


def get_cached_with_age(
    symbols: list[str], ttl: int | None = None
) -> tuple[dict[str, "Quote"], dict[str, float]]:
    """返回 (Symbol→Quote, Symbol→age秒) 两个字典; 未命中/过期/脏数据不在返回值中.

    age = now - fetched_at (秒). 首屏 stale-while-revalidate 按 age 分档:
    age ≤ CACHE_TTL 视为新鲜 (渲染即最终结果), 超过则视为陈旧 (先渲染,
    同时后台重取)。ttl 控制最长可接受年龄 (默认陈旧期)。
    """
    from .providers.base import Quote

    if ttl is None:
        ttl = _cache.STALE_TTL
    if not symbols:
        return {}, {}

    _cache._ensure_db()
    now = time.time()
    hits: dict[str, Quote] = {}
    ages: dict[str, float] = {}
    for chunk in _cache._chunks(list(symbols)):
        rows = _cache._read_all(
            f"SELECT symbol, name, price, prev_close, change_pct, currency, book_value, market_cap, pe_dynamic, source, fetched_at "
            f"FROM quotes "
            f"WHERE symbol IN ({','.join('?' * len(chunk))}) AND fetched_at > ?",
            [*chunk, now - ttl],
        )
        for sym, name, price, prev, chg, ccy, bv, mc, pe_dyn, src, fetched_at in rows:
            if price is not None and math.isfinite(price) and price > 0:
                quote_ccy = _normalize_quote_currency(sym, ccy)
                if quote_ccy is None:
                    continue
                hits[sym] = Quote(
                    symbol=sym,
                    name=name,
                    price=price,
                    prev_close=_finite_opt(prev),
                    change_pct=_finite_opt(chg),
                    currency=quote_ccy,
                    book_value=_finite_opt(bv),
                    market_cap=_finite_opt(mc),
                    pe_dynamic=_finite_opt(pe_dyn),
                    source=str(src) if src else None,
                )
                ages[sym] = max(0.0, now - float(fetched_at or now))
    return hits, ages


def get_cached(symbols: list[str], ttl: int | None = None) -> dict[str, "Quote"]:
    """返回缓存命中的 Symbol→Quote 字典, 未命中者不在返回值中."""
    if ttl is None:
        ttl = _cache.CACHE_TTL
    hits, _ages = get_cached_with_age(symbols, ttl=ttl)
    return hits


def set_cached(quotes: dict[str, "Quote"]) -> None:
    """将 Quote 写入缓存, 更新 fetched_at. 过滤掉 price<=0 的脏数据.

    book_value/market_cap 与 set_fundamental_cached 同口径用 COALESCE 保留
    库中旧值: yfinance/OpenBB 路径不填这两个指标 (恒 None), 无条件覆盖会把
    上一轮 akshare/长桥写进去的 BVPS 与总市值抹成 NULL, 首屏 PB/市值提醒
    随之拿不到值。
    """
    if not quotes:
        return
    _cache._ensure_db()
    now = time.time()
    rows: list[tuple] = []
    for q in quotes.values():
        if q.price is None or not math.isfinite(q.price) or q.price <= 0:
            continue
        quote_ccy = _normalize_quote_currency(q.symbol, q.currency)
        if quote_ccy is None:
            # 币种无法识别/推导: 不落库, 避免污染下一轮缓存读取与汇率链
            continue
        rows.append(
            (
                q.symbol,
                q.name,
                q.price,
                _finite_opt(q.prev_close),
                _finite_opt(q.change_pct),
                quote_ccy,
                _finite_opt(q.book_value),
                _finite_opt(getattr(q, "market_cap", None)),
                _finite_opt(getattr(q, "pe_dynamic", None)),
                getattr(q, "source", None),
                now,
            )
        )
    _cache._execute_write(
        """
        INSERT INTO quotes (symbol, name, price, prev_close, change_pct, currency, book_value, market_cap, pe_dynamic, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            -- 名称与基本面字段同样 COALESCE: yfinance/OpenBB 路径常回 name=None,
            -- 无条件覆盖会把上一轮 akshare/长桥/条目写入的中文名抹成 NULL
            -- (自选表/组合明细随后只剩代码)。空串也视为缺失。
            name=COALESCE(NULLIF(excluded.name, ''), quotes.name),
            price=excluded.price,
            prev_close=excluded.prev_close, change_pct=excluded.change_pct,
            currency=excluded.currency, book_value=COALESCE(excluded.book_value, quotes.book_value),
            market_cap=COALESCE(excluded.market_cap, quotes.market_cap),
            -- 动态 PE: akshare/东财是权威口径, 它返回 "-" 时要清掉旧值;
            -- 其它源 (yfinance/长桥) 不提供该字段, 不能把东财值抹成 NULL。
            pe_dynamic=CASE
                WHEN excluded.source IN ('akshare_em', 'akshare') THEN excluded.pe_dynamic
                ELSE COALESCE(excluded.pe_dynamic, quotes.pe_dynamic)
            END,
            source=excluded.source, fetched_at=excluded.fetched_at
        """,
        rows,
        many=True,
    )


# ---------- 汇率 (即期) 缓存 ----------


def get_fx_cached(
    base: str, currencies, ttl: int | None = None
) -> tuple[dict[str, float], dict[str, float]]:
    """返回 (currency→rate, currency→age秒); base 自身/未命中/过期不在返回值中.

    与 get_cached_with_age 同构: 首屏按 age 分新鲜/陈旧两档处理。
    """
    if ttl is None:
        ttl = _cache.FX_STALE_TTL
    base = str(base).upper()
    curs = [
        c
        for c in dict.fromkeys(str(x).upper() for x in currencies)
        if c != base and is_currency_code(c)
    ]
    if not curs:
        return {}, {}

    _cache._ensure_db()
    now = time.time()
    rates: dict[str, float] = {}
    ages: dict[str, float] = {}
    for chunk in _cache._chunks(curs):
        rows = _cache._read_all(
            f"SELECT currency, rate, fetched_at FROM fx_rates "
            f"WHERE base=? AND currency IN ({','.join('?' * len(chunk))}) AND fetched_at > ?",
            [base, *chunk, now - ttl],
        )
        for ccy, rate, fetched_at in rows:
            if rate is None or not math.isfinite(rate) or rate <= 0:
                continue
            rates[ccy] = float(rate)
            ages[ccy] = max(0.0, now - float(fetched_at or now))
    return rates, ages


def set_fx_cached(base: str, rates: dict[str, float]) -> None:
    """写入汇率缓存 (base→currency), 刷新 fetched_at; 无效值 (<=0) 与 base 跳过."""
    base = str(base).upper()
    rows = []
    for c, r in rates.items():
        ccy = str(c).upper()
        # 非法币种占位符 ("-" / "NONE" / 空串) 不是货币代码, 落库后会被
        # get_fx_cached 当成真实汇率复用 (旧版本曾把 "-" 写成 CNY 汇率)。
        if not is_currency_code(ccy) or ccy == base:
            continue
        try:
            rate = float(r)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and rate > 0:
            rows.append((base, ccy, rate, time.time()))
    if not rows:
        return
    _cache._execute_write(
        """
        INSERT INTO fx_rates (base, currency, rate, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(base, currency) DO UPDATE SET
            rate=excluded.rate, fetched_at=excluded.fetched_at
        """,
        rows,
        many=True,
    )


def _clear_table(table: str) -> int:
    """清空单张缓存表; 失败统一转成可读 RuntimeError (设置页据此提示).

    运行时损坏 (DatabaseError: file is not a database) 与截断后缺表
    (no such table) 都必须摘掉 _db_ready 并尝试一次重建, 否则本次失败后
    进程内会一直认为库已就绪, 自愈路径永远不触发。
    """

    def _delete():
        with _cache._lock, _cache._connection() as con:
            n = con.execute(f"DELETE FROM {table}").rowcount
            con.commit()
            return n

    try:
        return _cache._with_schema_retry(_delete)
    except sqlite3.Error as e:
        raise RuntimeError(f"缓存数据库忙或不可用 ({e}); 稍后重试") from e


def clear_fx() -> int:
    """清汇率磁盘缓存: 数据源偏好 (IBKR 开关) 变化后强制按新链路重取."""
    return _clear_table("fx_rates")


def clear_quotes() -> int:
    """只清实时行情缓存 (保留 K线/基本面): 数据源偏好切换后强制按新源链重取."""
    return _clear_table("quotes")


def clear_ohlc() -> int:
    """只清 K线(OHLC) 磁盘缓存: 数据源偏好切换后强制按新源链重取K线.

    缓存键含 symbol + 窗口 + 源偏好/EODHD/source_order 签名, 但设置页切换
    开关或拖动顺序后仍主动全清: 既要清跨版本/旧键的历史数据, 也要让页面的
    数据来源标记随新链路刷新。与 clear_quotes 配套, 由设置页在源变化时调用
    (clear 全清亦可, 但会连基本面一起清)。
    """
    return _clear_table("ohlc_cache")


def clear_fundamentals() -> int:
    """只清基本面缓存 (PB/市值): EODHD fundamentals 角色切换后重取."""
    return _clear_table("fundamentals")


def prune_orphans(*, dry_run: bool = False) -> dict:
    """删除当前持仓/自选之外的孤儿缓存 (可再生数据).

    覆盖:
      - quotes / fundamentals / series_cache(shares_full:*) / calendar_cache(g:*)
      - ohlc_cache (键可能是 ``SYMBOL|源签名``)
      - json_cache (valuation_snapshot:* / filings:*)

    保守保留:
      - 指数缓存 (``index:IX.*``) 与批量日历键 (``cn:*``): 不属于持仓/自选域
      - 无法解析的键、``g:UNKNOWN_OK`` 等哨兵
      - 持仓/自选为空或任一文件读取失败时整体跳过, 避免误删全部缓存
    """
    from . import storage

    scope: set[str] = set()
    for label, loader, path, key in (
        ("持仓", storage.load_portfolio, storage.PORTFOLIO_PATH, "holdings"),
        ("自选", storage.load_watchlist, storage.WATCHLIST_PATH, "watchlist"),
    ):
        try:
            data = loader(path)
        except Exception as e:  # noqa: BLE001 - 任一侧不可读时宁可不清
            return {
                "skipped": True,
                "reason": f"{label}文件读取失败, 未删除: {e}",
                "scope_size": len(scope),
                "deleted": 0,
                "tables": {},
            }
        rows = data.get(key) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = row.get("symbol")
            if not sym:
                continue
            try:
                scope.add(parse(str(sym)).yahoo)
            except (TypeError, ValueError):
                continue
    if not scope:
        return {
            "skipped": True,
            "reason": "当前持仓/自选为空, 为安全起见未删除",
            "scope_size": 0,
            "deleted": 0,
            "tables": {},
        }

    def _keep(sym: str) -> bool:
        try:
            p = parse(sym)
        except (TypeError, ValueError):
            return True  # 无法解析的键保守保留
        return p.market is Market.INDEX or p.yahoo in scope

    def _run():
        with _cache._lock, _cache._connection() as con:
            tables: dict[str, dict] = {}

            def _collect(sql: str):
                return con.execute(sql).fetchall()

            def _entry(keys: list, **extra) -> dict:
                item = {
                    "orphans": len(keys),
                    "deleted": 0 if dry_run else len(keys),
                    "sample": sorted(str(k) for k in keys)[:20],
                }
                item.update(extra)
                return item

            q_orph = [
                r[0]
                for r in _collect("SELECT symbol FROM quotes")
                if not _keep(r[0])
            ]
            tables["quotes"] = _entry(q_orph)

            ohlc_rows = _collect("SELECT symbol, months FROM ohlc_cache")
            ohlc_orph: list[tuple[str, int]] = []
            index_kept = 0
            for sym, months in ohlc_rows:
                base = str(sym).split("|", 1)[0]
                if base.startswith("index:"):
                    index_kept += 1
                    continue
                if not _keep(base):
                    ohlc_orph.append((str(sym), int(months)))
            tables["ohlc_cache"] = _entry(
                [f"{s} ({m}m)" for s, m in ohlc_orph],
                kept_index=index_kept,
            )

            json_orph = []
            for (key,) in _collect("SELECT key FROM json_cache"):
                prefix, sep, sym = str(key).partition(":")
                if prefix not in ("valuation_snapshot", "filings") or not sep:
                    continue
                if not _keep(sym):
                    json_orph.append(str(key))
            tables["json_cache"] = _entry(json_orph)

            fund_orph = [
                r[0]
                for r in _collect("SELECT symbol FROM fundamentals")
                if not _keep(r[0])
            ]
            tables["fundamentals"] = _entry(fund_orph)

            series_orph = []
            for (key,) in _collect("SELECT key FROM series_cache"):
                key = str(key)
                prefix, sep, sym = key.partition(":")
                if prefix != "shares_full" or not sep:
                    continue
                if not _keep(sym):
                    series_orph.append(key)
            tables["series_cache"] = _entry(series_orph)

            cal_orph = []
            for (key,) in _collect("SELECT key FROM calendar_cache"):
                key = str(key)
                if not key.startswith("g:"):
                    continue  # cn:* 整批缓存不属于单代码
                if key[2:].upper() == "UNKNOWN_OK":
                    continue  # 哨兵键 (无有效代码) 不属于任何代码
                if not _keep(key[2:]):
                    cal_orph.append(key)
            tables["calendar_cache"] = _entry(cal_orph)

            if not dry_run:
                if q_orph:
                    con.executemany(
                        "DELETE FROM quotes WHERE symbol=?", [(s,) for s in q_orph]
                    )
                if ohlc_orph:
                    con.executemany(
                        "DELETE FROM ohlc_cache WHERE symbol=? AND months=?",
                        ohlc_orph,
                    )
                if json_orph:
                    con.executemany(
                        "DELETE FROM json_cache WHERE key=?", [(k,) for k in json_orph]
                    )
                if fund_orph:
                    con.executemany(
                        "DELETE FROM fundamentals WHERE symbol=?",
                        [(s,) for s in fund_orph],
                    )
                if series_orph:
                    con.executemany(
                        "DELETE FROM series_cache WHERE key=?",
                        [(k,) for k in series_orph],
                    )
                if cal_orph:
                    con.executemany(
                        "DELETE FROM calendar_cache WHERE key=?",
                        [(k,) for k in cal_orph],
                    )
                con.commit()

            deleted = sum(t["deleted"] for t in tables.values())
            would_delete = sum(t["orphans"] for t in tables.values())
            return {
                "skipped": False,
                "scope_size": len(scope),
                "dry_run": dry_run,
                "deleted": deleted,
                "would_delete": would_delete,
                "tables": tables,
            }

    try:
        return _cache._with_schema_retry(_run)
    except sqlite3.Error as e:
        raise RuntimeError(f"缓存数据库忙或不可用 ({e}); 稍后重试") from e


# ---------- 财经日历 (财报/分红事件) 缓存 ----------


def _calendar_row_from_payload(payload: str, fetched_at: float):
    try:
        data = json.loads(payload)
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            return None
    except Exception:
        return None
    return {"events": events, "fetched_at": fetched_at}


def get_calendar_rows(keys: list[str]) -> dict[str, dict]:
    """批量读取日历缓存行; 未命中/载荷损坏的 key 不在结果里.

    与 get_calendar_row 语义一致 (不套 WHERE fetched_at, TTL 交给调用方),
    但一次查询覆盖整批 key —— 日历页逐票循环时避免 N 次连接/建表检查。
    """
    unique = list(dict.fromkeys(keys))
    if not unique:
        return {}
    _cache._ensure_db()
    out: dict[str, dict] = {}
    for chunk in _cache._chunks(unique):
        rows = _cache._read_all(
            f"SELECT key, payload, fetched_at FROM calendar_cache "
            f"WHERE key IN ({','.join('?' * len(chunk))})",
            list(chunk),
        )
        for key, payload, fetched_at in rows:
            row = _calendar_row_from_payload(payload, fetched_at)
            if row is not None:
                out[key] = row
    return out


def get_calendar_row(key: str) -> dict | None:
    """读取日历缓存行; 未命中/载荷损坏返回 None.

    返回 {"events": [...], "fetched_at": float}。TTL 判定留给调用方:
    单票缓存按「载荷是否为空」区分正/负缓存两个 TTL (events.py), 整批缓
    存统一 6h —— 这里不套 WHERE fetched_at, 只给原始时间戳。
    """
    return get_calendar_rows([key]).get(key)


def set_calendar_row(key: str, events: list) -> None:
    """写入日历缓存行; 空事件列表同样落库 (单票负缓存依赖空载荷)."""
    payload = json.dumps({"events": events}, ensure_ascii=False, default=str)
    _cache._write_payload("calendar_cache", key, payload)


# ---------- 通用序列缓存 (历史市值 em / 历史股本 shares 用) ----------


def get_series_cached(key: str, ttl: int) -> pd.DataFrame | None:
    """读取缓存的 (date, close) 序列; 未命中/过期/载荷损坏返回 None.

    与 OHLC 缓存同构: 整段 JSON 载荷按 key 覆盖, TTL 过期重取。
    """
    payload = _cache._read_payload("series_cache", "key=?", (key,), ttl)
    if payload is None:
        return None
    try:
        df = pd.DataFrame(json.loads(payload))
        if df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def set_series_cached(key: str, df: pd.DataFrame) -> None:
    """写入 (date, close) 序列缓存; 空表跳过 (不落库空载荷)."""
    if df is None or df.empty:
        return
    _cache._ensure_db()
    recs = df[["date", "close"]].copy()
    recs["date"] = pd.to_datetime(recs["date"]).dt.strftime("%Y-%m-%d")
    _cache._write_payload("series_cache", key, json.dumps(recs.to_dict(orient="records")))


# ---------- 通用 JSON 快照缓存 (估值快照 / 财报链接) ----------


def get_json_cached(key: str, ttl: float):
    """读取 JSON 快照缓存; 未命中/过期/载荷损坏返回 None.

    快照属低频数据 (估值/财报 6h), 与行情/序列缓存共库但独立表;
    读失败按无缓存处理, 不影响上层重新取数。
    """
    payload = _cache._read_payload("json_cache", "key=?", (key,), ttl)
    if payload is None:
        return None
    try:
        return json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def set_json_cached(key: str, value) -> None:
    """写入 JSON 快照缓存 (调用方负责保证 value 可 JSON 序列化)."""
    _cache._ensure_db()
    try:
        payload = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return
    _cache._write_payload("json_cache", key, payload)


def clear_json_cache(prefix: str = "") -> int:
    """按 key 前缀清 JSON 快照缓存; prefix 为空时清空整表."""
    _cache._ensure_db()

    def _delete() -> int:
        with _cache._lock, _cache._connection() as con:
            if prefix:
                # 前缀里的 % / _ 按字面量处理, 避免 "valuation:" 这类 key
                # 被 LIKE 通配符误伤到别的业务前缀。
                escaped = (
                    prefix.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                n = con.execute(
                    "DELETE FROM json_cache WHERE key LIKE ? ESCAPE '\\'",
                    (escaped + "%",),
                ).rowcount
            else:
                n = con.execute("DELETE FROM json_cache").rowcount
            con.commit()
            return n

    try:
        return _cache._with_schema_retry(_delete)
    except sqlite3.Error:
        return 0


