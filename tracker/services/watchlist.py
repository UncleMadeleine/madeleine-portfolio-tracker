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
from ..storage import parse_lists, normalize_watch_entry
from ..symbols import parse
from .rules import (
    METRIC_PB,
    METRIC_PRICE,
    STATUS_NO_DATA,
    STATUS_RANK,
    STATUS_WITHIN,
    entry_book_value,
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
    """全部条目 (扁平, 一个代码一条, 天然去重)."""
    return [
        e for e in (data or {}).get("watchlist", []) if str(e.get("symbol", "")).strip()
    ]


def entries_for(data: dict, name: str | None = None) -> list[dict]:
    """按所属列表过滤条目; name 为空返回全部.

    name 支持逗号分隔多个列表 (与 add --list 一致), 命中任一即返回.
    """
    if not name:
        return merge_entries(data)
    names = set(parse_lists(name))
    if not names:
        return merge_entries(data)
    return [
        e
        for e in (data or {}).get("watchlist", [])
        if names & set(e.get("lists", []) or [])
    ]


def _note_str(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


def build_watchlist_view(
    entries, quotes: dict[str, Quote]
) -> tuple[pd.DataFrame, list[str]]:
    """条目 + 行情 → 视图. metric 决定基准值: price=现价, pb=现价/每股净资产.

    PB 条目的 book_value 缺失时 (crypto/无数据源/净资产为负) 不判状态,
    记 issue 并保留配置列, 阈值列留空。
    """
    rows: list[dict] = []
    issues: list[str] = []
    # PB 条目每股净资产: 行情自带 (长桥/akshare 实时口径) 优先, 只有缺失的
    # 代码才走 fundamentals 批量取数 (yfinance, 磁盘缓存 6h)
    pb_syms_all = {
        str(e.get("symbol", "")).strip()
        for e in entries
        if metric_for_entry(e) == METRIC_PB
    }
    book_values: dict[str, float | None] = {}

    fundamentals_done: set[str] = set()

    def _fetch_bvps_missing(yahoo: str) -> None:
        """行情/条目均无 BVPS 时批量拉取 (一次一批, 进程内去重)."""
        if yahoo in fundamentals_done:
            return
        from .. import fundamentals

        remaining = [
            s
            for s in pb_syms_all
            if s not in book_values and s not in fundamentals_done
        ]
        fundamentals_done.update(remaining or [yahoo])
        if remaining:
            book_values.update(fundamentals.fetch_book_values(remaining))

    for e in entries:
        e = normalize_watch_entry(e)
        raw = str(e.get("symbol", "")).strip()
        if not raw:
            continue
        try:
            p = parse(raw)
        except ValueError as err:
            issues.append(str(err))
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
        if metric == METRIC_PB:
            if not has_thresholds:
                # 无阈值仅跟踪: 不为 PB 拉基本面 (crypto 等无数据源不报假 issue)
                base_value = None
            else:
                # BVPS 优先级: 条目手写 (离线可用) > 行情自带 (akshare 东财实时
                # 口径, 最准) > fundamentals 数据源 (yfinance 财报, 磁盘缓存 6h)
                bv = entry_book_value(e) or q.book_value
                if bv is None:
                    _fetch_bvps_missing(p.yahoo)
                    bv = book_values.get(p.yahoo)
                effective_bv = bv
                base_value = (
                    price / bv
                    if bv is not None and math.isfinite(bv) and bv > 0
                    else None
                )
                if base_value is None:
                    issues.append(f"{p.yahoo}: PB 缺失 (无每股净资产数据)")
        else:
            base_value = metric_value_for_quote(metric, q)
        if has_thresholds and base_value is None:
            # 有阈值但指标无数据 (PB 缺失): 不判区间内, 标记无数据
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
            **thresholds,
            "status": status,
            "note": _note_str(e.get("note")),
            **dist,
        }
        if metric == METRIC_PB:
            # book_value 列 = 实际参与计算的 BVPS (手写/行情自带/数据源), 而非缓存值
            row["book_value"] = effective_bv
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
) -> tuple[pd.DataFrame, list[str]]:
    """用例: 取数 (providers) → 阈值规则 → 视图. watchlist list / report 复用."""
    symbols = [str(e["symbol"]) for e in entries]
    quotes, errors, notes = prices.get_quotes(
        symbols,
        prefer_akshare=prefer_akshare,
        use_ibkr=use_ibkr,
        use_longport=use_longport,
    )
    wview, wissues = build_watchlist_view(entries, quotes)
    issues = [f"{k}: {v}" for k, v in errors.items()] + wissues + notes
    return wview, issues
