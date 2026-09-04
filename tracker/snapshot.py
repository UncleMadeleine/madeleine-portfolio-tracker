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
    load_watchlist,
    triggered_entries,
)

DEFAULT_PORTFOLIO = Path(__file__).resolve().parent.parent / "portfolio.json"

WATCH_COLS = [
    "symbol", "name", "market", "currency", "price", "change_pct",
    "upper", "lower", "status", "dist_upper_pct", "dist_lower_pct", "note",
]


def load_portfolio(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def take_snapshot(
    portfolio: dict, watchlist: dict | None = None, prefer_akshare: bool = False
) -> tuple[str, pd.DataFrame, dict, pd.DataFrame, list[str]]:
    holdings = [h for h in portfolio.get("holdings", []) if h.get("symbol")]
    wentries = [
        w for w in (watchlist or {}).get("watchlist", []) if w.get("symbol")
    ]
    base = str(portfolio.get("base_currency") or "CNY").upper()
    symbols = [h["symbol"] for h in holdings] + [w["symbol"] for w in wentries]
    quotes, errors = prices.get_quotes(symbols, prefer_akshare=prefer_akshare)
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
    return base, view, summarize(view), wview, all_issues


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="投资组合命令行快照 (持仓 + 自选)")
    ap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    ap.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    ap.add_argument("--base", default=None, help="覆盖基础货币, 如 USD")
    ap.add_argument("--akshare", action="store_true", help="A股/港股优先走 akshare")
    args = ap.parse_args(argv)

    portfolio = load_portfolio(args.portfolio)
    if args.base:
        portfolio["base_currency"] = args.base.upper()
    watchlist = load_watchlist(args.watchlist)
    base, view, summary, wview, issues = take_snapshot(
        portfolio, watchlist, prefer_akshare=args.akshare
    )

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
        for k, v in m["by_market"].items():
            print(f"  {k}: {v:,.2f} ({v / m['total_value']:.1%})")
        print("\n币种分布:")
        for k, v in m["by_currency"].items():
            print(f"  {k}: {v:,.2f} ({v / m['total_value']:.1%})")

    print("\n=== 自选观察 ===")
    if wview.empty:
        print("(空)")
    else:
        with pd.option_context(
            "display.float_format", "{:,.2f}".format,
            "display.width", 240,
            "display.max_columns", None,
        ):
            print(wview[WATCH_COLS].to_string(index=False))
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


if __name__ == "__main__":
    main()
