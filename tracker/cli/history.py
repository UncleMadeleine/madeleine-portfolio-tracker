"""history 子命令: 历史价格 (近 N 个月)."""

from __future__ import annotations

import math

import pandas as pd

from .. import prices
from ..symbols import parse
from ._common import _finish_with_error, _print_json, _records


def cmd_history(args) -> None:
    """查询历史价格 (近 N 个月), 支持 --json."""
    try:
        parse(args.symbol)
    except ValueError as e:
        _finish_with_error(str(e))
    try:
        df = prices.get_history(
            args.symbol,
            months=args.months,
            prefer_akshare=args.akshare,
            use_ibkr=args.ibkr,
            use_longport=getattr(args, "longport", False),
            crypto_source=getattr(args, "crypto_source", "auto"),
        )
    except Exception as e:
        _finish_with_error(f"{args.symbol}: 历史数据获取失败 ({e})")
    if args.json:
        recs = df.copy()
        recs["date"] = recs["date"].astype(str)
        # _records 把 NaN/NaT 转 None: json.dumps 默认会输出裸 NaN, 不是合法 JSON
        _print_json(_records(recs))
        return
    # 摘要/区间高低与 kline/compare 共用同一道清洗: 数据源偶尔返回
    # high<close / NaN / inf 的脏行, 不清理会让「区间高」低于收盘价。
    # JSON 分支保持逐行原样 (缺失值转 null), 供下游自行决定是否过滤。
    from ..charting import clean_ohlc

    df = clean_ohlc(df)
    if df.empty:
        print(f"⚠ {args.symbol}: 无历史数据")
        return
    close = pd.to_numeric(df["close"], errors="coerce")
    # 只校验首根不够: provider 偶尔返回末根 close=NaN/inf, 会打印
    # "收 10.000 → nan (+nan%)"。过滤掉无效收盘后再取首末有效值。
    valid_close = close[close.notna() & close.map(math.isfinite) & (close > 0)]
    if valid_close.empty:
        print(f"⚠ {args.symbol}: 无有效收盘价")
        return
    first = float(valid_close.iloc[0])
    last = float(valid_close.iloc[-1])
    change = (
        (last / first - 1) * 100
        if len(valid_close) >= 2 and first > 0
        else None
    )
    # 区间高/低取当日 high/low 的极值, 不能用收盘价: provider 返回的是完整
    # OHLC, 盘中冲高/探底后收回的交易日会漏掉 (输出与真实区间高低不符)。
    high = (
        float(pd.to_numeric(df["high"], errors="coerce").max())
        if "high" in df.columns
        else float(close.max())
    )
    low = (
        float(pd.to_numeric(df["low"], errors="coerce").min())
        if "low" in df.columns
        else float(close.min())
    )
    print(
        f"\n=== 历史行情 {args.symbol} (近 {args.months} 个月, 共 {len(df)} 个交易日) ==="
    )
    print(
        f"区间: {df['date'].iloc[0]:%Y-%m-%d} → {df['date'].iloc[-1]:%Y-%m-%d}"
        f"  · 收 {first:,.3f} → {last:,.3f}"
        + (f" ({change:+.2f}%)" if change is not None else "")
        + f"  · 区间高 {high:,.3f} / 低 {low:,.3f}"
    )
    tail = df.tail(args.rows)
    tail = tail.copy()
    tail["date"] = tail["date"].astype(str)
    with pd.option_context(
        "display.float_format", "{:,.3f}".format, "display.width", 160
    ):
        print("\n最近交易日:")
        print(tail.to_string(index=False))
