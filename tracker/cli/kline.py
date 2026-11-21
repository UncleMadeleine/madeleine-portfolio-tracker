"""kline 子命令: K线蜡烛图 (交互式 HTML + 摘要, 含成交量/均线/周月K)."""

from __future__ import annotations

import sys

import pandas as pd

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


def _fetch_ohlc(args, symbol: str):
    """按 kline 子命令的统一参数取单只K线 (比值模式复用给分母)."""
    return prices.get_ohlc(
        symbol,
        months=args.months,
        prefer_akshare=args.akshare,
        refresh=args.refresh,
        use_ibkr=args.ibkr,
        use_longport=getattr(args, "longport", False),
        crypto_source=getattr(args, "crypto_source", "auto"),
    )


def _naive_min_date(df: pd.DataFrame) -> pd.Timestamp:
    """DataFrame 日期列最小值 (剥掉时区; 下游 get_fx_history 只接受日期)."""
    s = pd.to_datetime(df["date"], errors="coerce")
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_localize(None)
    return s.min()


def _cmd_ratio_kline(args, p, df) -> None:
    """--ratio: 两标的比值画成蜡烛图 (分子=当前代码, 分母=--ratio)."""
    from .. import charting, fx as fx_mod

    den_p = _resolve_or_fail(args.ratio)
    if den_p.yahoo == p.yahoo:
        _finish_with_error("比值K线的分子与分母不能是同一代码")
    if den_p.yahoo != args.ratio.strip().upper():
        print(
            f"ℹ {args.ratio} → {den_p.yahoo} ({den_p.market_label})",
            file=sys.stderr,
        )
    try:
        den_df = _fetch_ohlc(args, den_p.yahoo)
    except Exception as e:
        _finish_with_error(f"{den_p.yahoo}: K线数据获取失败 ({e})")

    # 两标的原生粒度可能不同 (如一方只有周K): 取较粗的一档, 避免把粗粒度
    # 数据伪装成日K (与单只 K 线的 resolve_period 降级口径一致)。
    period = max(
        charting.resolve_period(df, args.period),
        charting.resolve_period(den_df, args.period),
        key=charting.PERIOD_ORDER.index,
    )
    if period != args.period:
        print(
            f"ℹ {p.yahoo} / {den_p.yahoo}: 数据源为"
            f"{charting.PERIOD_LABELS[period]}, "
            f"{charting.PERIOD_LABELS[args.period]}不可用 — 已按"
            f"{charting.PERIOD_LABELS[period]}展示",
            file=sys.stderr,
        )

    fx_df = None
    fx_pair = None
    fx_missing = False
    fx_adjusted = False
    if args.ratio_fx and p.currency and den_p.currency:
        if p.currency != den_p.currency:
            start_ts = min(_naive_min_date(df), _naive_min_date(den_df)) - (
                pd.Timedelta(days=10)
            )
            fx_pair = f"{den_p.currency}{p.currency}"
            fx_kwargs = {"refresh": True} if args.refresh else {}
            fx_df = fx_mod.get_fx_history(
                den_p.currency,
                p.currency,
                start_ts.strftime("%Y-%m-%d"),
                pd.Timestamp.today().strftime("%Y-%m-%d"),
                **fx_kwargs,
            )
            fx_adjusted = fx_df is not None
            fx_missing = fx_df is None
            if fx_missing:
                print(
                    f"⚠ {fx_pair}: 汇率历史取数失败, 比值未做汇率调整",
                    file=sys.stderr,
                )
    try:
        ratio_df = charting.build_ratio_ohlc(
            {p.yahoo: df, den_p.yahoo: den_df},
            p.yahoo,
            den_p.yahoo,
            period=period,
            fx_df=fx_df,
        )
    except ValueError as e:
        _finish_with_error(f"比值K线: {e}")

    label = f"{p.yahoo} / {den_p.yahoo}"
    output_kline(
        args,
        p,
        ratio_df,
        heading=f"K线 {label} (比值)",
        title=label,
        currency=None,
        show_volume=False,
        summary_volume=False,
        default_dir=VAR_DIR,
        output_prefix="ratio_kline",
        noun="比值K线图",
        warn_prefix=label,
        resolved_period=period,
        file_stem=f"{p.yahoo}_{den_p.yahoo}".replace(".", "_"),
        json_round=10,
        extra_json={
            "ratio": {
                "numerator": p.yahoo,
                "denominator": den_p.yahoo,
                "fxAdjusted": fx_adjusted,
                "fxPair": fx_pair,
                "fxMissing": fx_missing,
            }
        },
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
        df = _fetch_ohlc(args, p.yahoo)
    except Exception as e:
        _finish_with_error(f"{p.yahoo}: K线数据获取失败 ({e})")
    if getattr(args, "ratio", None):
        _cmd_ratio_kline(args, p, df)
        return
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
