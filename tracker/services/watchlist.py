"""自选股用例层: 条目查询 + 视图组装 + 取数用例.

- 条目查询 (list_names / merge_entries / entries_for) 是纯函数,
  输入 load_watchlist() 产出的 dict, 不做 I/O。
- 视图组装 (build_watchlist_view / sort_watchlist / triggered_entries)
  消费 (entries, quotes), 不做 I/O。
- fetch_watchlist_view 是用例: 取数 (providers) → 规则 → 视图,
  被 cli/watchlist (list) 与 cli/report 复用。
"""

from __future__ import annotations

import math

import pandas as pd

from .. import prices
from ..providers.base import Quote
from ..storage import dedupe_watch_entries, parse_lists, normalize_watch_entry
from ..symbols import parse
from .rules import (
    METRIC_MCAP,
    METRIC_PB,
    STATUS_NO_DATA,
    STATUS_RANK,
    STATUS_WITHIN,
    entry_book_value,
    entry_market_cap,
    evaluate_thresholds,
    metric_for_entry,
    metric_value_for_quote,
    parse_thresholds,
)


def list_names(data: dict) -> list[str]:
    names: list[str] = []
    for e in (data or {}).get("watchlist", []):
        for n in e.get("lists", []) or []:
            if n and n not in names:
                names.append(n)
    return names or ["默认"]


def merge_entries(data: dict) -> list[dict]:
    """全部条目 (扁平, 一个代码一条).

    按归一后的 symbol 去重 (合并语义与 save_watchlist/load_watchlist 一致):
    外部编辑/旧文件可能在同代码上留多条, 读路径若不去重会把同一提醒重复
    计数 (report/snapshot/UI 均放大)。
    """
    entries = [
        e for e in (data or {}).get("watchlist", []) if str(e.get("symbol", "")).strip()
    ]
    return dedupe_watch_entries(entries)


def entries_for(data: dict, name: str | None = None) -> list[dict]:
    """按所属列表过滤条目; name 为空返回全部.

    name 支持逗号分隔多个列表 (与 add --list 一致), 命中任一即返回.
    返回前按代码去重 (合并语义同 save_watchlist), 避免外部编辑造成的
    同代码多条在过滤后依然重复计数。
    """
    if not name:
        return merge_entries(data)
    names = set(parse_lists(name))
    if not names:
        return merge_entries(data)
    matched = [
        e
        for e in (data or {}).get("watchlist", [])
        if names & set(e.get("lists", []) or [])
    ]
    return dedupe_watch_entries(matched)


def _note_str(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


def build_watchlist_view(
    entries, quotes: dict[str, Quote]
) -> tuple[pd.DataFrame, list[str]]:
    """条目 + 行情 → 视图.

    metric 决定基准值: price=现价 (默认), pb=现价/每股净资产, mcap=总市值
    (当地货币)。基本面条目的数据优先级: 条目手写 (离线可用) > 行情自带
    (长桥/akshare 实时口径) > fundamentals 数据源 (yfinance, 磁盘缓存 6h);
    该优先级与有无阈值无关 (仅跟踪条目同样按此取值并回落 BVPS/市值列)。
    指标缺失时 (crypto/无数据源) 不判状态, 记 issue 并保留配置列, 阈值列留空。
     fundamentals 拉取只发生在带阈值的 pb/mcap 条目上 (不为观察条目发请求)。
    """
    rows: list[dict] = []
    issues: list[str] = []
    # 条目先统一归一 (symbol 规范化 + 旧阈值键迁移 + 权威 type), 后续键与
    # 查找均以归一后的 symbol 为准 (别名 600519.SH → 600519.SS 不会取不到数)
    norm_entries = [
        ne
        for ne in (normalize_watch_entry(dict(e)) for e in entries)
        if str(ne.get("symbol", "")).strip()
    ]
    # 基本面兜底 (PB→BVPS / mcap→总市值): 只收集真正需要的代码, 按指标分桶,
    # 无阈值仅跟踪的条目不在此列 (不为观察条目发基本面请求)
    fund_syms = {
        str(ne["symbol"]).strip()
        for ne in norm_entries
        if metric_for_entry(ne) in (METRIC_PB, METRIC_MCAP)
        and any(v is not None for v in parse_thresholds(ne).values())
    }
    book_values: dict[str, float | None] = {}
    market_caps: dict[str, float | None] = {}

    # 已取过基本面的 (指标, 代码): 同一指标同代码只发一次请求 (进程内去重);
    # 两种指标各记各的 (PB 命中不得阻止 mcap 取数)
    fundamentals_done: set[tuple[str, str]] = set()

    def _fetch_fundamentals(metric: str, yahoo: str) -> None:
        """行情/条目均无该指标数据时批量拉取 (一次一批, 按指标+代码去重)."""
        if (metric, yahoo) in fundamentals_done:
            return
        fundamentals_done.add((metric, yahoo))
        from .. import fundamentals

        pool = book_values if metric == METRIC_PB else market_caps
        missing = [s for s in fund_syms if s not in pool]
        if not missing:
            return
        fetcher = (
            fundamentals.fetch_book_values
            if metric == METRIC_PB
            else fundamentals.fetch_market_caps
        )
        pool.update(fetcher(missing))

    for e in norm_entries:
        raw = str(e.get("symbol", "")).strip()
        try:
            p = parse(raw)
        except ValueError as err:
            # 与取数层 (orchestration._parse_all) 的错误格式保持一致
            # (symbol: 原因), 供 fetch_watchlist_view 拼接时去重
            issues.append(f"{raw}: {err}")
            continue
        metric = metric_for_entry(e)
        thresholds = parse_thresholds(e)
        has_thresholds = any(v is not None for v in thresholds.values())
        q = quotes.get(p.yahoo)
        if q is None or q.price is None or not math.isfinite(q.price):
            issues.append(f"{p.yahoo}: 行情缺失")
            continue
        price = q.price
        base_value = None
        effective_bv = None
        effective_mcap = None
        if metric == METRIC_PB:
            # BVPS 优先级: 条目手写 (离线可用) > 行情自带 (akshare 东财实时
            # 口径, 最准) > fundamentals 数据源 (yfinance 财报, 磁盘缓存 6h)。
            # 优先级与有无阈值无关: 仅跟踪的 pb 条目同样按 手写>行情 取值,
            # 并同样回落 effective_bv (book_value 列), 与预警条目口径一致
            bv = entry_book_value(e) or getattr(q, "book_value", None)
            if bv is None and has_thresholds:
                _fetch_fundamentals(metric, p.yahoo)
                bv = book_values.get(p.yahoo)
            effective_bv = bv
            base_value = (
                price / bv
                if bv is not None and math.isfinite(bv) and bv > 0
                else None
            )
            if base_value is None and has_thresholds:
                issues.append(f"{p.yahoo}: PB 缺失 (无每股净资产数据)")
        elif metric == METRIC_MCAP:
            # 总市值优先级: 条目手写 (离线可用) > 行情自带 (akshare 东财/
            # 长桥实时口径) > fundamentals 数据源 (yfinance, 磁盘缓存 6h)
            mc = entry_market_cap(e) or getattr(q, "market_cap", None)
            if mc is None and has_thresholds:
                _fetch_fundamentals(metric, p.yahoo)
                mc = market_caps.get(p.yahoo)
            effective_mcap = mc
            base_value = (
                mc if mc is not None and math.isfinite(mc) and mc > 0 else None
            )
            if base_value is None and has_thresholds:
                issues.append(f"{p.yahoo}: 市值缺失 (无总市值数据)")
        else:
            # price 基准: 直接取行情现值
            base_value = metric_value_for_quote(metric, q)
        if has_thresholds and base_value is None:
            # 有阈值但指标无数据 (PB/市值缺失): 不判区间内, 标记无数据
            status, dist = STATUS_NO_DATA, {}
        else:
            status, dist = evaluate_thresholds(thresholds, base_value if base_value is not None else price)
        row = {
            "symbol": p.yahoo,
            "name": q.name or "",
            "market": p.market_label,
            "currency": q.currency,
            "metric": metric,
            "price": price,
            "change_pct": q.change_pct,
            "source": getattr(q, "source", None),
            **thresholds,
            "status": status,
            "note": _note_str(e.get("note")),
            **dist,
        }
        if metric == METRIC_PB:
            # book_value 列 = 实际参与计算的 BVPS (手写/行情自带/数据源), 而非缓存值
            row["book_value"] = effective_bv
            row["base_value"] = base_value
        elif metric == METRIC_MCAP:
            row["market_cap"] = effective_mcap
            row["base_value"] = base_value
        rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        # 触发 = 越过阈值; 「指标无数据」(如 PB 缺失) 不算触发, 不打扰用户
        df["triggered"] = df["status"].ne(STATUS_WITHIN) & df["status"].ne(
            STATUS_NO_DATA
        )
        df = df.sort_values(
            ["triggered", "symbol"], ascending=[False, True]
        ).reset_index(drop=True)
    return df, issues


def triggered_entries(view: pd.DataFrame) -> pd.DataFrame:
    if view.empty or "triggered" not in view.columns:
        return view.iloc[0:0]
    return view[view["triggered"]]


def sort_watchlist(view: pd.DataFrame, mode: str = "default") -> pd.DataFrame:
    if view.empty:
        return view
    if mode == "severity":
        ranked = view.copy()
        ranked["status_rank"] = ranked["status"].map(STATUS_RANK)
        return ranked.sort_values(
            ["status_rank", "symbol"], ascending=[True, True]
        ).drop(columns=["status_rank"])
    if mode == "change_desc":
        return view.sort_values(["change_pct", "symbol"], ascending=[False, True])
    if mode == "change_asc":
        return view.sort_values(["change_pct", "symbol"], ascending=[True, True])
    return view.sort_values(["triggered", "symbol"], ascending=[False, True])


def fetch_watchlist_view(
    entries: list[dict],
    prefer_akshare: bool = False,
    use_ibkr: bool = False,
    use_longport: bool = False,
    crypto_source: str = "auto",
) -> tuple[pd.DataFrame, list[str]]:
    """用例: 取数 (providers) → 阈值规则 → 视图. watchlist list / report 复用."""
    symbols = [str(e["symbol"]) for e in entries]
    quotes, errors, notes = prices.get_quotes(
        symbols,
        prefer_akshare=prefer_akshare,
        use_ibkr=use_ibkr,
        use_longport=use_longport,
        crypto_source=crypto_source,
    )
    wview, wissues = build_watchlist_view(entries, quotes)
    # 无法解析的代码会在取数层 (errors) 与视图层 (wissues) 各记一次,
    # 拼接前按出现顺序去重, 避免同一问题重复打扰
    issues = list(
        dict.fromkeys(
            [f"{k}: {v}" for k, v in errors.items()] + wissues + notes
        )
    )
    return wview, issues
