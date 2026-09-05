"""命令行快照: python -m tracker.snapshot"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from . import prices
from .analytics import build_view, summarize
from .fx import get_fx_rates
from .watchlist import (
    DEFAULT_WATCHLIST,
    build_watchlist_view,
    entries_for,
    load_watchlist,
    triggered_entries,
)

DEFAULT_PORTFOLIO = Path(__file__).resolve().parent.parent / "portfolio.json"

WATCH_COLS = [
    "symbol", "name", "market", "currency", "price", "change_pct",
    "upper_1", "upper_2", "lower_1", "lower_2", "status",
    "dist_upper_1_pct", "dist_upper_2_pct", "dist_lower_1_pct", "dist_lower_2_pct", "note",
]


def load_portfolio(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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
        currencies = sorted({q.currency for q in quotes.values()})
        fx, fx_missing = get_fx_rates(base, currencies)
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


def run_snapshot(args) -> None:
    portfolio = load_portfolio(args.portfolio)
    if args.base:
        portfolio["base_currency"] = args.base.upper()
    watchlist = load_watchlist(args.watchlist_file)
    base, view, summary, wview, issues = take_snapshot(
        portfolio, watchlist, watch_name=args.watchlist,
        prefer_akshare=args.akshare, use_ibkr=args.ibkr,
    )

    if getattr(args, "json", False):
        print(json.dumps(snapshot_json(base, view, summary, wview, issues),
                         ensure_ascii=False, indent=2))
        return

    print(f"\n=== 投资组合快照 ({base}) ===")
    if view.empty:
        print("无可用持仓数据")
    else:
        with pd.option_context(
            "display.float_format", "{:,.2f}".format,
            "display.width", 220,
            "display.max_columns", None,
        ):
            print(view.to_string(index=False))
        m = summary
        print(f"\n总市值: {m['total_value']:,.2f} {base}")
        if m["total_pnl"] is not None:
            pct = f" ({m['total_pnl_pct']:+.2%})" if m["total_pnl_pct"] is not None else ""
            print(f"浮动盈亏: {m['total_pnl']:+,.2f} {base}{pct}")
        if m["today_pnl"] is not None:
            print(f"今日估算: {m['today_pnl']:+,.2f} {base}")
        print("\n市场分布:")
        total = m["total_value"] or 0
        for k, v in m["by_market"].items():
            pct = v / total if total else 0
            print(f"  {k}: {v:,.2f} ({pct:.1%})")
        print("\n币种分布:")
        for k, v in m["by_currency"].items():
            pct = v / total if total else 0
            print(f"  {k}: {v:,.2f} ({pct:.1%})")

    scope_label = f"自选列表: {args.watchlist}" if args.watchlist else "全部自选"
    print(f"\n=== 自选观察 ({scope_label}) ===")
    if wview.empty:
        print("(空)")
    else:
        with pd.option_context(
            "display.float_format", "{:,.2f}".format,
            "display.width", 240,
            "display.max_columns", None,
        ):
            print(wview.reindex(columns=WATCH_COLS).to_string(index=False))
        trig = triggered_entries(wview)
        if not trig.empty:
            print(f"\n🔔 阈值提醒 ({len(trig)}):")
            for _, r in trig.iterrows():
                print(
                    f"  {r['symbol']} {r['status']} 现价 {r['price']:,.2f} {r['currency']}"
                )

    if issues:
        print("\n⚠ 问题:")
        for i in issues:
            print(f"  - {i}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="投资组合命令行快照 (持仓 + 自选)")
    ap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    ap.add_argument("--watchlist-file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径")
    ap.add_argument("--watchlist", default=None, help="只查看某个子自选列表 (默认全部)")
    ap.add_argument("--base", default=None, help="覆盖基础货币, 如 USD")
    ap.add_argument("--akshare", action="store_true", help="A股/港股优先走 akshare")
    ap.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 TWS/IB Gateway)")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = ap.parse_args(argv)
    run_snapshot(args)


if __name__ == "__main__":
    main()
