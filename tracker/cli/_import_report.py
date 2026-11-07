"""import 系兼容入口共用的 CLI 输出管线.

`python -m tracker.ibkr_sync` / `python -m tracker.ashare_sync` 与
`tracker sync` 走同一套语义: 缺省追加合并、--overwrite 覆盖、
JSON / 表格二选一。此前两份 run_sync 各写一遍, 提示文案逐渐漂移;
本模块收敛为单一实现, 两个入口只保留「如何收集 rows / skipped」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .. import importer


def run_import_report(
    rows: list[dict],
    skipped: list[str],
    args,
    *,
    title: str,
    empty_msg: str,
    tip: str,
    source: str | None = None,
) -> None:
    """apply_import + 终端/JSON 报告 (两个兼容入口共用)."""
    # 与统一入口 tracker import ibkr / import file 一致: 缺省追加合并,
    # --overwrite 才覆盖全部。(旧版缺省为覆盖, 会让按「导入默认追加」文档走的
    # 用户在执行 sync 时静默丢掉加密钱包/手工录入/券商文件导入的持仓)
    # --append 保留为兼容参数: 它现在是缺省行为, 传不传一个样。
    mode = (
        importer.MODE_OVERWRITE
        if getattr(args, "overwrite", False)
        else importer.MODE_APPEND
    )
    result = importer.apply_import(
        rows, args.portfolio, mode=mode, dry_run=args.dry_run
    )

    if getattr(args, "json", False):
        payload: dict = {"source": source} if source is not None else {}
        payload.update(
            {
                "positions": rows,
                "skipped": skipped,
                "dry_run": bool(args.dry_run),
                "written": result["written"],
                "mode": mode,
                "added": result["added"],
                "updated": result["updated"],
            }
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    header = f"\n=== {title} ({len(rows)} 只"
    if source:
        header += f", 来源: {Path(source).name}"
    header += ") ==="
    print(header)
    if rows:
        with pd.option_context(
            "display.float_format", "{:,.4f}".format, "display.width", 160
        ):
            print(pd.DataFrame(rows).to_string(index=False))
    else:
        print(empty_msg)
    for s in skipped:
        print(f"  ⚠ {s}")

    if args.dry_run or not rows:
        return

    if result["written"]:
        print(
            f"✅ 已写入 {args.portfolio} "
            f"({'追加合并' if mode == importer.MODE_APPEND else '覆盖'}; "
            f"新增 {len(result['added'])} · 更新 {len(result['updated'])}, "
            f"共 {result['total']} 条持仓, 基础货币保留)"
        )
    print(tip)
