"""nav 子命令: 历史净值 (读取 / 保存当日快照 / 按当前持仓回填).

净值记录与行情缓存分离 (见 tracker/nav.py), 因此这里的读写不会影响
quote/kline 缓存; 回填只用于「当前持仓一直持有到现在」的近似走势。
"""

from __future__ import annotations

import argparse
import json
from datetime import date

import pandas as pd

from .. import nav
from ..services.snapshot import take_snapshot
from ..storage import PORTFOLIO_PATH as DEFAULT_PORTFOLIO, load_portfolio
from ._common import _finish_with_error


def _read_and_print(base: str, days: int | None, as_json: bool) -> None:
    rows = nav.load_history(base, days=days)
    if as_json:
        print(
            json.dumps(
                {"base": base, "count": len(rows), "rows": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if not rows:
        print(
            f"暂无 {base} 净值历史。\n"
            f"  先保存今天: tracker nav --save\n"
            f"  或回填历史: tracker nav --backfill --days 365"
        )
        return

    df = pd.DataFrame(rows)
    df["日期"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d")
    df["总市值"] = pd.to_numeric(df["total_value"], errors="coerce")
    df["成本"] = pd.to_numeric(df["total_cost"], errors="coerce")
    df["浮动盈亏"] = pd.to_numeric(df["total_pnl"], errors="coerce")
    df["今日"] = pd.to_numeric(df["today_pnl"], errors="coerce")
    df["收益率"] = df["total_pnl_pct"].map(lambda v: "—" if pd.isna(v) else f"{v:+.2%}")
    df["成本覆盖"] = df["cost_coverage"].map(
        lambda v: "—" if pd.isna(v) else f"{v:.0%}"
    )
    df["来源"] = df["source"].map({"snapshot": "快照", "backfill": "回填"})
    show = df[
        ["日期", "总市值", "成本", "浮动盈亏", "收益率", "今日", "成本覆盖", "来源"]
    ]
    with pd.option_context(
        "display.float_format",
        "{:,.2f}".format,
        "display.width",
        200,
        "display.max_rows",
        None,
    ):
        print(show.to_string(index=False, na_rep="—"))

    first, last = rows[0], rows[-1]
    change = (last["total_value"] or 0) - (first["total_value"] or 0)
    pct = (
        change / abs(first["total_value"])
        if first["total_value"] not in (None, 0)
        else None
    )
    span = f"{first['day']} → {last['day']}"
    tail = f" · 区间 {change:+,.2f} {base}"
    if pct is not None:
        tail += f" ({pct:+.2%})"
    if last.get("cost_coverage") is not None and last["cost_coverage"] < 1.0:
        tail += f" · 成本覆盖 {last['cost_coverage']:.0%}"
    print(f"\n{span}{tail}")


def cmd_nav(args) -> None:
    portfolio = load_portfolio(args.portfolio)
    base = (args.base or portfolio.get("base_currency") or "CNY").upper()
    days = None if args.all else args.days

    if args.save:
        _base, view, summary, _wview, _issues = take_snapshot(
            portfolio,
            watch_name=None,
            prefer_akshare=args.akshare,
            use_ibkr=args.ibkr,
            use_longport=getattr(args, "longport", False),
            crypto_source=getattr(args, "crypto_source", "auto"),
        )
        record = nav.record_from_summary(
            date.today().isoformat(), base, summary, view, source="snapshot"
        )
        if view.empty:
            if args.json:
                print(
                    json.dumps(
                        {"base": base, "action": "save", "saved": 0},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print("⚠ 无可用持仓数据, 未写入历史净值")
            return
        try:
            saved = nav.save_rows([record])
        except RuntimeError as e:
            _finish_with_error(str(e))
        if args.json:
            clean = {k: v for k, v in record.items() if k != "payload"}
            print(
                json.dumps(
                    {"base": base, "action": "save", "saved": saved, "record": clean},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        print(
            f"已保存 {record['day']} 净值: 总市值 {record['total_value']:,.2f} {base}"
        )
        return

    if args.backfill:
        rows, issues = nav.backfill(
            portfolio,
            base,
            days=args.days,
            prefer_akshare=args.akshare,
            use_ibkr=args.ibkr,
            use_longport=getattr(args, "longport", False),
            crypto_source=getattr(args, "crypto_source", "auto"),
        )
        inserted = 0
        if rows and not args.dry_run:
            try:
                inserted = nav.save_rows(rows, overwrite=False)
            except RuntimeError as e:
                _finish_with_error(str(e))
        if args.json:
            payload = {
                "base": base,
                "action": "backfill",
                "days": args.days,
                "generated": len(rows),
                "inserted": inserted,
                "dry_run": bool(args.dry_run),
                "issues": issues,
            }
            if args.dry_run:
                payload["rows"] = rows
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        head = (
            f"回填 {len(rows)} 个交易日 ({args.days} 天)" if rows else "未生成回填数据"
        )
        if args.dry_run:
            head += " · 预览模式, 未写库"
        elif rows:
            head += f" · 新增/更新 {inserted} 条 (真实快照的日期保持不变)"
        print(head)
        if issues:
            print("\n⚠ 问题:")
            for i in issues:
                print(f"  - {i}")
        return

    _read_and_print(base, days, args.json)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="历史净值: 读取 / 保存 / 回填")
    ap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    ap.add_argument("--base", default=None, help="基础货币, 如 USD (默认取持仓文件)")
    ap.add_argument("--days", type=int, default=180, help="回看自然日数 (默认 180)")
    ap.add_argument("--all", action="store_true", help="显示全部历史 (忽略 --days)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--save", action="store_true", help="保存今日快照")
    mode.add_argument("--backfill", action="store_true", help="按当前持仓回填历史")
    ap.add_argument("--dry-run", action="store_true", help="回填只预览不写库")
    ap.add_argument("--akshare", action="store_true")
    ap.add_argument("--ibkr", action="store_true")
    ap.add_argument("--longport", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        cmd_nav(args)
    except Exception as e:  # noqa: BLE001 - 直跑模块时给可读错误
        _finish_with_error(str(e))


if __name__ == "__main__":
    main()
