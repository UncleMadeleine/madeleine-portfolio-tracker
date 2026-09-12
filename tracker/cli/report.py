"""report 子命令: 导出自选监控阈值报告 (md / csv / json)."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .. import prices
from ..watchlist import (
    build_watchlist_view,
    entries_for,
    load_watchlist,
    sort_watchlist,
    triggered_entries,
)
from ._common import _fmt_num, _fmt_pct, _records, _sanitize, _sym

_MD_COLS = [
    ("symbol", "代码"), ("price", "现价"), ("change_pct", "涨跌%"),
    ("upper_1", "上限I"), ("upper_2", "上限II"),
    ("lower_1", "下限I"), ("lower_2", "下限II"),
    ("dist_upper_1_pct", "距上限I%"), ("dist_upper_2_pct", "距上限II%"),
    ("dist_lower_1_pct", "距下限I%"), ("dist_lower_2_pct", "距下限II%"),
    ("status", "状态"), ("note", "备注"),
]

_CSV_COLS = [
    "lists", "symbol", "name", "market", "currency", "price", "change_pct",
    "upper_1", "upper_2", "lower_1", "lower_2", "status",
    "dist_upper_1_pct", "dist_upper_2_pct", "dist_lower_1_pct", "dist_lower_2_pct",
    "note", "triggered",
]


def _build_report(args) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, list[str]], list[str]]:
    """拉取行情并构建自选视图 / 触发条目 / 列表映射 / 问题清单."""
    data = load_watchlist(args.file)
    entries = entries_for(data, args.watchlist)
    symbols = [str(e["symbol"]) for e in entries]
    quotes, errors, notes = prices.get_quotes(
        symbols, prefer_akshare=args.akshare, use_ibkr=args.ibkr
    )
    wview, wissues = build_watchlist_view(entries, quotes)
    wview = sort_watchlist(wview, args.sort)
    trig = triggered_entries(wview)
    issues = [f"{k}: {v}" for k, v in errors.items()] + wissues + notes
    sym_lists: dict[str, list[str]] = {}
    for e in entries:
        try:
            names = list(e.get("lists") or [])
            if args.watchlist:
                names = [args.watchlist]
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
            lines.append(
                f"- **{r['symbol']}** {r['status']} — 现价 "
                f"{r['price']:,.2f} {r['currency']}{note}"
            )
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
            for col, _ in _MD_COLS:
                v = r.get(col)
                if col in ("price", "upper_1", "upper_2", "lower_1", "lower_2"):
                    cells.append(_fmt_num(v))
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
        text = json.dumps(_report_payload(args, wview, trig, sym_lists, issues),
                          ensure_ascii=False, indent=2)
    elif args.format == "csv":
        text = _report_csv(wview, sym_lists)
    else:
        text = _report_md(
            _report_payload(args, wview, trig, sym_lists, issues),
            wview, sym_lists, issues,
        )
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"✅ 报告已写入: {args.output}")
    else:
        print(text)
