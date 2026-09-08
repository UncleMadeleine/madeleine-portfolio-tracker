"""统一命令行入口: python -m tracker.cli <子命令> [选项]

子命令:
  snapshot  组合 + 自选快照 (持仓 / 自选提醒 / 阈值触发)
  quote     查询单个或多个代码实时行情
  watchlist 自选股管理 (list / add / remove) 与阈值提醒
  report    导出自选监控阈值报告 (md / csv / json)
  fx        汇率查询
  history   历史价格 (近 N 个月)
  kline     K线蜡烛图 (交互式 HTML + 摘要, 含成交量/均线/周月K)
  sync      从 IBKR 账户同步持仓
  cache     行情磁盘缓存管理 (info / clear)

所有子命令均支持 --json 输出机器可读结果, 便于脚本与 AI 消费。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import prices
from .fx import get_fx_rates
from .snapshot import DEFAULT_PORTFOLIO, load_portfolio, save_portfolio
from .symbols import parse
from .watchlist import (
    DEFAULT_WATCHLIST,
    build_watchlist_view,
    entries_for,
    load_watchlist,
    parse_lists,
    save_watchlist,
    sort_watchlist,
    triggered_entries,
)


VERSION = "1.0.0"


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> records, NaN/NaT 转 None 便于 JSON 序列化."""
    if df is None or df.empty:
        return []
    return df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")


def _sym(e: dict) -> str:
    raw = str(e.get("symbol", "")).strip()
    try:
        return parse(raw).yahoo
    except ValueError:
        return raw.upper()


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _finish_with_error(msg: str, code: int = 2) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    raise SystemExit(code)


# ---------- snapshot / sync (复用独立模块) ----------


def cmd_snapshot(args) -> None:
    from . import snapshot

    snapshot.run_snapshot(args)


def cmd_sync(args) -> None:
    from . import ibkr_sync

    ibkr_sync.run_sync(args)


# ---------- quote ----------


def cmd_quote(args) -> None:
    quotes, errors, notes = prices.get_quotes(
        args.symbols, prefer_akshare=args.akshare, use_ibkr=args.ibkr
    )
    rows: list[dict] = []
    for s in args.symbols:
        try:
            p = parse(s)
        except ValueError as e:
            rows.append({"symbol": s, "error": str(e)})
            continue
        q = quotes.get(p.yahoo)
        if q is None:
            rows.append(
                {
                    "symbol": p.yahoo,
                    "error": errors.get(p.yahoo) or errors.get(s) or "行情缺失",
                }
            )
        else:
            rows.append(
                {
                    "symbol": q.symbol,
                    "name": q.name,
                    "price": q.price,
                    "currency": q.currency,
                    "prev_close": q.prev_close,
                    "change_pct": q.change_pct,
                }
            )
    if args.json:
        _print_json({"quotes": rows, "notes": notes})
        return
    print("\n=== 实时行情 ===")
    for r in rows:
        if "error" in r:
            print(f"  {r['symbol']}: ⚠ {r['error']}")
        else:
            chg = (
                f" ({r['change_pct']:+.2f}%)" if r["change_pct"] is not None else ""
            )
            print(
                f"  {r['symbol']:>12}  {r['price']:,.3f} {r['currency']}"
                f"{chg}  {r['name'] or ''}"
            )
    for n in notes:
        print(f"  ℹ {n}")


# ---------- watchlist ----------


def watchlist_add(args) -> None:
    if not args.symbols:
        _finish_with_error("请指定要添加的代码, 如: watchlist add AAPL 600519.SS")
    data = load_watchlist(args.file)
    entries = data.setdefault("watchlist", [])
    lists = parse_lists(args.wl_list) if args.wl_list else []
    changed: list[str] = []
    for s in args.symbols:
        try:
            p = parse(s)
        except ValueError as e:
            changed.append(f"跳过 {s}: {e}")
            continue
        found = next((e for e in entries if _sym(e) == p.yahoo), None)
        if found is None:
            e: dict = {"symbol": p.yahoo, "lists": lists or ["默认"]}
            if args.upper1 is not None:
                e["upper_1"] = args.upper1
            if args.upper2 is not None:
                e["upper_2"] = args.upper2
            if args.lower1 is not None:
                e["lower_1"] = args.lower1
            if args.lower2 is not None:
                e["lower_2"] = args.lower2
            if args.note is not None:
                e["note"] = args.note
            entries.append(e)
            changed.append(f"新增 {p.yahoo}")
        else:
            cur = found.setdefault("lists", [])
            for name in lists:
                if name not in cur:
                    cur.append(name)
            for key, val in (
                ("upper_1", args.upper1),
                ("upper_2", args.upper2),
                ("lower_1", args.lower1),
                ("lower_2", args.lower2),
            ):
                if val is not None:
                    found[key] = val
            if args.note is not None:
                found["note"] = args.note
            changed.append(f"更新 {p.yahoo}")
    save_watchlist(data, args.file)
    print("✅ " + "; ".join(changed))


def watchlist_remove(args) -> None:
    if not args.symbols:
        _finish_with_error("请指定要删除的代码, 如: watchlist remove AAPL")
    data = load_watchlist(args.file)
    targets: set[str] = set()
    for s in args.symbols:
        try:
            targets.add(parse(s).yahoo)
        except ValueError:
            targets.add(s.strip().upper())
    scope = args.wl_list
    remaining: list[dict] = []
    removed: list[str] = []
    for e in data.get("watchlist", []):
        sym = _sym(e)
        if sym in targets:
            if scope:
                # 仅从指定列表移除; 仍属其他列表则保留
                cur = e.get("lists") or []
                if scope in cur:
                    cur = [n for n in cur if n != scope]
                    removed.append(sym)
                    if cur:
                        e["lists"] = cur
                        remaining.append(e)
                        continue
                    # 已不属于任何列表 → 整体删除
                    continue
                # 不在指定列表中 → 保留不动
                remaining.append(e)
                continue
            else:
                removed.append(sym)
        else:
            remaining.append(e)
    data["watchlist"] = remaining
    save_watchlist(data, args.file)
    if removed:
        print(f"✅ 已删除: {', '.join(removed)}")
    else:
        print("未找到可删除的代码 (可能不在自选中)")
    if args.json:
        _print_json({"removed": removed})


def watchlist_list(args) -> None:
    data = load_watchlist(args.file)
    entries = entries_for(data, args.wl_list)
    if args.no_quotes:
        if args.json:
            _print_json({"entries": entries, "lists": sorted({n for e in entries for n in e.get("lists", [])})})
            return
        print(f"\n=== 自选配置 ({args.wl_list or '全部'}) ===")
        if not entries:
            print("(空)")
        for e in entries:
            parts = [str(e.get("symbol"))]
            if e.get("lists"):
                parts.append("[" + ",".join(e["lists"]) + "]")
            for k in ("upper_1", "upper_2", "lower_1", "lower_2"):
                if e.get(k) is not None:
                    parts.append(f"{k}={e[k]}")
            if e.get("note"):
                parts.append(f"#{e['note']}")
            print("  " + " ".join(parts))
        return

    symbols = [str(e["symbol"]) for e in entries]
    quotes, errors, notes = prices.get_quotes(
        symbols, prefer_akshare=args.akshare, use_ibkr=args.ibkr
    )
    wview, wissues = build_watchlist_view(entries, quotes)
    trig = triggered_entries(wview)
    issues = [f"{k}: {v}" for k, v in errors.items()] + wissues + notes
    if args.json:
        _print_json(
            {
                "scope": args.wl_list or "全部",
                "watchlist": _records(wview),
                "triggered": _records(trig),
                "issues": issues,
            }
        )
        return
    print(f"\n=== 自选观察 ({args.wl_list or '全部'}) ===")
    if wview.empty:
        print("(空)")
    else:
        cols = [
            c for c in
            ("symbol", "name", "price", "change_pct", "status",
             "upper_1", "upper_2", "lower_1", "lower_2", "note")
            if c in wview.columns
        ]
        with pd.option_context(
            "display.float_format", "{:,.2f}".format,
            "display.width", 200, "display.max_columns", None,
        ):
            print(wview[cols].to_string(index=False))
    if not trig.empty:
        print(f"\n🔔 阈值提醒 ({len(trig)}):")
        for _, r in trig.iterrows():
            print(f"  {r['symbol']} {r['status']} 现价 {r['price']:,.2f} {r['currency']}")
    else:
        print("\n自选中暂无阈值触发。")
    for i in issues:
        print(f"  ⚠ {i}")


def cmd_watchlist(args) -> None:
    if args.action == "add":
        watchlist_add(args)
    elif args.action == "remove":
        watchlist_remove(args)
    else:
        watchlist_list(args)



# ---------- portfolio ----------


def portfolio_add(args) -> None:
    if not args.symbols:
        _finish_with_error("请指定要添加的代码, 如: portfolio add AAPL --quantity 10 --avg-cost 180")
    if args.quantity is None:
        _finish_with_error("请指定持仓数量 --quantity")
    data = load_portfolio(args.portfolio)
    holdings = data.setdefault("holdings", [])
    changed: list[str] = []
    for s in args.symbols:
        try:
            p = parse(s)
        except ValueError as e:
            changed.append(f"跳过 {s}: {e}")
            continue
        found = next((h for h in holdings if _sym(h) == p.yahoo), None)
        if found is None:
            h: dict = {"symbol": p.yahoo, "quantity": args.quantity}
            if args.avg_cost is not None:
                h["avg_cost"] = args.avg_cost
            holdings.append(h)
            changed.append(f"新增 {p.yahoo} 数量 {args.quantity}")
        else:
            found["quantity"] = args.quantity
            if args.avg_cost is not None:
                found["avg_cost"] = args.avg_cost
            changed.append(f"更新 {p.yahoo} 数量 {args.quantity}")
    save_portfolio(data, args.portfolio)
    print("✅ " + "; ".join(changed))


def portfolio_remove(args) -> None:
    if not args.symbols:
        _finish_with_error("请指定要删除的代码, 如: portfolio remove AAPL")
    data = load_portfolio(args.portfolio)
    targets: set[str] = set()
    for s in args.symbols:
        try:
            targets.add(parse(s).yahoo)
        except ValueError:
            targets.add(s.strip().upper())
    original = data.get("holdings", [])
    remaining = [h for h in original if _sym(h) not in targets]
    removed = [_sym(h) for h in original if _sym(h) in targets]
    data["holdings"] = remaining
    save_portfolio(data, args.portfolio)
    if removed:
        print(f"✅ 已删除: {', '.join(removed)}")
    else:
        print("未找到可删除的持仓")
    if args.json:
        _print_json({"removed": removed})


def portfolio_list(args) -> None:
    data = load_portfolio(args.portfolio)
    holdings = data.get("holdings", [])
    if args.json:
        _print_json({"base_currency": data.get("base_currency", "CNY"), "holdings": holdings})
        return
    base = data.get("base_currency", "CNY")
    print(f"\n=== 持仓 ({base}) ===")
    if not holdings:
        print("(空)")
        return
    rows = []
    for h in holdings:
        rows.append({
            "symbol": _sym(h),
            "quantity": h.get("quantity"),
            "avg_cost": h.get("avg_cost"),
        })
    with pd.option_context("display.float_format", "{:,.2f}".format, "display.width", 120):
        print(pd.DataFrame(rows).to_string(index=False))


def portfolio_set_base(args) -> None:
    currency = args.currency or (args.symbols[0] if args.symbols else None)
    if not currency:
        _finish_with_error("请指定基础货币, 如: portfolio set-base USD")
    data = load_portfolio(args.portfolio)
    old = data.get("base_currency", "CNY")
    data["base_currency"] = currency.upper()
    save_portfolio(data, args.portfolio)
    print(f"✅ 基础货币: {old} → {data['base_currency']}")


def cmd_portfolio(args) -> None:
    if args.action == "add":
        portfolio_add(args)
    elif args.action == "remove":
        portfolio_remove(args)
    elif args.action == "set-base":
        portfolio_set_base(args)
    else:
        portfolio_list(args)

# ---------- report ----------

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


def _sanitize(d: dict) -> dict:
    return {
        k: (None if (v is None or (isinstance(v, float) and pd.isna(v))) else v)
        for k, v in d.items()
    }


def _fmt_num(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{digits}f}"


def _fmt_pct(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:+.1f}%"


def _build_report(args) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, list[str]], list[str]]:
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
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLS, extrasaction="ignore")
    writer.writeheader()
    for _, r in wview.iterrows():
        row = _sanitize(r.to_dict())
        row["lists"] = "、".join(sym_lists.get(row.get("symbol"), []))
        writer.writerow(row)
    return "\ufeff" + buf.getvalue()


def _report_md(payload, wview, sym_lists, issues) -> str:
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


# ---------- fx ----------


def cmd_fx(args) -> None:
    base = args.base.upper()
    currencies = [c.upper() for c in (args.currencies or ["CNY", "USD", "EUR", "GBP", "HKD", "JPY", "CAD", "AUD"])]
    rates, missing = get_fx_rates(base, currencies, use_ibkr=args.ibkr)
    if args.json:
        _print_json({"base": base, "rates": rates, "missing": missing})
        return
    print(f"\n=== 汇率 (以 1 外币兑 {base} 计) ===")
    for c in currencies:
        r = rates.get(c)
        if r is not None:
            print(f"  1 {c} = {r:.6f} {base}")
        else:
            print(f"  1 {c} = 缺失")
    if missing:
        print(f"\n⚠ 缺失: {', '.join(missing)}")


# ---------- history ----------


def cmd_history(args) -> None:
    df = prices.get_history(args.symbol, months=args.months, prefer_akshare=args.akshare, use_ibkr=args.ibkr)
    if args.json:
        recs = df.copy()
        recs["date"] = recs["date"].astype(str)
        _print_json(recs.to_dict(orient="records"))
        return
    close = df["close"].astype(float)
    change = (close.iloc[-1] / close.iloc[0] - 1) * 100 if len(close) >= 2 else None
    print(f"\n=== 历史行情 {args.symbol} (近 {args.months} 个月, 共 {len(df)} 个交易日) ===")
    print(
        f"区间: {df['date'].iloc[0]:%Y-%m-%d} → {df['date'].iloc[-1]:%Y-%m-%d}"
        f"  · 收 {close.iloc[0]:,.3f} → {close.iloc[-1]:,.3f}"
        + (f" ({change:+.2f}%)" if change is not None else "")
        + f"  · 区间高 {close.max():,.3f} / 低 {close.min():,.3f}"
    )
    tail = df.tail(args.rows)
    tail = tail.copy()
    tail["date"] = tail["date"].astype(str)
    with pd.option_context(
        "display.float_format", "{:,.3f}".format, "display.width", 160
    ):
        print("\n最近交易日:")
        print(tail.to_string(index=False))


# ---------- kline ----------

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _fmt_vol(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    for unit, div in (("亿", 1e8), ("万", 1e4)):
        if v >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:,.0f}"


def cmd_kline(args) -> None:
    import webbrowser

    from . import charting

    try:
        p = parse(args.symbol)
    except ValueError as e:
        _finish_with_error(str(e))
    try:
        df = prices.get_ohlc(
            args.symbol, months=args.months,
            prefer_akshare=args.akshare, refresh=args.refresh,
            use_ibkr=args.ibkr,
        )
    except Exception as e:
        _finish_with_error(f"{args.symbol}: K线数据获取失败 ({e})")
    if args.period != "daily":
        df = charting.resample_ohlc(df, args.period)
    if df.empty:
        _finish_with_error(f"{p.yahoo}: 无有效K线数据")

    mas = charting.parse_ma_periods(args.ma)
    s = charting.summarize_ohlc(df, mas)
    if args.json:
        recs = df.copy()
        recs["date"] = recs["date"].dt.strftime("%Y-%m-%d")
        _print_json(
            {
                "symbol": p.yahoo,
                "currency": p.currency,
                "period": args.period,
                "months": args.months,
                "bars": len(df),
                "summary": s,
                "data": recs.round(6).to_dict(orient="records"),
            }
        )
        return

    period_label = charting.PERIOD_LABELS.get(args.period, args.period)
    print(
        f"\n=== K线 {p.yahoo} · {period_label} · 近 {args.months} 个月"
        f" ({s['bars']} 根) ==="
    )
    chg = f" ({s['change_pct']:+.2f}%)" if s["change_pct"] is not None else ""
    print(
        f"区间: {s['first_date']} → {s['last_date']}"
        f"  · 收 {s['first_close']:,.3f} → {s['last_close']:,.3f}{chg}"
    )
    print(
        f"最新 {s['last_date']}: 开 {s['last_open']:,.3f}  高 {s['last_high']:,.3f}"
        f"  低 {s['last_low']:,.3f}  收 {s['last_close']:,.3f}  量 {_fmt_vol(s['last_volume'])}"
    )
    if s["ma"]:
        parts = [
            f"MA{n[2:]} {v:,.3f}" if v is not None else f"MA{n[2:]} —"
            for n, v in s["ma"].items()
        ]
        print("均线: " + "  ".join(parts))
    print(
        f"区间最高 {s['period_high']:,.3f} ({s['period_high_date']})"
        f"  · 最低 {s['period_low']:,.3f} ({s['period_low_date']})"
    )

    fig = charting.build_candlestick_fig(
        df, p.yahoo, currency=p.currency, mas=mas,
        show_volume=not args.no_volume, period=args.period,
    )
    out = Path(args.output) if args.output else DATA_DIR / f"kline_{p.yahoo.replace('.', '_')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(charting.fig_to_html(fig), encoding="utf-8")
    print(f"\n✅ K线图已生成: {out}")
    if args.open_browser:
        webbrowser.open(out.resolve().as_uri())
        print("已在浏览器中打开。")


# ---------- cache ----------


def cmd_cache(args) -> None:
    from . import cache as cache_mod

    if args.action == "clear":
        n = cache_mod.clear()
        if args.json:
            _print_json({"cleared": n})
        else:
            print(f"✅ 已清空行情缓存 ({n} 条)")
        return
    info = cache_mod.info()
    if args.json:
        _print_json(info)
        return
    print("\n=== 行情缓存 ===")
    print(f"  数据库: {info['db']}")
    print(f"  TTL: {info['ttl_seconds']}s")
    print(f"  总条数: {info['total']} (有效 {info['fresh']} / 过期 {info['stale']})")
    print(
        f"  K线缓存: {info['ohlc_total']} 组 (有效 {info['ohlc_fresh']}"
        f" / TTL {info['ohlc_ttl_seconds']}s)"
    )
    print(f"  文件大小: {info['size_bytes']:,} bytes")
    if info["newest_at"]:
        print(
            f"  最近更新: {datetime.fromtimestamp(info['newest_at']):%Y-%m-%d %H:%M:%S}"
        )


# ---------- 参数与入口 ----------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tracker",
        description="OpenBB Portfolio Tracker 命令行工具 (持仓 / 自选 / 行情 / 汇率 / 缓存)",
    )
    ap.add_argument("--version", action="version", version=f"tracker {VERSION}")
    sub = ap.add_subparsers(dest="command", metavar="<子命令>", required=True)

    p_snap = sub.add_parser("snapshot", help="组合 + 自选快照")
    p_snap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    p_snap.add_argument("--watchlist-file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径")
    p_snap.add_argument("--watchlist", default=None, help="只查看某个子自选列表 (默认全部)")
    p_snap.add_argument("--base", default=None, help="覆盖基础货币, 如 USD")
    p_snap.add_argument("--akshare", action="store_true", help="A股/港股优先走 akshare")
    p_snap.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 行情")
    p_snap.add_argument("--json", action="store_true", help="输出 JSON")
    p_snap.set_defaults(func=cmd_snapshot)

    p_q = sub.add_parser("quote", help="查询实时行情")
    p_q.add_argument("symbols", nargs="+", help="Yahoo 代码, 如 AAPL 600519.SS")
    p_q.add_argument("--akshare", action="store_true")
    p_q.add_argument("--ibkr", action="store_true")
    p_q.add_argument("--json", action="store_true")
    p_q.set_defaults(func=cmd_quote)

    p_w = sub.add_parser("watchlist", help="自选股管理 (list/add/remove) 与阈值提醒")
    p_w.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    p_w.add_argument("symbols", nargs="*", help="add/remove 的目标代码")
    p_w.add_argument("--file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径")
    p_w.add_argument("--list", dest="wl_list", default=None, help="所属列表名 (add 时指定 / list 时过滤)")
    p_w.add_argument("--upper1", type=float, help="上限 I")
    p_w.add_argument("--upper2", type=float, help="上限 II")
    p_w.add_argument("--lower1", type=float, help="下限 I")
    p_w.add_argument("--lower2", type=float, help="下限 II")
    p_w.add_argument("--note", help="备注")
    p_w.add_argument("--akshare", action="store_true")
    p_w.add_argument("--ibkr", action="store_true")
    p_w.add_argument("--json", action="store_true", help="输出 JSON")
    p_w.add_argument("--no-quotes", action="store_true", help="list 时不拉行情, 仅展示配置")
    p_w.set_defaults(func=cmd_watchlist)

    p_p = sub.add_parser("portfolio", help="持仓管理 (list/add/remove/set-base)")
    p_p.add_argument("action", nargs="?", choices=["list", "add", "remove", "set-base"],
                     default="list")
    p_p.add_argument("symbols", nargs="*", help="add/remove 的目标代码")
    p_p.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO), help="portfolio.json 路径")
    p_p.add_argument("--quantity", type=float, help="持仓数量 (add 时必填)")
    p_p.add_argument("--avg-cost", type=float, help="成本价 (当地货币, add 时可选)")
    p_p.add_argument("--currency", help="基础货币 (set-base 时指定, 如 CNY/USD)")
    p_p.add_argument("--json", action="store_true", help="输出 JSON")
    p_p.set_defaults(func=cmd_portfolio)

    p_r = sub.add_parser("report", help="导出自选监控阈值报告 (md/csv/json)")
    p_r.add_argument("--format", "-f", choices=["md", "csv", "json"], default="md",
                     help="输出格式 (默认 md)")
    p_r.add_argument("--watchlist", "-w", default=None, help="只导出某个子自选列表 (默认全部)")
    p_r.add_argument("--file", default=str(DEFAULT_WATCHLIST), help="watchlist.json 路径")
    p_r.add_argument("--sort", choices=["default", "severity", "change_desc", "change_asc"],
                     default="default", help="列表内排序方式")
    p_r.add_argument("--akshare", action="store_true")
    p_r.add_argument("--ibkr", action="store_true")
    p_r.add_argument("--output", "-o", default=None, help="输出文件路径 (缺省打印到终端)")
    p_r.set_defaults(func=cmd_report)

    p_fx = sub.add_parser("fx", help="汇率查询")
    p_fx.add_argument("base", help="基础货币, 如 USD")
    p_fx.add_argument("currencies", nargs="*", help="目标货币, 缺省常用币种")
    p_fx.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 汇率")
    p_fx.add_argument("--json", action="store_true")
    p_fx.set_defaults(func=cmd_fx)

    p_h = sub.add_parser("history", help="历史价格 (近 N 个月)")
    p_h.add_argument("symbol")
    p_h.add_argument("--months", type=int, default=12)
    p_h.add_argument("--rows", type=int, default=10, help="表格模式打印最近 N 行")
    p_h.add_argument("--akshare", action="store_true")
    p_h.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 行情")
    p_h.add_argument("--json", action="store_true")
    p_h.set_defaults(func=cmd_history)

    p_k = sub.add_parser("kline", help="K线蜡烛图 (生成交互式 HTML, 含成交量/均线)")
    p_k.add_argument("symbol", help="Yahoo 代码, 如 AAPL 600519.SS")
    p_k.add_argument("--months", type=int, default=12, help="拉取近 N 个月日线")
    p_k.add_argument("--period", choices=["daily", "weekly", "monthly"], default="daily",
                     help="K线周期 (默认日K)")
    p_k.add_argument("--ma", default="5,20,60", help="均线周期, 逗号分隔 (如 5,10,20,60)")
    p_k.add_argument("--no-volume", action="store_true", help="隐藏成交量副图")
    p_k.add_argument("--refresh", action="store_true", help="忽略缓存强制刷新")
    p_k.add_argument("--akshare", action="store_true")
    p_k.add_argument("--ibkr", action="store_true", help="优先使用 IBKR 行情")
    p_k.add_argument("--output", "-o", default=None,
                     help="HTML 输出路径 (默认 data/kline_<代码>.html)")
    p_k.add_argument("--open", dest="open_browser", action="store_true",
                     help="生成后自动在浏览器打开")
    p_k.add_argument("--json", action="store_true", help="输出 JSON 数据 (不生成图表)")
    p_k.set_defaults(func=cmd_kline)

    p_sync = sub.add_parser("sync", help="从 IBKR 账户同步持仓")
    p_sync.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    p_sync.add_argument("--dry-run", action="store_true", help="仅预览, 不写入")
    p_sync.add_argument("--json", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_c = sub.add_parser("cache", help="行情磁盘缓存管理")
    p_c.add_argument("action", nargs="?", choices=["info", "clear"], default="info")
    p_c.add_argument("--json", action="store_true")
    p_c.set_defaults(func=cmd_cache)

    return ap


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
