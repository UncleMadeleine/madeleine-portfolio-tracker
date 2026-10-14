"""report 子命令: 导出自选监控阈值报告 (md / csv / json)."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime

import pandas as pd

from ..services.watchlist import (
    entries_for,
    fetch_watchlist_view,
    sort_watchlist,
    triggered_entries,
)
from ..watchlist import load_watchlist, parse_lists
from ._common import _finish_with_error, _fmt_num, _fmt_pct, _records, _sanitize, _sym

# 指标基准 → 中文标签 (md 表 / 触发汇总)
_METRIC_LABELS = {"price": "现价", "pb": "市净率", "mcap": "市值"}
# 大数字友好格式: 亿/万亿缩写, 仅市值基准的阈值列用 (现价是每股价, 不套用)
_METRIC_CELL_COLS = ("upper_1", "upper_2", "lower_1", "lower_2")


def _metric_base_text(r: dict) -> str:
    """行记录 → 指标基准文案 (pb 倍数 / 市值 / 现价)."""
    metric = r.get("metric") or "price"
    bv = r.get("base_value")
    if metric == "pb" and bv is not None:
        return f"PB {bv:.2f}"
    if metric == "mcap" and bv is not None:
        return _fmt_mcap(bv)
    return f"现价 {r['price']:,.2f} {r['currency']}"


def _fmt_mcap(v) -> str:
    """市值 → 亿/万亿缩写 (threshold 同为绝对金额, 展示口径一致)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    v = float(v)
    if abs(v) >= 1e12:
        return f"{v / 1e12:,.2f}万亿"
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.2f}亿"
    return f"{v:,.0f}"

_MD_COLS = [
    ("symbol", "代码"),
    ("price", "现价"),
    ("change_pct", "涨跌%"),
    ("metric", "基准"),
    ("upper_1", "上限I"),
    ("upper_2", "上限II"),
    ("lower_1", "下限I"),
    ("lower_2", "下限II"),
    ("dist_upper_1_pct", "距上限I%"),
    ("dist_upper_2_pct", "距上限II%"),
    ("dist_lower_1_pct", "距下限I%"),
    ("dist_lower_2_pct", "距下限II%"),
    ("status", "状态"),
    ("note", "备注"),
]

_CSV_COLS = [
    "lists",
    "symbol",
    "name",
    "market",
    "currency",
    "metric",
    "price",
    "change_pct",
    "upper_1",
    "upper_2",
    "lower_1",
    "lower_2",
    "market_cap",
    "book_value",
    "base_value",
    "status",
    "dist_upper_1_pct",
    "dist_upper_2_pct",
    "dist_lower_1_pct",
    "dist_lower_2_pct",
    "note",
    "triggered",
]


def _build_report(
    args,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, list[str]], list[str]]:
    """拉取行情并构建自选视图 / 触发条目 / 列表映射 / 问题清单."""
    data = load_watchlist(args.file)
    entries = entries_for(data, args.watchlist)
    wview, issues = fetch_watchlist_view(
        entries,
        prefer_akshare=args.akshare,
        use_ibkr=args.ibkr,
        use_longport=getattr(args, "longport", False),
        crypto_source=getattr(args, "crypto_source", "auto"),
    )
    wview = sort_watchlist(wview, args.sort)
    trig = triggered_entries(wview)
    sym_lists: dict[str, list[str]] = {}
    # -w 支持逗号分隔多个列表; 取交集而不是把原始字符串当成单个列表名,
    # 否则报告会多出一个名为 "科技,美股" 的假分组
    scope = set(parse_lists(args.watchlist)) if args.watchlist else set()
    for e in entries:
        try:
            names = list(e.get("lists") or [])
            if scope:
                names = [n for n in names if n in scope]
            sym_lists.setdefault(_sym(e), names)
        except ValueError:
            pass
    return wview, trig, sym_lists, issues


def _report_payload(args, wview, trig, sym_lists, issues) -> dict:
    """构建 JSON 报告 payload."""
    total = len(wview)
    trig_n = len(trig)
    by_list: dict[str, list[dict]] = {}
    list_stats: dict[str, dict] = {}
    for _, r in wview.iterrows():
        rec = _sanitize(r.to_dict())
        names = sym_lists.get(rec["symbol"], [])
        rec["lists"] = names
        for name in names:
            by_list.setdefault(name, []).append(rec)
    for name, rows in by_list.items():
        list_stats[name] = {
            "total": len(rows),
            "triggered": sum(1 for x in rows if x.get("triggered")),
        }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope": args.watchlist or "全部",
        "summary": {"total": total, "triggered": trig_n, "by_list": list_stats},
        "triggered": _records(trig),
        "by_list": by_list,
        "issues": issues,
    }


def _report_csv(wview, sym_lists) -> str:
    """构建 CSV 报告 (带 BOM 便于 Excel 打开)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLS, extrasaction="ignore")
    writer.writeheader()
    for _, r in wview.iterrows():
        row = _sanitize(r.to_dict())
        row["lists"] = "、".join(sym_lists.get(row.get("symbol"), []))
        writer.writerow(row)
    return "\ufeff" + buf.getvalue()


def _report_md(payload, wview, sym_lists, issues) -> str:
    """构建 Markdown 报告 (触发汇总 + 按列表分表)."""
    scope = payload["scope"]
    s = payload["summary"]
    lines = [
        "# 自选监控阈值报告",
        "",
        f"- 生成时间: {payload['generated_at']}",
        f"- 范围: {scope}",
        f"- 代码数: {s['total']} · 触发 {s['triggered']}",
        "",
    ]

    if payload["triggered"]:
        lines.append("## 触发汇总")
        lines.append("")
        lines.append(f"🔔 共 {s['triggered']} 只触及阈值:")
        for r in payload["triggered"]:
            note = f" · {r['note']}" if r.get("note") else ""
            metric = r.get("metric") or "price"
            base = _metric_base_text(r)
            lines.append(f"- **{r['symbol']}** {r['status']} — {base}{note}")
        lines.append("")

    lines.append("## 按列表")
    lines.append("")
    groups: dict[str, list] = {}
    for _, r in wview.iterrows():
        for name in sym_lists.get(r["symbol"], []):
            groups.setdefault(name, []).append(r)
    if not groups:
        lines.append("(自选为空)")
    for name in sorted(groups.keys()):
        rows = groups[name]
        trig_n = sum(1 for r in rows if r.get("triggered"))
        lines.append(f"### {name} ({len(rows)} 只 · 触发 {trig_n})")
        lines.append("")
        header = "| " + " | ".join(label for _, label in _MD_COLS) + " |"
        sep = "|" + "|".join(["---"] * len(_MD_COLS)) + "|"
        lines.append(header)
        lines.append(sep)
        for r in rows:
            cells = []
            metric = r.get("metric") or "price"
            for col, _ in _MD_COLS:
                v = r.get(col)
                if col in _METRIC_CELL_COLS and metric == "mcap":
                    # 市值基准: 阈值同为绝对金额, 亿/万亿缩写展示
                    cells.append(_fmt_mcap(v))
                elif col in ("price", "upper_1", "upper_2", "lower_1", "lower_2"):
                    cells.append(_fmt_num(v))
                elif col == "metric":
                    cells.append(_METRIC_LABELS.get(v, "现价") if v else "现价")
                elif col == "change_pct" or col.startswith("dist_"):
                    cells.append(_fmt_pct(v))
                else:
                    cells.append("" if v is None else str(v))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    if issues:
        lines.append("## 数据问题")
        lines.append("")
        for i in issues:
            lines.append(f"- {i}")
        lines.append("")
    return "\n".join(lines)


def cmd_report(args) -> None:
    """导出自选监控阈值报告 (md / csv / json)."""
    wview, trig, sym_lists, issues = _build_report(args)
    if args.format == "json":
        text = json.dumps(
            _report_payload(args, wview, trig, sym_lists, issues),
            ensure_ascii=False,
            indent=2,
        )
    elif args.format == "csv":
        text = _report_csv(wview, sym_lists)
    else:
        text = _report_md(
            _report_payload(args, wview, trig, sym_lists, issues),
            wview,
            sym_lists,
            issues,
        )
    if args.output:
        from ._common import atomic_write_text

        try:
            atomic_write_text(args.output, text)
        except OSError as e:
            _finish_with_error(f"报告写入失败: {args.output} ({e})")
        print(f"✅ 报告已写入: {args.output}")
    else:
        print(text)
