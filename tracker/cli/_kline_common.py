"""kline / index-kline 子命令共用的终端摘要、JSON 与 HTML 输出."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from .. import charting
from ._common import _finish_with_error, _print_json, _records, atomic_write_text


def _fmt_vol(v) -> str:
    """成交量格式化 (亿/万单位)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    for unit, div in (("亿", 1e8), ("万", 1e4)):
        if v >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:,.0f}"


def output_kline(
    args,
    p,
    df: pd.DataFrame,
    *,
    heading: str,
    title: str,
    currency: str | None,
    show_volume: bool,
    summary_volume: bool,
    output_prefix: str,
    noun: str,
    warn_prefix: str,
    extra_json: dict | None = None,
    default_dir,
) -> None:
    """统一处理周期降级、摘要、JSON、HTML 写出与浏览器打开.

    调用方只负责解析代码并取到 df; 两个 CLI 的输出格式与错误口径保持一致。
    """
    period = charting.resolve_period(df, args.period)
    if period != args.period:
        print(
            f"ℹ {warn_prefix}: 数据源为{charting.PERIOD_LABELS[period]}, "
            f"{charting.PERIOD_LABELS[args.period]}不可用 — 已按"
            f"{charting.PERIOD_LABELS[period]}展示",
            file=sys.stderr,
        )
    if period != "daily":
        df = charting.resample_ohlc(df, period)
    if df.empty:
        _finish_with_error(f"{p.yahoo}: 无有效K线数据")

    mas = charting.parse_ma_periods(args.ma)
    summary = charting.summarize_ohlc(df, mas)
    if args.json:
        recs = df.copy()
        recs["date"] = recs["date"].dt.strftime("%Y-%m-%d")
        payload = {
            "symbol": p.yahoo,
            **(extra_json or {}),
            "currency": p.currency,
            "period": period,
            "months": args.months,
            "bars": len(df),
            "summary": summary,
            # _records 把 NaN/NaT 转 None: json.dumps 默认会输出裸 NaN,
            # 不是合法 JSON (history 子命令同款处理)
            "data": _records(recs.round(6)),
        }
        _print_json(payload)
        return

    period_label = charting.PERIOD_LABELS.get(period, period)
    # 精度随价格量级 (summarize_ohlc 已按收盘价中位数计算): 固定 3 位会把
    # SHIB/PEPE 这类 ~1e-5 的价格显示成 0.000。
    prec = int(summary.get("precision", 3))
    print(f"\n=== {heading} · {period_label} · 近 {args.months} 个月"
          f" ({summary['bars']} 根) ===")
    chg = (
        f" ({summary['change_pct']:+.2f}%)"
        if summary["change_pct"] is not None
        else ""
    )
    print(
        f"区间: {summary['first_date']} → {summary['last_date']}"
        f"  · 收 {summary['first_close']:,.{prec}f}"
        f" → {summary['last_close']:,.{prec}f}{chg}"
    )
    vol = f"  量 {_fmt_vol(summary['last_volume'])}" if summary_volume else ""
    print(
        f"最新 {summary['last_date']}: 开 {summary['last_open']:,.{prec}f}"
        f"  高 {summary['last_high']:,.{prec}f}"
        f"  低 {summary['last_low']:,.{prec}f}"
        f"  收 {summary['last_close']:,.{prec}f}{vol}"
    )
    if summary["ma"]:
        parts = [
            f"MA{n[2:]} {v:,.{prec}f}" if v is not None else f"MA{n[2:]} —"
            for n, v in summary["ma"].items()
        ]
        print("均线: " + "  ".join(parts))
    print(
        f"区间最高 {summary['period_high']:,.{prec}f}"
        f" ({summary['period_high_date']})"
        f"  · 最低 {summary['period_low']:,.{prec}f}"
        f" ({summary['period_low_date']})"
    )

    fig = charting.build_candlestick_fig(
        df,
        title,
        currency=currency,
        mas=mas,
        show_volume=show_volume,
        period=period,
    )
    out = (
        Path(args.output)
        if args.output
        else default_dir / f"{output_prefix}_{p.yahoo.replace('.', '_')}.html"
    )
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.is_dir():
            _finish_with_error(f"输出路径是一个目录, 请指定文件: {out}")
        atomic_write_text(out, charting.fig_to_html(fig))
    except OSError as e:
        _finish_with_error(f"{noun}写入失败: {out} ({e})")
    print(f"\n✅ {noun}已生成: {out}")
    if args.open_browser:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
        print("已在浏览器中打开。")
