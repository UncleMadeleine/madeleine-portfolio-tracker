"""import-wallet 子命令: 通过链上公钥/地址查询余额并导入投资组合.

兼容入口 — 写盘已统一到 tracker.importer (推荐: tracker import wallet).
"""
from __future__ import annotations

import argparse

from .. import importer
from .. import wallet as wallet_mod
from ._common import _finish_with_error, _print_json


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

    if not (args.add or args.overwrite):
        return
    rows = importer.wallet_rows(result)
    if not rows:
        print("\n⏭ 未发现非零余额, 跳过写入 portfolio.json")
        return
    mode = importer.MODE_OVERWRITE if args.overwrite else importer.MODE_APPEND
    res = importer.apply_import(rows, args.portfolio, mode=mode)
    if args.json:
        _print_json({"import": res})
        return
    n = len(res["added"]) + len(res["updated"])
    print(f"\n✅ 已写入 {n} 个代币 → {args.portfolio} "
          f"(新增 {len(res['added'])} · 更新 {len(res['updated'])})")
    if res["added"]:
        print(f"   新增: {', '.join(res['added'])}")
    print("   (avg_cost 未设置, 请在组合页补填)")
