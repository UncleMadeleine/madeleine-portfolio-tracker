"""export 子命令: 导出快照为 CSV / JSON / Markdown 报表."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..services.snapshot import snapshot_json, take_snapshot
from ..storage import load_portfolio
from ..watchlist import load_watchlist
from ._common import _sanitize

# 持仓 CSV 导出列 (按出现顺序)
_CSV_COLS = [
    "symbol", "name", "market", "currency", "price", "change_pct",
    "quantity", "avg_cost", "market_value", "cost", "pnl", "pnl_pct",
    "today_pnl",
]

# 持仓 Markdown 表格列
_MD_COLS = [
    ("symbol", "代码"), ("name", "名称"), ("market", "市场"),
    ("price", "现价"), ("change_pct", "涨跌%"), ("quantity", "数量"),
    ("avg_cost", "成本"), ("market_value", "市值"), ("cost", "成本额"),
    ("pnl", "盈亏"), ("pnl_pct", "盈亏%"),
]


def _export_csv(view: pd.DataFrame) -> str:
    """持仓视图 → CSV 字符串 (带 BOM 便于 Excel 打开)."""
    cols = [c for c in _CSV_COLS if c in view.columns]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for _, r in view.iterrows():
        writer.writerow(_sanitize(r.to_dict()))
    return "\ufeff" + buf.getvalue()


def _fmt_cell(v) -> str:
    """Markdown 单元格格式化: 空值 → —, 数值 → 千分位."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:,.2f}"
    return str(v)


def _export_md(base: str, view: pd.DataFrame, summary: dict, issues: list[str]) -> str:
    """持仓视图 + 汇总 → Markdown 报表."""
    lines = [
        "# 投资组合快照",
        "",
        f"- 基础货币: {base}",
        f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}",
        "",
    ]
    m = summary
    if m.get("total_value") is not None:
        lines.append(f"- 总市值: {m['total_value']:,.2f} {base}")
    if m.get("total_pnl") is not None:
        pct = f" ({m['total_pnl_pct']:+.2%})" if m.get("total_pnl_pct") is not None else ""
        lines.append(f"- 浮动盈亏: {m['total_pnl']:+,.2f} {base}{pct}")
    if m.get("today_pnl") is not None:
        lines.append(f"- 今日估算: {m['today_pnl']:+,.2f} {base}")
    lines.append("")

    if view.empty:
        lines.append("无可用持仓数据。")
        return "\n".join(lines)

    lines.append("## 持仓明细")
    lines.append("")
    cols = [c for c, _ in _MD_COLS if c in view.columns]
    header = "| " + " | ".join(label for c, label in _MD_COLS if c in cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    lines.append(header)
    lines.append(sep)
    for _, r in view.iterrows():
        cells = [_fmt_cell(r.get(c)) for c in cols]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    # 市场分布
    by_market = m.get("by_market")
    if by_market is not None and hasattr(by_market, "items") and len(by_market):
        lines.append("## 市场分布")
        lines.append("")
        total = m.get("total_value") or 0
        for k, v in by_market.items():
            pct = v / total if total else 0
            lines.append(f"- {k}: {v:,.2f} ({pct:.1%})")
        lines.append("")

    # 币种分布
    by_currency = m.get("by_currency")
    if by_currency is not None and hasattr(by_currency, "items") and len(by_currency):
        lines.append("## 币种分布")
        lines.append("")
        total = m.get("total_value") or 0
        for k, v in by_currency.items():
            pct = v / total if total else 0
            lines.append(f"- {k}: {v:,.2f} ({pct:.1%})")
        lines.append("")

    if issues:
        lines.append("## 数据问题")
        lines.append("")
        for i in issues:
            lines.append(f"- {i}")
        lines.append("")
    return "\n".join(lines)


def cmd_export(args) -> None:
    """导出投资组合快照为 CSV / JSON / Markdown 报表."""
    portfolio = load_portfolio(args.portfolio)
    if args.base:
        portfolio["base_currency"] = args.base.upper()
    watchlist = load_watchlist(args.watchlist_file)
    base, view, summary, wview, issues = take_snapshot(
        portfolio, watchlist, watch_name=args.watchlist,
        prefer_akshare=args.akshare, use_ibkr=args.ibkr,
    )
    if args.format == "json":
        payload = snapshot_json(base, view, summary, wview, issues)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    elif args.format == "csv":
        text = _export_csv(view)
    else:  # md
        text = _export_md(base, view, summary, issues)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"✅ 快照已导出: {args.output}")
    else:
        print(text)
