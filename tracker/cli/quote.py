"""quote 子命令: 查询单个或多个代码实时行情."""

from __future__ import annotations

from .. import prices
from ..symbols import parse
from ._common import _print_json


def cmd_quote(args) -> None:
    """查询实时行情, 支持 --json 机器可读输出."""
    quotes, errors, notes = prices.get_quotes(
        args.symbols,
        prefer_akshare=args.akshare,
        use_ibkr=args.ibkr,
        use_longport=getattr(args, "longport", False),
        crypto_source=getattr(args, "crypto_source", "auto"),
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
                    "source": getattr(q, "source", None),
                }
            )
    if args.json:
        _print_json({"quotes": rows, "notes": notes})
        return
    print("\n=== 实时行情 ===")
    for r in rows:
        if "error" in r:
            print(f"  {r['symbol']}: ⚠ {r['error']}")
            continue
        chg = f" ({r['change_pct']:+.2f}%)" if r["change_pct"] is not None else ""
        src = f" [{r['source']}]" if r.get("source") else ""
        print(
            f"  {r['symbol']:>12}  {r['price']:,.3f} {r['currency']}"
            f"{chg}{src}  {r['name'] or ''}"
        )
    for n in notes:
        print(f"  ℹ {n}")
