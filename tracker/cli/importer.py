"""import 子命令: 统一持仓导入入口 (ibkr / wallet / file).

    tracker import ibkr   从 IB Gateway 账户导入持仓
    tracker import wallet 从链上地址导入加密资产
    tracker import file   从券商导出文件 (CSV/Excel) 导入

三个来源共用: --overwrite 覆盖(默认追加合并) / --dry-run / --portfolio / --json.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .. import importer
from ._common import _finish_with_error, _print_json


def _collect(args: argparse.Namespace) -> tuple[list[dict], list[str], str]:
    """按来源采集持仓, 返回 (rows, skipped, 来源标签)."""
    source = getattr(args, "source", None)
    if source == "ibkr":
        try:
            rows, skipped = importer.collect_ibkr(mode=args.mode)
        except Exception as e:
            _finish_with_error(f"{e} (请确认 IB Gateway 已登录运行, 且 API 连接已启用)")
        return rows, skipped, "IBKR 账户持仓"
    if source == "wallet":
        try:
            rows, skipped, raw = importer.collect_wallet(
                args.chain.lower(),
                args.address,
                tokenlist=args.tokenlist,
                base_currency=args.base_currency,
            )
        except ValueError as e:
            _finish_with_error(str(e))
        except (RuntimeError, FileNotFoundError) as e:
            _finish_with_error(f"钱包查询失败: {e}")
        label = f"{args.chain.lower()} 链上钱包 ({raw['address'][:10]}…)"
        return rows, skipped, label
    try:
        rows, skipped = importer.collect_ashare_file(args.file)
    except (FileNotFoundError, RuntimeError) as e:
        _finish_with_error(str(e))
    return rows, skipped, f"券商文件 {Path(args.file).name}"


def cmd_import(args: argparse.Namespace) -> None:
    """import <来源> 统一入口: 采集 → 预览 → 按模式写入 portfolio.json."""
    rows, skipped, label = _collect(args)
    mode = importer.MODE_OVERWRITE if args.overwrite else importer.MODE_APPEND
    result = importer.apply_import(rows, args.portfolio, mode=mode, dry_run=args.dry_run)

    if args.json:
        _print_json(
            {
                "source": args.source,
                "positions": rows,
                "skipped": skipped,
                **result,
            }
        )
        return

    print(f"\n=== {label} ({len(rows)} 条) ===")
    if rows:
        with pd.option_context(
            "display.float_format", "{:,.4f}".format, "display.width", 160
        ):
            print(pd.DataFrame(rows).to_string(index=False))
    else:
        print("(无有效持仓或均无法映射)")
    for s in skipped:
        print(f"  ⚠ {s}")

    if args.dry_run:
        print(f"\n(dry-run, 未写入; 导入方式: {_mode_label(mode)})")
        return
    if not result["written"]:
        print("\n⏭ 无可导入持仓, 未写入")
        return
    if result["backup"]:
        print(f"\n已备份原文件: {Path(result['portfolio']).name}.bak")
    print(
        f"✅ 已写入 {result['portfolio']} "
        f"({_mode_label(mode)}: 新增 {len(result['added'])} · "
        f"更新 {len(result['updated'])}, 共 {result['total']} 条持仓)"
    )


def _mode_label(mode: str) -> str:
    return "覆盖" if mode == importer.MODE_OVERWRITE else "追加合并"
