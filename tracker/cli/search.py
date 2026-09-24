"""search 子命令: 代码/名称搜索 → 规范代码 (可直接喂 kline/compare/quote)."""
from __future__ import annotations

from .. import search
from ..search import resolve_symbol
from ._common import _finish_with_error, _print_json


def cmd_search(args) -> None:
    """搜索代码/名称, 输出规范代码 + 名称 + 市场 (代码可直接用于取数命令)."""
    q = (args.query or "").strip()
    if not q:
        _finish_with_error("请输入搜索关键词 (代码片段/中文名/英文名), 如 700 / 腾讯 / maotai")
    if args.exact:
        hits = resolve_symbol(q)
    else:
        grouped = search.search_grouped(q, limit_per_domain=args.limit)
        hits = []
        for t in ("cn", "global", "crypto"):
            hits.extend(grouped.get(t, []))
        hits = hits[: args.limit]
    if args.json:
        _print_json({"query": q, "results": hits})
        return
    if not hits:
        print(f"无匹配结果: {q}")
        return
    print(f"\n=== 搜索 {q} ({len(hits)} 条, code 可直接用于 kline/compare/quote) ===")
    for h in hits:
        print(f"  {h['code']:<14} {h['name']:<20} [{h['market']}]")
    print("\n示例: tracker kline " + hits[0]["code"])
