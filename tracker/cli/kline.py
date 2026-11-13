"""kline 子命令: K线蜡烛图 (交互式 HTML + 摘要, 含成交量/均线/周月K)."""

from __future__ import annotations

import sys

from .. import prices
from ..paths import var_dir
from ..symbols import parse
from ._common import _finish_with_error
from ._kline_common import output_kline

# K线 HTML 默认输出目录 (数据目录 var/, 安装位置无关)
VAR_DIR = var_dir()


def _resolve_or_fail(symbol: str):
    """宽松输入 → ParsedSymbol; 解析失败且搜索无果时报错 (附搜索建议).

    parse 是纯形态判定, 会把中文名/裸数字也当美股放行 (腾讯→US, 700→US),
    这类 code 取数必失败 —— 判定为"parse 放行但非规范形态"时仍走在线搜索:
    仅当搜索无结果才沿用 parse 结果。
    """
    from ..search import resolve_symbol

    try:
        p = parse(symbol)
    except ValueError:
        p = None
    # 规范形态: 带已知后缀, 或 (美股) ASCII 字母开头的 ticker
    strict = p is not None and (
        "." in p.yahoo or "-" in p.yahoo or (p.yahoo.isascii() and p.yahoo.isalpha())
    )
    if strict:
        return p
    hits = resolve_symbol(symbol, limit=1)
    if hits:
        return parse(hits[0]["code"])
    if p is not None:
        return p  # parse 放行 + 搜索无果: 保留原判定, 让取数层报真实错误
    _finish_with_error(
        f"无法识别的代码: {symbol} (先运行 tracker search {symbol} 查规范代码)"
    )


def cmd_kline(args) -> None:
    """生成 K线蜡烛图 HTML + 终端摘要, 支持 --json; 宽松输入自动搜索回退."""
    p = _resolve_or_fail(args.symbol)
    if p.yahoo != args.symbol.strip().upper():
        # 归一化提示走 stderr: --json 的 stdout 必须纯净 (周期降级提示与
        # index_kline/compare 同口径), 否则 json.loads(stdout) 直接失败
        print(
            f"ℹ {args.symbol} → {p.yahoo} ({p.market_label})", file=sys.stderr
        )
    try:
        df = prices.get_ohlc(
            p.yahoo,
            months=args.months,
            prefer_akshare=args.akshare,
            refresh=args.refresh,
            use_ibkr=args.ibkr,
            use_longport=getattr(args, "longport", False),
            crypto_source=getattr(args, "crypto_source", "auto"),
        )
    except Exception as e:
        _finish_with_error(f"{p.yahoo}: K线数据获取失败 ({e})")
    output_kline(
        args,
        p,
        df,
        heading=f"K线 {p.yahoo}",
        title=p.yahoo,
        currency=p.currency,
        show_volume=not args.no_volume,
        summary_volume=True,
        default_dir=VAR_DIR,
        output_prefix="kline",
        noun="K线图",
        warn_prefix=p.yahoo,
    )
