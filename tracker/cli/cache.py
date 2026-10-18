"""cache 子命令: 行情磁盘缓存管理 (info / clear)."""

from __future__ import annotations

from datetime import datetime

from ._common import _print_json


def cmd_cache(args) -> None:
    """行情缓存管理: info 查看统计 / clear 清空."""
    from .. import cache as cache_mod

    if args.action == "clear":
        n = cache_mod.clear()
        if args.json:
            _print_json({"cleared": n})
        else:
            print(f"✅ 已清空行情缓存 ({n} 条)")
        return
    info = cache_mod.info()
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
    print(f"  文件大小: {info['size_bytes']:,} bytes")
    if info["newest_at"]:
        print(
            f"  最近更新: {datetime.fromtimestamp(info['newest_at']):%Y-%m-%d %H:%M:%S}"
        )
