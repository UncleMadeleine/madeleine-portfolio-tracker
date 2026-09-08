"""从 IBKR 账户同步真实持仓到 portfolio.json: python -m tracker.ibkr_sync"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd

from .ibkr import fetch_positions, load_config, positions_to_rows
from .snapshot import DEFAULT_PORTFOLIO, load_portfolio, save_portfolio


def run_sync(args) -> None:
    try:
        positions = fetch_positions(load_config())
    except Exception as e:
        print(f"❌ {e}")
        print("请确认 TWS/IB Gateway 已登录运行, 且 API 连接已启用 (ibkr.json 配置端口)。")
        raise SystemExit(1)

    rows, skipped = positions_to_rows(positions)

    written = False
    base = "CNY"
    if not args.dry_run and rows:
        target = Path(args.portfolio)
        if target.exists():
            try:
                base = load_portfolio(target).get("base_currency", "CNY")
            except Exception:
                pass
            shutil.copy(target, Path(str(target) + ".bak"))
        save_portfolio({"base_currency": base, "holdings": rows}, target)
        written = True

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "positions": rows,
                    "skipped": skipped,
                    "dry_run": bool(args.dry_run),
                    "written": written,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(f"\n=== IBKR 账户持仓 ({len(rows)} 只) ===")
    if rows:
        with pd.option_context(
            "display.float_format", "{:,.4f}".format, "display.width", 160
        ):
            print(pd.DataFrame(rows).to_string(index=False))
    else:
        print("(无股票持仓或均无法映射)")
    for s in skipped:
        print(f"  ⚠ {s}")

    if args.dry_run or not rows:
        return

    if written:
        print(f"\n已备份原文件: {Path(args.portfolio).name}.bak")
        print(f"✅ 已写入 {args.portfolio} (基础货币保留: {base})")
    print("提示: avg_cost 为 IBKR 报告的合约货币每股均价 (含佣金), 仅供估算。")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="IBKR 账户持仓同步")
    ap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    ap.add_argument("--dry-run", action="store_true", help="仅打印, 不写入")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = ap.parse_args(argv)
    run_sync(args)


if __name__ == "__main__":
    main()
