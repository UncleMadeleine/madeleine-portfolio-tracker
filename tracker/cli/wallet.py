"""import-wallet 子命令: 通过链上公钥/地址查询余额并导入投资组合."""
from __future__ import annotations

import argparse

from ..snapshot import load_portfolio, save_portfolio
from ..symbols import parse
from .. import wallet as wallet_mod
from ._common import _finish_with_error, _print_json, _sym


def _print_holdings_table(holdings: list[dict]) -> None:
    """打印代币余额表格."""
    if not holdings:
        print("(未发现非零余额)")
        return
    print(f"\n{'代币':<16} {'余额':>20} {'来源':<10} {'合约地址'}")
    print("-" * 80)
    for h in holdings:
        sym = h.get("symbol", "?")
        amt = h.get("quantity", 0)
        src = h.get("source", "?")
        ctr = h.get("contract") or "—"
        ctr_short = (ctr[:10] + "..." + ctr[-6:]) if len(ctr) > 16 else ctr
        print(f"{sym:<16} {amt:>20.8f} {src:<10} {ctr_short}")


def cmd_import_wallet(args: argparse.Namespace) -> None:
    """查询链上地址余额, 可追加到 portfolio.json."""
    if not args.chain:
        _finish_with_error("请指定链名称, 如: import-wallet eth 0xd8dA...")
    if not args.address:
        _finish_with_error("请指定链上地址/公钥, 如: import-wallet eth 0xd8dA...")

    chain = args.chain.lower()
    supported = ", ".join(sorted(wallet_mod._SUPPORTED_CHAINS))
    if chain not in wallet_mod._SUPPORTED_CHAINS:
        _finish_with_error(f"不支持的链 '{args.chain}'. 支持: {supported}")

    try:
        result = wallet_mod.import_wallet(
            chain,
            args.address,
            tokenlist=args.tokenlist,
            base_currency=args.base_currency,
        )
    except ValueError as e:
        _finish_with_error(str(e))
    except RuntimeError as e:
        _finish_with_error(f"RPC 查询失败: {e}")

    holdings = result.get("holdings", [])
    errors = result.get("errors", [])

    if args.json:
        _print_json(result)
    else:
        print(f"\n链: {wallet_mod._CHAIN_CONFIG[chain]['label']} ({chain})")
        print(f"地址: {result['address']}")
        print(f"计价: {args.base_currency.upper()}")
        _print_holdings_table(holdings)
        if errors:
            print("\n⚠ 警告:")
            for e in errors:
                print(f"  - {e}")

    if args.add:
        if not holdings:
            print("\n⏭ 未发现非零余额, 跳过写入 portfolio.json")
            return
        data = load_portfolio(args.portfolio)
        existing_syms: set[str] = {_sym(h) for h in data.get("holdings", [])}
        rows = data.setdefault("holdings", [])
        added: list[str] = []
        for h in holdings:
            sym = h["symbol"]
            try:
                p = parse(sym)
                yahoo = p.yahoo
            except ValueError:
                yahoo = sym.upper()
            if yahoo in existing_syms:
                continue
            row: dict = {"symbol": yahoo, "quantity": h["quantity"], "type": "crypto"}
            rows.append(row)
            added.append(yahoo)
            existing_syms.add(yahoo)
        save_portfolio(data, args.portfolio)
        if args.json:
            _print_json({"added": added, "portfolio_path": args.portfolio})
        else:
            print(f"\n✅ 已写入 {len(added)} 个代币 → {args.portfolio}")
            if added:
                print(f"   新增: {', '.join(added)}")
            print("   (avg_cost 未设置, 请在组合页补填)")
