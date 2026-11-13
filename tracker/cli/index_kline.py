"""index-kline 子命令: 宏观/风险指数K线 (IX.<KEY>), 与股票 kline 完全独立."""

from __future__ import annotations

from .. import prices
from ..paths import var_dir
from ..symbols import INDEX_CATALOG, index_groups, index_label, parse
from ._common import _finish_with_error, _print_json
from ._kline_common import output_kline

# 指数K线 HTML 默认输出目录 (数据目录 var/, 安装位置无关)
VAR_DIR = var_dir()


def cmd_index_kline(args) -> None:
    """生成指数K线蜡烛图 HTML + 终端摘要, 支持 --json."""
    if not args.symbol:
        _finish_with_error(
            "请指定指数代码 (IX.<KEY>), 如 index-kline IX.DXY; --list 查看目录"
        )
    try:
        p = parse(args.symbol)
    except ValueError as e:
        _finish_with_error(str(e))
    if p.type != "index":
        _finish_with_error(f"{args.symbol}: 不是指数代码 (规范 IX.<KEY>, 见 --list)")
    try:
        df = prices.get_index_history(p.yahoo, months=args.months, refresh=args.refresh)
    except Exception as e:
        _finish_with_error(f"{p.yahoo}: 指数K线数据获取失败 ({e})")
    label = index_label(p.yahoo[3:])
    output_kline(
        args,
        p,
        df,
        heading=f"指数K线 {label} ({p.yahoo})",
        title=f"{label} ({p.yahoo})",
        currency=None,
        show_volume=args.volume,
        summary_volume=False,
        default_dir=VAR_DIR,
        output_prefix="index_kline",
        noun="指数K线图",
        warn_prefix=f"{p.yahoo} ({label})",
        extra_json={"name": label},
    )


def print_index_catalog(as_json: bool = False) -> None:
    """打印指数目录 (IX.<KEY> 全表, 按分组)."""
    groups = index_groups()
    if as_json:
        _print_json(
            {
                "total": len(INDEX_CATALOG),
                "indices": [
                    {
                        "code": f"IX.{key}",
                        "key": key,
                        "name": index_label(key),
                        "group": group,
                        **INDEX_CATALOG[key],
                    }
                    for group, keys in groups.items()
                    for key in keys
                ],
            }
        )
        return
    print(f"已收录指数 ({len(INDEX_CATALOG)} 个, 代码规范 IX.<KEY>):")
    for group, keys in groups.items():
        print(f"\n[{group}]")
        for key in keys:
            print(f"  IX.{key:<12} {INDEX_CATALOG[key]['name']}")
