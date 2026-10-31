"""portfolio 子命令: 持仓管理 (list / add / remove / set-base)."""

from __future__ import annotations

import pandas as pd

from ..storage import edit_portfolio, load_portfolio
from ..symbols import parse
from ._common import _finish_with_error, _print_json, _sym


def portfolio_add(args) -> None:
    """添加/更新持仓 (需指定 --quantity)."""
    if not args.symbols:
        _finish_with_error(
            "请指定要添加的代码, 如: portfolio add AAPL --quantity 10 --avg-cost 180"
        )
    if args.quantity is None:
        _finish_with_error("请指定持仓数量 --quantity")
    if args.quantity <= 0:
        _finish_with_error(f"持仓数量必须为正数, 得到: {args.quantity}")
    if args.avg_cost is not None and args.avg_cost < 0:
        _finish_with_error(f"成本价不能为负数, 得到: {args.avg_cost}")
    # 先解析再进锁: edit_portfolio 的 with 块正常退出时无条件保存, 校验若放在
    # 块内, 「没有可添加代码」也会在 --portfolio 指向的新路径创建空持仓文件。
    parsed: list = []
    skipped: list[str] = []
    for s in args.symbols:
        try:
            parsed.append(parse(s))
        except ValueError as e:
            skipped.append(f"跳过 {s}: {e}")
    if not parsed:
        if args.json:
            _print_json({"changed": [], "skipped": skipped})
        else:
            for s in skipped:
                print(f"⚠ {s}")
        _finish_with_error("没有可添加的有效代码")
    # 锁内读-改-写: 读也在临界区内, 并发进程的写入不会被本次的全量替换覆盖
    with edit_portfolio(args.portfolio) as data:
        holdings = data.setdefault("holdings", [])
        changed: list[str] = []
        for p in parsed:
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
                # 多钱包/多来源导入会维护 source_quantities 台账, quantity 是
                # 各来源分量之和。这里手工改写总数量后若台账仍与 quantity
                # 不一致, 下一次钱包导入会把旧分量加回来 (静默覆盖手改值)。
                # 与组合页保存口径一致: 数量对不上台账就作废台账。
                sq = found.get("source_quantities")
                if isinstance(sq, dict):
                    try:
                        same = args.quantity == sum(
                            float(v or 0) for v in sq.values()
                        )
                    except (TypeError, ValueError):
                        same = False
                    if not same:
                        found.pop("source_quantities", None)
                        # 保留原 import_source (与组合页保存口径一致): 同一来源
                        # 下次导入仍按「覆盖该来源」处理, 不会被当成新来源累加。
                        found["import_source"] = found.get("import_source") or ""
                changed.append(f"更新 {p.yahoo} 数量 {args.quantity}")
    if args.json:
        _print_json(
            {"changed": changed, "skipped": skipped, "holdings": data.get("holdings", [])}
        )
        return
    print("✅ " + "; ".join(changed))
    for s in skipped:
        print(f"⚠ {s}")


def portfolio_remove(args) -> None:
    """删除持仓."""
    if not args.symbols:
        _finish_with_error("请指定要删除的代码, 如: portfolio remove AAPL")
    with edit_portfolio(args.portfolio) as data:
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
    if args.json:
        _print_json({"removed": removed})
        return
    if removed:
        print(f"✅ 已删除: {', '.join(removed)}")
    else:
        print("未找到可删除的持仓")


def portfolio_list(args) -> None:
    """列出持仓配置."""
    data = load_portfolio(args.portfolio)
    holdings = data.get("holdings", [])
    if args.json:
        _print_json(
            {"base_currency": data.get("base_currency", "CNY"), "holdings": holdings}
        )
        return
    base = data.get("base_currency", "CNY")
    print(f"\n=== 持仓 ({base}) ===")
    if not holdings:
        print("(空)")
        return
    rows = []
    for h in holdings:
        rows.append(
            {
                "symbol": _sym(h),
                "quantity": h.get("quantity"),
                "avg_cost": h.get("avg_cost"),
            }
        )
    with pd.option_context(
        "display.float_format", "{:,.2f}".format, "display.width", 120
    ):
        print(pd.DataFrame(rows).to_string(index=False))


def portfolio_set_base(args) -> None:
    """设置基础货币."""
    currency = args.currency or (args.symbols[0] if args.symbols else None)
    if not currency:
        _finish_with_error("请指定基础货币, 如: portfolio set-base USD")
    with edit_portfolio(args.portfolio) as data:
        old = data.get("base_currency", "CNY")
        data["base_currency"] = currency.upper()
    if args.json:
        _print_json({"base_currency": data["base_currency"], "previous": old})
        return
    print(f"✅ 基础货币: {old} → {data['base_currency']}")


def cmd_portfolio(args) -> None:
    """portfolio 子命令分发: list / add / remove / set-base."""
    if args.action == "add":
        portfolio_add(args)
    elif args.action == "remove":
        portfolio_remove(args)
    elif args.action == "set-base":
        portfolio_set_base(args)
    else:
        portfolio_list(args)
