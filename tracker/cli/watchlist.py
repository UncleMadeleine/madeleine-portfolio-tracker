"""watchlist 子命令: 自选股管理 (list / add / remove) 与阈值提醒."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .. import importer, prices, storage
from ..symbols import parse
from ..services.watchlist import (
    build_watchlist_view,
    entries_for,
    triggered_entries,
)
from ..watchlist import load_watchlist, parse_lists, save_watchlist
from ._common import _finish_with_error, _print_json, _records, _sym


def watchlist_add(args) -> None:
    """添加代码到自选 (已存在则合并列表与阈值)."""
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
            if args.metric is not None:
                e["metric"] = args.metric
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
                ("metric", args.metric),
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
    if args.json:
        _print_json({"changed": changed, "watchlist": data.get("watchlist", [])})
        return
    print("✅ " + "; ".join(changed))


def watchlist_remove(args) -> None:
    """从自选删除代码 (可仅从指定列表移除)."""
    if not args.symbols:
        _finish_with_error("请指定要删除的代码, 如: watchlist remove AAPL")
    data = load_watchlist(args.file)
    targets: set[str] = set()
    for s in args.symbols:
        try:
            targets.add(parse(s).yahoo)
        except ValueError:
            targets.add(s.strip().upper())
    # --list 允许多个列表 (逗号分隔, 与 add/entries_for 行为一致);
    # 直接拿原串比对单个列表名会永远匹配不上, 导致静默删不掉
    scope = set(parse_lists(args.wl_list)) if args.wl_list else set()
    remaining: list[dict] = []
    removed: list[str] = []
    for e in data.get("watchlist", []):
        sym = _sym(e)
        if sym in targets:
            if scope:
                # 仅从指定列表移除; 仍属其他列表则保留
                cur = e.get("lists") or []
                keep = [n for n in cur if n not in scope]
                if len(keep) != len(cur):
                    removed.append(sym)
                    if keep:
                        e["lists"] = keep
                        remaining.append(e)
                    # 已不属于任何列表 → 整体删除 (不入 remaining)
                else:
                    # 不在指定列表中 → 保留不动
                    remaining.append(e)
            else:
                removed.append(sym)
        else:
            remaining.append(e)
    data["watchlist"] = remaining
    save_watchlist(data, args.file)
    if args.json:
        _print_json({"removed": removed})
        return
    if removed:
        print(f"✅ 已删除: {', '.join(removed)}")
    else:
        print("未找到可删除的代码 (可能不在自选中)")


def watchlist_list(args) -> None:
    """列出自选配置 (可选 --no-quotes 仅展示配置)."""
    data = load_watchlist(args.file)
    entries = entries_for(data, args.wl_list)
    if args.no_quotes:
        if args.json:
            _print_json(
                {
                    "entries": entries,
                    "lists": sorted({n for e in entries for n in e.get("lists", [])}),
                }
            )
            return
        print(f"\n=== 自选配置 ({args.wl_list or '全部'}) ===")
        if not entries:
            print("(空)")
        for e in entries:
            parts = [str(e.get("symbol"))]
            if e.get("lists"):
                parts.append("[" + ",".join(e["lists"]) + "]")
            for k in ("metric", "upper_1", "upper_2", "lower_1", "lower_2"):
                if e.get(k) is not None:
                    parts.append(f"{k}={e[k]}")
            if e.get("note"):
                parts.append(f"#{e['note']}")
            print("  " + " ".join(parts))
        return

    symbols = [str(e["symbol"]) for e in entries]
    quotes, errors, notes = prices.get_quotes(
        symbols,
        prefer_akshare=args.akshare,
        use_ibkr=args.ibkr,
        use_longport=getattr(args, "longport", False),
        crypto_source=getattr(args, "crypto_source", "auto"),
    )
    wview, wissues = build_watchlist_view(entries, quotes)
    trig = triggered_entries(wview)
    issues = [f"{k}: {v}" for k, v in errors.items()] + wissues
    if args.json:
        _print_json(
            {
                "scope": args.wl_list or "全部",
                "watchlist": _records(wview),
                "triggered": _records(trig),
                "issues": issues,
                "notes": notes,
            }
        )
        return
    print(f"\n=== 自选观察 ({args.wl_list or '全部'}) ===")
    if wview.empty:
        print("(空)")
    else:
        cols = [
            c
            for c in (
                "symbol",
                "name",
                "metric",
                "price",
                "change_pct",
                "status",
                "upper_1",
                "upper_2",
                "lower_1",
                "lower_2",
                "note",
            )
            if c in wview.columns
        ]
        with pd.option_context(
            "display.float_format",
            "{:,.2f}".format,
            "display.width",
            200,
            "display.max_columns",
            None,
        ):
            print(wview[cols].to_string(index=False))
    if not trig.empty:
        print(f"\n🔔 阈值提醒 ({len(trig)}):")
        for _, r in trig.iterrows():
            print(
                f"  {r['symbol']} {r['status']} 现价 {r['price']:,.2f} {r['currency']}"
            )
    else:
        print("\n自选中暂无阈值触发。")
    for i in issues:
        print(f"  ⚠ {i}")
    for n in notes:
        print(f"  ℹ {n}")


def watchlist_import_longport(args) -> None:
    """从长桥账户自选分组导入 (归一为 Yahoo 规范代码, 按组名建列表).

    合并语义与持仓导入一致: 已有代码只并列表/补空字段, 其余保留;
    --overwrite 清空现有自选后写入。写前自动备份 .bak。
    """
    from .. import longport as lp

    try:
        groups = lp.fetch_watchlist_groups()
    except Exception as e:
        _finish_with_error(
            f"{e} (请先完成登录: 运行 tracker longport-login, 或在 UI 导入页长桥标签登录)"
        )
    rows, skipped = lp.watchlist_to_rows(groups)

    if args.dry_run:
        if args.json:
            _print_json(
                {
                    "dry_run": True,
                    "groups": len(groups),
                    "entries": rows,
                    "skipped": skipped,
                }
            )
            return
        print(f"\n=== 长桥账户自选 ({len(groups)} 组, {len(rows)} 个规范代码) ===")
        if rows:
            df = pd.DataFrame(rows)
            df["lists"] = df["lists"].apply(lambda v: ", ".join(v))
            with pd.option_context("display.max_colwidth", 40, "display.width", 160):
                print(df.to_string(index=False))
        else:
            print("(无可导入的自选)")
        for s in skipped:
            print(f"  ⚠ {s}")
        print(
            f"\n(dry-run, 未写入; 导入方式: {'覆盖' if args.overwrite else '追加合并'})"
        )
        return

    data = {"watchlist": []} if args.overwrite else load_watchlist(args.file)
    merged, stats = importer.merge_watchlist(
        data.get("watchlist", []),
        rows,
        importer.MODE_OVERWRITE if args.overwrite else importer.MODE_APPEND,
    )
    backup = None
    if args.overwrite:
        p = Path(args.file)
        if p.exists():
            backup = storage.backup_file(p)
    data["watchlist"] = merged
    save_watchlist(data, args.file)
    verb = "覆盖写入" if args.overwrite else "追加合并"
    if args.json:
        _print_json(
            {
                "written": args.file,
                "mode": verb,
                "backup": backup,
                "added": stats["added"],
                "updated": stats["updated"],
                "total": len(merged),
                "skipped": skipped,
            }
        )
        return

    print(f"\n=== 长桥账户自选 ({len(groups)} 组, {len(rows)} 个规范代码) ===")
    if rows:
        df = pd.DataFrame(rows)
        df["lists"] = df["lists"].apply(lambda v: ", ".join(v))
        with pd.option_context("display.max_colwidth", 40, "display.width", 160):
            print(df.to_string(index=False))
    else:
        print("(无可导入的自选)")
    for s in skipped:
        print(f"  ⚠ {s}")
    print(
        f"\n✅ 已写入 {args.file} ({verb}: 新增 {len(stats['added'])} · "
        f"更新 {len(stats['updated'])}, 共 {len(merged)} 条自选)"
    )


def cmd_watchlist(args) -> None:
    """watchlist 子命令分发: list / add / remove / import-longport."""
    if args.action == "add":
        watchlist_add(args)
    elif args.action == "remove":
        watchlist_remove(args)
    elif args.action == "import-longport":
        watchlist_import_longport(args)
    else:
        watchlist_list(args)
