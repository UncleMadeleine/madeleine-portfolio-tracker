"""fx 子命令: 汇率查询."""
from __future__ import annotations

from ..fx import get_fx_rates
from ._common import _print_json


def cmd_fx(args) -> None:
    """查询汇率 (以 1 外币兑基础货币计), 支持 --json."""
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
