"""snapshot 子命令: 组合 + 自选快照 (持仓 / 自选提醒 / 阈值触发).

用例层 (services/snapshot.take_snapshot) 负责取数与视图组装;
本模块只负责终端渲染与 argparse, 支持 python -m tracker.cli.snapshot 直跑。
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from ..services.snapshot import snapshot_json, take_snapshot
from ..services.watchlist import triggered_entries
from ..storage import PORTFOLIO_PATH as DEFAULT_PORTFOLIO, load_portfolio
from ..watchlist import DEFAULT_WATCHLIST, load_watchlist

WATCH_COLS = [
    "symbol", "name", "market", "currency", "price", "change_pct",
    "upper_1", "upper_2", "lower_1", "lower_2", "status",
    "dist_upper_1_pct", "dist_upper_2_pct", "dist_lower_1_pct", "dist_lower_2_pct", "note",
]


def cmd_snapshot(args) -> None:
    """组合 + 自选快照, 复用本模块的渲染逻辑."""
    run_snapshot(args)


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
        if m["total_pnl"] is not None:
            note = ""
            cov = m.get("cost_coverage")
            if cov is not None and cov < 1.0:
                note = f"，口径覆盖 {cov:.0%} 市值"
            pct = f" ({m['total_pnl_pct']:+.2%})" if m["total_pnl_pct"] is not None else ""
            print(f"浮动盈亏: {m['total_pnl']:+,.2f} {base}{pct}{note}")
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
    ap.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 行情 (需 IB Gateway)")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = ap.parse_args(argv)
    run_snapshot(args)


if __name__ == "__main__":
    main()
