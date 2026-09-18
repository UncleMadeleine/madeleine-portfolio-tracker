"""统一持仓导入管道: IBKR 账户 / 链上钱包 / A股券商文件 → portfolio.json.

各来源的采集函数 collect_*() 统一返回 (rows, skipped):
rows 为 [{symbol, quantity, avg_cost?, type?}] (symbol 已是 Yahoo 规范代码),
skipped 为无法导入/查询失败的说明 (不阻断整体导入).

merge_holdings() 与 apply_import() 是合并与写盘的唯一入口, 支持两种模式:
  - append    追加合并: 已有代码更新 quantity (row 带 avg_cost 时一并更新),
              新代码追加, 其余持仓原样保留。带 import_source 溯源键的行
              (钱包导入) 按 (代码, 来源) 合并: 同来源覆盖 (重复导入幂等),
              不同来源累加 (多链/多地址同名代币不互相覆盖)
  - overwrite 覆盖:     忽略现有持仓, 全部由本次 rows 组成

apply_import() 写盘前自动备份 <portfolio>.bak, 并保留文件中的
base_currency 与其它自定义键 (如 _说明)。

CLI (tracker import <来源>) 与 Streamlit「导入」页共用本模块;
tracker.ibkr_sync / tracker.ashare_sync 的 run_sync 为兼容入口.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .storage import PORTFOLIO_PATH as DEFAULT_PORTFOLIO, backup_file, load_portfolio, save_portfolio
from .symbols import parse

MODE_APPEND = "append"
MODE_OVERWRITE = "overwrite"
IMPORT_MODES = (MODE_APPEND, MODE_OVERWRITE)


def _norm_sym(symbol: str) -> str:
    """代码 → Yahoo 规范形式; 无法识别时退回原始大写."""
    raw = str(symbol).strip()
    try:
        return parse(raw).yahoo
    except ValueError:
        return raw.upper()


def normalize_row(row: dict) -> dict:
    """导入行归一化: symbol 转 Yahoo 规范代码, quantity 转 float."""
    r = dict(row)
    r["symbol"] = _norm_sym(r.get("symbol", ""))
    if "quantity" in r:
        r["quantity"] = float(r["quantity"])
    return r


def _merge_key(row: dict) -> tuple[str, str]:
    """合并键: (代码, 导入来源).

    无 import_source 的普通导入 (IBKR / A股券商文件) 沿用「按代码覆盖」语义,
    保证同一账户重复同步是幂等的。
    """
    return (_norm_sym(row.get("symbol", "")), str(row.get("import_source") or ""))


# ---------------------------------------------------------------------------
# 采集 (各来源 → 统一 rows)
# ---------------------------------------------------------------------------

def collect_ibkr(mode: str | None = None) -> tuple[list[dict], list[str]]:
    """从 IB Gateway 账户读取股票持仓. mode: paper/live/None(按 ibkr.json 配置)."""
    from . import ibkr

    cfg = ibkr.load_config()
    if mode:
        cfg["mode"] = mode
    positions = ibkr.fetch_positions(cfg)
    return ibkr.positions_to_rows(positions)


def collect_ashare_file(path: str | Path) -> tuple[list[dict], list[str]]:
    """解析 A股券商客户端导出的持仓文件 (CSV/Excel)."""
    from .ashare_sync import parse_positions_file

    return parse_positions_file(path)


def wallet_rows(result: dict) -> list[dict]:
    """wallet.import_wallet() 结果 → portfolio rows (无 avg_cost, 导入后补填).

    带 import_source 溯源键 (链 + 地址): 同一地址重复导入按代码覆盖 (幂等),
    不同地址/链的同名代币 (如 eth 与 bsc 上的 USDT-USD) 在合并时累加,
    避免多钱包余额互相覆盖造成静默丢失。
    """
    source = f"wallet:{result.get('chain') or ''}:{result.get('address') or ''}"
    rows: list[dict] = []
    for h in result.get("holdings", []):
        rows.append(
            {
                "symbol": _norm_sym(h.get("symbol", "")),
                "quantity": h.get("quantity", 0),
                "type": "crypto",
                "import_source": source,
            }
        )
    return rows


def collect_wallet(
    chain: str,
    address: str,
    *,
    tokenlist: str | None = None,
    base_currency: str = "USD",
) -> tuple[list[dict], list[str], dict]:
    """查询链上地址余额. 返回 (rows, errors, 原始结果).

    errors 为单个代币查询失败等非致命问题; 地址/链非法抛 ValueError,
    tokenlist 缺失抛 FileNotFoundError.
    """
    from . import wallet

    raw = wallet.import_wallet(
        chain, address, tokenlist=tokenlist, base_currency=base_currency
    )
    return wallet_rows(raw), list(raw.get("errors", [])), raw


# ---------------------------------------------------------------------------
# 合并与写入
# ---------------------------------------------------------------------------

def merge_holdings(
    existing: list[dict], rows: list[dict], mode: str = MODE_APPEND
) -> tuple[list[dict], dict[str, list[str]]]:
    """按模式合并持仓, 返回 (合并后 holdings, {added, updated}).

    append:    已存在的代码更新 quantity (row 带 avg_cost 时一并更新, 否则保留原值),
               新代码追加到末尾
    overwrite: 忽略 existing, 全部由 rows 组成
    """
    if mode not in IMPORT_MODES:
        raise ValueError(f"未知导入模式: {mode} (支持: {', '.join(IMPORT_MODES)})")
    normalized = [normalize_row(r) for r in rows]
    stats: dict[str, list[str]] = {"added": [], "updated": []}
    if mode == MODE_OVERWRITE:
        stats["added"] = [r["symbol"] for r in normalized]
        return normalized, stats
    merged = [dict(h) for h in existing]
    index: dict[tuple[str, str], int] = {}
    for i, h in enumerate(merged):
        index.setdefault(_merge_key(h), i)
    for r in normalized:
        sym = r["symbol"]
        key = _merge_key(r)
        if key in index:
            target = merged[index[key]]
            target["quantity"] = r["quantity"]
            if "avg_cost" in r:
                target["avg_cost"] = r["avg_cost"]
            stats["updated"].append(sym)
            continue
        # 同代码但来源不同 (不同链/地址的钱包导入): 累加数量, 不覆盖
        same = next(
            (
                i for i, h in enumerate(merged)
                if r.get("import_source") and _norm_sym(h.get("symbol", "")) == sym
            ),
            None,
        )
        if same is not None:
            merged[same]["quantity"] = (
                float(merged[same].get("quantity") or 0) + float(r["quantity"] or 0)
            )
            stats["updated"].append(sym)
            continue
        merged.append(r)
        index[key] = len(merged) - 1
        stats["added"].append(sym)
    return merged, stats


def apply_import(
    rows: list[dict],
    portfolio: str | Path = DEFAULT_PORTFOLIO,
    *,
    mode: str = MODE_APPEND,
    dry_run: bool = False,
) -> dict[str, Any]:
    """合并并写盘 (导入写盘的唯一入口).

    写前自动备份 <portfolio>.bak; 保留 portfolio.json 中的 base_currency
    与其它自定义键。dry_run=True 或 rows 为空时不写文件 (written=False)。
    """
    target = Path(portfolio)
    data: dict = load_portfolio(target) if target.exists() else {}
    merged, stats = merge_holdings(data.get("holdings", []), rows, mode)
    written = False
    backup = None
    if not dry_run and rows:
        backup = backup_file(target)
        data["holdings"] = merged
        data.setdefault("base_currency", "CNY")
        save_portfolio(data, target)
        written = True
    return {
        "mode": mode,
        "dry_run": bool(dry_run),
        "written": written,
        "added": stats["added"],
        "updated": stats["updated"],
        "total": len(merged),
        "portfolio": str(target),
        "backup": backup,
    }
