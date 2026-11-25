"""cache 子命令: 行情磁盘缓存管理 (info / clear)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ._common import _print_json, _finish_with_error


def cmd_cache(args) -> None:
    """行情缓存管理: info 查看统计 / clear 清空 / prune 清理孤儿缓存."""
    from .. import cache as cache_mod

    if args.action == "clear":
        try:
            n = cache_mod.clear()
        except (RuntimeError, sqlite3.Error) as e:
            _finish_with_error(f"清空缓存失败: {e}")
        if args.json:
            _print_json({"cleared": n})
        else:
            print(f"✅ 已清空行情缓存 ({n} 条)")
        return
    if args.action == "prune":
        try:
            result = cache_mod.prune_orphans(
                dry_run=bool(getattr(args, "dry_run", False))
            )
        except (RuntimeError, sqlite3.Error) as e:
            _finish_with_error(f"清理孤儿缓存失败: {e}")
        if args.json:
            _print_json(result)
            return
        if result.get("skipped"):
            print(f"⏭ 跳过: {result.get('reason')}")
            return
        print("\n=== 孤儿缓存 ===")
        print(f"  扫描范围: {result['scope_size']} 个持仓/自选代码")
        labels = {
            "quotes": "实时行情",
            "ohlc_cache": "K线",
            "json_cache": "估值/财报快照",
            "fundamentals": "基本面",
            "series_cache": "市值序列",
            "calendar_cache": "日历单票",
        }
        for name, item in result["tables"].items():
            if not item["orphans"]:
                continue
            sample = ", ".join(item["sample"][:5])
            extra = f" (保留指数 {item['kept_index']} 组)" if "kept_index" in item else ""
            print(
                f"  {labels.get(name, name)}: {item['orphans']} 条{extra}"
                + (f" · {sample}" if sample else "")
            )
        verb = "将删除" if result.get("dry_run") else "已删除"
        count = result.get("would_delete") if result.get("dry_run") else result["deleted"]
        print(f"  {verb} {count} 条孤儿缓存")
        return
    try:
        info = cache_mod.info()
    except (RuntimeError, sqlite3.Error) as e:
        _finish_with_error(f"读取缓存统计失败: {e}")
    if args.json:
        _print_json(info)
        return
    print("\n=== 行情缓存 ===")
    print(f"  数据库: {info['db']}")
    print(
        f"  TTL: {info['ttl_seconds']}s "
        f"(陈旧 {info['stale_ttl_seconds']}s 内首屏可先渲染后刷新)"
    )
    print(
        f"  总条数: {info['total']} (新鲜 {info['fresh']} / 陈旧 {info['stale']}, "
        f"其中可渲染 {info['usable'] - info['fresh']})"
    )
    print(
        f"  汇率缓存: {info['fx_total']} 组 (新鲜 {info['fx_fresh']}"
        f" / TTL {info['fx_ttl_seconds']}s, 陈旧可渲染 {info['fx_stale_ttl_seconds']}s)"
    )
    print(
        f"  K线缓存: {info['ohlc_total']} 组 (有效 {info['ohlc_fresh']}"
        f" / TTL {info['ohlc_ttl_seconds']}s)"
    )
    print(
        f"  日历缓存: {info['calendar_total']} 组"
        f" (TTL {info['calendar_ttl_seconds']}s)"
    )
    print(f"  文件大小: {info['size_bytes']:,} bytes")
    if info["newest_at"]:
        print(
            f"  最近更新: {datetime.fromtimestamp(info['newest_at']):%Y-%m-%d %H:%M:%S}"
        )
