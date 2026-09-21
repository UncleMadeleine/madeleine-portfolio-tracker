"""A股券商持仓导入: 解析券商客户端导出的持仓 CSV/Excel 文件, 同步到 portfolio.json.

与 IBKR 的 ibkr_sync.py 不同, A股券商没有跨券商统一 API, 且监管对个人程序化接入
持续收紧。因此本模块采用"文件导入"模式: 用户在券商客户端(同花顺/通达信/华泰/东财/QMT
等)手动导出持仓为 CSV/Excel, 本模块解析后映射为 Yahoo 代码写入 portfolio.json。

零凭证、零新增硬依赖(仅用 pandas)、跨平台、无合规风险。

用法:
    python -m tracker.ashare_sync 持仓.csv
    python -m tracker.ashare_sync 持仓.xlsx --dry-run
    python -m tracker.ashare_sync 持仓.csv --json
    tracker import file 持仓.csv --overwrite   # 统一 CLI 入口 (tracker.importer)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from . import importer
from .cli._common import _finish_with_error
from .storage import PORTFOLIO_PATH as DEFAULT_PORTFOLIO
from .symbols import type_for_symbol

# A股代码前缀 -> Yahoo 交易所后缀 (与 ibkr.py _A_SHARE_*_PREFIX 规则一致)
_BJ_PREFIX = ("4", "8", "920")
_SH_PREFIX = ("5", "6", "9")


def code_to_yahoo(code: str) -> str | None:
    """6位A股代码 -> Yahoo 后缀代码 (.SS/.SZ/.BJ); 非A股代码原样返回."""
    raw = str(code).strip()
    if not raw:
        return None
    # 去掉可能的前导市场标记 (SH/SZ/BJ.)
    for prefix in ("SH", "SZ", "BJ"):
        if raw.upper().startswith(prefix):
            raw = raw[len(prefix):].lstrip(".")
            break
    # 数值读入残留的小数形式 (1.0 → 1) 先归一为整数串
    if raw.endswith(".0"):
        raw = raw[:-2]
    if not raw.isdigit() or len(raw) != 6:
        return None
    if raw.startswith(_BJ_PREFIX):
        return f"{raw}.BJ"
    if raw.startswith(_SH_PREFIX):
        return f"{raw}.SS"
    return f"{raw}.SZ"


# 各券商导出列名候选 (按常见度排序), 模糊匹配取第一个命中
_CODE_KEYS = ("证券代码", "股票代码", "代码", "product_code", "symbol", "证券账号代码")
_NAME_KEYS = ("证券名称", "股票名称", "名称", "product_name", "name")
_QTY_KEYS = (
    "持仓数量", "当前持仓", "股份余额", "股份可用", "可用余额",
    "持仓股数", "总持仓", "volume", "当前数量", "持仓量",
)
_COST_KEYS = (
    "成本价", "参考成本价", "买入均价", "持仓成本", "开仓均价",
    "摊薄成本价", "保本价", "成本", "avg_price", "vwap", "open_price",
)


def _pick_col(df: pd.DataFrame, keys: tuple[str, ...]) -> str | None:
    cols = {str(c).strip(): c for c in df.columns}
    for k in keys:
        if k in cols:
            return cols[k]
    # 容错: 大小写/空格归一化再匹配一次
    norm = {k.replace(" ", "").lower(): c for k, c in cols.items()}
    for k in keys:
        if k.replace(" ", "").lower() in norm:
            return norm[k.replace(" ", "").lower()]
    return None


def _to_float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(f):
        return None
    return float(f)


def parse_positions_file(path: str | Path) -> tuple[list[dict], list[str]]:
    """解析券商导出的持仓文件 (CSV/Excel), 返回 (rows, skipped).

    rows: [{symbol, quantity, avg_cost?}] symbol 已映射为 Yahoo 代码。
    skipped: 无法解析的行说明。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")
    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        try:
            df = pd.read_excel(p, dtype=str)
        except ImportError as e:
            raise RuntimeError(
                "读取 Excel 需要 openpyxl, 请: pip install openpyxl"
            ) from e
    else:
        # CSV: 尝试常见编码 (券商导出多为 GBK/GB2312)
        df = None
        for enc in ("utf-8", "gbk", "gb18030", "utf-8-sig"):
            try:
                # dtype=str: 券商导出常省略前导零 (000001), 按数值读入会变成 1 而丢失代码
                df = pd.read_csv(p, encoding=enc, dtype=str)
                break
            except UnicodeDecodeError:
                continue
        if df is None:
            raise RuntimeError(f"无法解码 CSV (尝试 utf-8/gbk/gb18030): {p}")

    if df is None or df.empty:
        return [], [f"文件为空或无数据: {p.name}"]

    code_col = _pick_col(df, _CODE_KEYS)
    qty_col = _pick_col(df, _QTY_KEYS)
    if not code_col or not qty_col:
        return [], [
            f"未找到代码列(候选: {_CODE_KEYS}) 或数量列(候选: {_QTY_KEYS})",
            f"实际列: {list(df.columns)}",
        ]
    cost_col = _pick_col(df, _COST_KEYS)

    rows: list[dict] = []
    skipped: list[str] = []
    for _, r in df.iterrows():
        raw_code = str(r[code_col]).strip()
        yahoo = code_to_yahoo(raw_code)
        if not yahoo:
            skipped.append(f"{raw_code}: 非6位A股代码, 跳过")
            continue
        qty = _to_float(r[qty_col])
        if qty is None or qty == 0:
            continue
        avg_cost = _to_float(r[cost_col]) if cost_col else None
        rows.append(
            {
                "symbol": yahoo,
                "type": type_for_symbol(yahoo),
                "quantity": qty,
                # avg_cost=0 是合法成本 (如送股/配股后), 不能被当作缺失去掉
                **({"avg_cost": round(avg_cost, 6)} if avg_cost is not None else {}),
            }
        )
    return rows, skipped


def run_sync(args) -> None:
    try:
        rows, skipped = parse_positions_file(args.file)
    except FileNotFoundError as e:
        _finish_with_error(str(e))
    except (ValueError, RuntimeError) as e:
        _finish_with_error(f"{args.file}: 解析失败 ({e})")

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
                    "source": str(args.file),
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

    print(f"\n=== 券商文件持仓 ({len(rows)} 只, 来源: {Path(args.file).name}) ===")
    if rows:
        with pd.option_context(
            "display.float_format", "{:,.4f}".format, "display.width", 160
        ):
            print(pd.DataFrame(rows).to_string(index=False))
    else:
        print("(无有效持仓或均无法映射)")
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
    print("提示: avg_cost 为券商导出的成本价(当地货币), 仅供估算。")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="A股券商持仓文件导入 (CSV/Excel; 等价 tracker import file)"
    )
    ap.add_argument("file", help="券商导出的持仓文件路径 (CSV/Excel)")
    ap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    ap.add_argument("--dry-run", action="store_true", help="仅打印, 不写入")
    ap.add_argument("--append", action="store_true",
                    help="追加合并 (按代码更新/新增); 缺省覆盖全部持仓")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = ap.parse_args(argv)
    run_sync(args)


if __name__ == "__main__":
    main()
