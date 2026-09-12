"""portfolio 子命令: 持仓管理 (list / add / remove / set-base)."""
from __future__ import annotations

import pandas as pd

from ..snapshot import load_portfolio, save_portfolio
from ..symbols import parse
from ._common import _finish_with_error, _print_json, _sym


def portfolio_add(args) -> None:
    """添加/更新持仓 (需指定 --quantity)."""
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
    """删除持仓."""
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
    """列出持仓配置."""
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
    """设置基础货币."""
    currency = args.currency or (args.symbols[0] if args.symbols else None)
    if not currency:
        _finish_with_error("请指定基础货币, 如: portfolio set-base USD")
    data = load_portfolio(args.portfolio)
    old = data.get("base_currency", "CNY")
    data["base_currency"] = currency.upper()
    save_portfolio(data, args.portfolio)
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
