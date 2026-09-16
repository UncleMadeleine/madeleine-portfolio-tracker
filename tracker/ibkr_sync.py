"""从 IB Gateway 账户同步真实持仓到 portfolio.json: python -m tracker.ibkr_sync

兼容入口 — 导入功能已统一到 tracker.importer (CLI: tracker import ibkr);
本模块保留 python -m tracker.ibkr_sync 与 `tracker sync` 调用路径。
默认覆盖写入 (历史 sync 语义); --append 切换为追加合并。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from . import importer
from .storage import PORTFOLIO_PATH as DEFAULT_PORTFOLIO


def run_sync(args) -> None:
    try:
        rows, skipped = importer.collect_ibkr(mode=getattr(args, "mode", None))
    except Exception as e:
        print(f"❌ {e}")
        print("请确认 IB Gateway 已登录运行, 且 API 连接已启用 (ibkr.json 配置的 mode/port)。")
        raise SystemExit(1)

    mode = (
        importer.MODE_APPEND
        if getattr(args, "append", False)
        else importer.MODE_OVERWRITE
    )
    result = importer.apply_import(rows, args.portfolio, mode=mode, dry_run=args.dry_run)

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "positions": rows,
                    "skipped": skipped,
                    "dry_run": bool(args.dry_run),
                    "written": result["written"],
                    "mode": mode,
                    "added": result["added"],
                    "updated": result["updated"],
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

    if result["written"]:
        print(f"\n已备份原文件: {Path(args.portfolio).name}.bak")
        print(
            f"✅ 已写入 {args.portfolio} "
            f"({'追加合并' if mode == importer.MODE_APPEND else '覆盖'}; "
            f"新增 {len(result['added'])} · 更新 {len(result['updated'])}, "
            f"共 {result['total']} 条持仓, 基础货币保留)"
        )
    print("提示: avg_cost 为 IBKR 报告的合约货币每股均价 (含佣金), 仅供估算。")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="IBKR 账户持仓同步 (等价 tracker import ibkr)")
    ap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    ap.add_argument("--dry-run", action="store_true", help="仅打印, 不写入")
    ap.add_argument("--mode", choices=["paper", "live"], default=None,
                    help="Gateway API 模式: paper 模拟(4002) / live 实盘(4001); 缺省用配置")
    ap.add_argument("--append", action="store_true",
                    help="追加合并 (按代码更新/新增); 缺省覆盖全部持仓")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = ap.parse_args(argv)
    run_sync(args)


if __name__ == "__main__":
    main()
