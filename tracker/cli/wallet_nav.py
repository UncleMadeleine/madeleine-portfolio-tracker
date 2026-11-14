"""wallet-nav 子命令: 按链上转账记录回放钱包历史净值曲线.

与 ``nav`` 的区别:
- ``nav`` 是组合层面的每日快照/回填;
- ``wallet-nav`` 针对单个链上地址, 用**历史余额 × 历史币价**生成曲线,
  不需要先把钱包导入 portfolio.json, 也不写 nav_history.db。
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

from .. import wallet_history
from ..storage import PORTFOLIO_PATH as DEFAULT_PORTFOLIO, load_portfolio
from ..wallet_nav import compute_wallet_nav
from ._common import _finish_with_error

_CHAIN_LABELS = {
    "eth": "Ethereum",
    "bsc": "BNB Chain",
    "polygon": "Polygon",
    "arbitrum": "Arbitrum One",
    "avalanche": "Avalanche C-Chain",
    "tron": "TRON",
    "solana": "Solana",
}


def _print_table(result: dict) -> None:
    rows = result.get("rows") or []
    base = result.get("base") or "USD"
    chain = result.get("chain") or ""
    print(
        f"链: {_CHAIN_LABELS.get(chain, chain)} ({chain}) · "
        f"地址: {result.get('address')}"
    )
    print(
        f"区间: {result.get('start')} → {result.get('end')} "
        f"({result.get('days')} 天) · 基础货币: {base}"
    )
    if result.get("providers"):
        print(f"数据源: {', '.join(result['providers'])}")
    if result.get("truncated"):
        print("⚠ 历史记录超过上限, 只回放了最近部分")

    if not rows:
        print("\n未生成净值数据。")
    else:
        df = pd.DataFrame(rows)
        df["日期"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d")
        df["总市值"] = pd.to_numeric(df["total_value"], errors="coerce")
        df["持仓数"] = (
            pd.to_numeric(df["holdings"], errors="coerce").fillna(0).astype(int)
        )
        with pd.option_context(
            "display.float_format",
            "{:,.2f}".format,
            "display.width",
            160,
            "display.max_rows",
            None,
        ):
            print(df[["日期", "总市值", "持仓数"]].to_string(index=False, na_rep="—"))
        first, last = rows[0], rows[-1]
        change = (last["total_value"] or 0) - (first["total_value"] or 0)
        pct = (
            change / abs(first["total_value"])
            if first["total_value"] not in (None, 0)
            else None
        )
        tail = f"\n{first['day']} → {last['day']} · 区间 {change:+,.2f} {base}"
        if pct is not None:
            tail += f" ({pct:+.2%})"
        print(tail)

    assets = result.get("assets") or []
    if assets:
        print("\n资产:")
        for item in assets:
            mark = "✅" if item.get("priced") else "⚠"
            qty = item.get("quantity")
            print(
                f"  {mark} {item.get('symbol'):<10} "
                f"{qty:>20,.8f}  {item.get('price_symbol') or '稳定币/无行情'}"
            )
    issues = result.get("issues") or []
    notes = result.get("notes") or []
    if issues or notes:
        print("\n⚠ 说明:")
        for msg in issues + notes:
            print(f"  - {msg}")


def cmd_wallet_nav(args: argparse.Namespace) -> None:
    chain = str(getattr(args, "chain", "") or "").strip().lower()
    address = str(getattr(args, "address", "") or "").strip()
    if not chain:
        _finish_with_error("请指定链名称, 如: wallet-nav eth 0x...")
    if not address:
        _finish_with_error("请指定链上地址/公钥")

    base = (args.base or "").strip().upper()
    if not base:
        try:
            portfolio = load_portfolio(args.portfolio)
            base = str(portfolio.get("base_currency") or "USD").upper()
        except Exception:  # noqa: BLE001 - 只用于默认币种, 读失败回退 USD
            base = "USD"

    try:
        result = compute_wallet_nav(
            chain,
            address,
            base=base,
            days=args.days,
            tokenlist=args.tokenlist,
            config_path=args.config,
            crypto_source=args.crypto_source,
            refresh=args.refresh,
            max_assets=args.max_assets,
        )
    except (
        wallet_history.WalletHistoryError,
        ValueError,
        RuntimeError,
        FileNotFoundError,
    ) as e:
        _finish_with_error(str(e))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    _print_table(result)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="钱包历史净值曲线 (链上转账回放)")
    ap.add_argument("chain", help="链: eth/bsc/polygon/arbitrum/avalanche/tron/solana")
    ap.add_argument("address", help="链上地址/公钥")
    ap.add_argument("--portfolio", default=str(DEFAULT_PORTFOLIO))
    ap.add_argument("--base", default=None, help="基础货币 (默认取 portfolio.json)")
    ap.add_argument("--days", type=int, default=180, help="回看自然日数 (默认 180)")
    ap.add_argument("--tokenlist", default=None, help="外部 tokenlist JSON (可选)")
    ap.add_argument("--config", default=None, help="wallet_history.json 路径")
    ap.add_argument(
        "--max-assets",
        type=int,
        default=30,
        help="单次估值最多取多少个资产的历史价 (默认 30, 按当前估值优先保留大头)",
    )
    ap.add_argument(
        "--crypto-source",
        default="auto",
        help="加密货币行情源: auto/binance/gate/hyperliquid/yfinance",
    )
    ap.add_argument("--refresh", action="store_true", help="强制刷新历史行情缓存")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)
    try:
        cmd_wallet_nav(args)
    except Exception as e:  # noqa: BLE001 - 直跑模块时给可读错误
        _finish_with_error(str(e))


if __name__ == "__main__":
    main()
