"""快照用例: 持仓 + 自选聚合 (取数 → 纯函数 → 视图), 无存储 I/O。

调用方 (cli/snapshot, cli/export, tests) 传入已加载的 portfolio/watchlist dict;
终端渲染留在 cli 层。
"""
from __future__ import annotations

import pandas as pd

from .. import prices
from ..analytics import build_view, summarize
from ..fx import get_fx_rates
from ..symbols import parse
from .watchlist import build_watchlist_view, entries_for, triggered_entries


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> records, NaN/NaT 转 None 便于 JSON 序列化."""
    if df is None or df.empty:
        return []
    return df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")


def _series_to_dict(s) -> dict[str, float]:
    """Series/dict -> {key: float}, 空值返回 {}."""
    if s is None:
        return {}
    return {str(k): float(v) for k, v in s.items()}


def snapshot_json(
    base: str, view: pd.DataFrame, summary: dict, wview: pd.DataFrame, issues: list[str]
) -> dict:
    return {
        "base_currency": base,
        "holdings": _records(view),
        "summary": {
            "total_value": summary.get("total_value"),
            "total_cost": summary.get("total_cost"),
            "total_pnl": summary.get("total_pnl"),
            "total_pnl_pct": summary.get("total_pnl_pct"),
            "cost_coverage": summary.get("cost_coverage"),
            "today_pnl": summary.get("today_pnl"),
            "by_market": _series_to_dict(summary.get("by_market")),
            "by_currency": _series_to_dict(summary.get("by_currency")),
        },
        "watchlist": _records(wview),
        "triggered": _records(triggered_entries(wview)),
        "issues": issues,
    }


def take_snapshot(
    portfolio: dict,
    watchlist: dict | None = None,
    watch_name: str | None = None,
    prefer_akshare: bool = False,
    use_ibkr: bool = False,
) -> tuple[str, pd.DataFrame, dict, pd.DataFrame, list[str]]:
    holdings = [h for h in portfolio.get("holdings", []) if h.get("symbol")]
    wentries = [
        w for w in entries_for(watchlist or {}, watch_name) if w.get("symbol")
    ]
    base = str(portfolio.get("base_currency") or "CNY").upper()
    symbols = [h["symbol"] for h in holdings] + [w["symbol"] for w in wentries]
    quotes, errors, notes = prices.get_quotes(
        symbols, prefer_akshare=prefer_akshare, use_ibkr=use_ibkr
    )
    if holdings:
        # 只对持仓实际涉及的币种取汇率; 自选股不做换算,
        # 把自选币种混进来会造成虚假的「汇率缺失」告警
        holding_syms = set()
        for h in holdings:
            try:
                holding_syms.add(parse(str(h["symbol"])).yahoo)
            except ValueError:
                holding_syms.add(str(h["symbol"]).strip().upper())
        currencies = sorted({
            q.currency for k, q in quotes.items() if k in holding_syms
        })
        fx, fx_missing = get_fx_rates(base, currencies, use_ibkr=use_ibkr)
    else:
        fx, fx_missing = {}, []
    view, issues = build_view(holdings, quotes, fx)
    wview, wissues = build_watchlist_view(wentries, quotes)
    all_issues = (
        [f"{k}: {v}" for k, v in errors.items()]
        + issues
        + wissues
        + [f"汇率缺失: {c}" for c in fx_missing]
    )
    return base, view, summarize(view), wview, all_issues + notes
