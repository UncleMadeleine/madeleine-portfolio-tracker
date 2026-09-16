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
from ..storage import parse_lists, normalize_watch_entry
from ..symbols import parse
from .rules import (
    STATUS_RANK,
    STATUS_WITHIN,
    evaluate_thresholds,
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
        e for e in (data or {}).get("watchlist", [])
        if str(e.get("symbol", "")).strip()
    ]

def entries_for(data: dict, name: str | None = None) -> list[dict]:
    """按所属列表过滤条目; name 为空返回全部.

    name 支持逗号分隔多个列表 (与 add --list 一致), 命中任一即返回.
    """
    if not name:
        return merge_entries(data)
    names = set(parse_lists(name)) - {"默认"}
    if not names:
        return merge_entries(data)
    return [
        e for e in (data or {}).get("watchlist", [])
        if names & set(e.get("lists", []) or [])
    ]


def _note_str(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


def build_watchlist_view(
    entries, quotes: dict[str, Quote]
) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict] = []
    issues: list[str] = []
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
        q = quotes.get(p.yahoo)
        if q is None or q.price is None or not math.isfinite(q.price):
            issues.append(f"{p.yahoo}: 行情缺失")
            continue
        price = q.price
        thresholds = parse_thresholds(e)
        status, dist = evaluate_thresholds(thresholds, price)
        rows.append(
            {
                "symbol": p.yahoo,
                "name": q.name or "",
                "market": p.market_label,
                "currency": q.currency,
                "price": price,
                "change_pct": q.change_pct,
                **thresholds,
                "status": status,
                "note": _note_str(e.get("note")),
                **dist,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["triggered"] = df["status"] != STATUS_WITHIN
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
        return ranked.sort_values(["status_rank", "symbol"], ascending=[True, True]).drop(columns=["status_rank"])
    if mode == "change_desc":
        return view.sort_values(["change_pct", "symbol"], ascending=[False, True])
    if mode == "change_asc":
        return view.sort_values(["change_pct", "symbol"], ascending=[True, True])
    return view.sort_values(["triggered", "symbol"], ascending=[False, True])


def fetch_watchlist_view(
    entries: list[dict],
    prefer_akshare: bool = False,
    use_ibkr: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """用例: 取数 (providers) → 阈值规则 → 视图. watchlist list / report 复用."""
    symbols = [str(e["symbol"]) for e in entries]
    quotes, errors, notes = prices.get_quotes(
        symbols, prefer_akshare=prefer_akshare, use_ibkr=use_ibkr
    )
    wview, wissues = build_watchlist_view(entries, quotes)
    issues = [f"{k}: {v}" for k, v in errors.items()] + wissues + notes
    return wview, issues
