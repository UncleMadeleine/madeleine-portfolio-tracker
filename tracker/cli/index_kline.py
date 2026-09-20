"""index-kline 子命令: 宏观/风险指数K线 (IX.<KEY>), 与股票 kline 完全独立."""
from __future__ import annotations

from pathlib import Path

from .. import prices
from ..symbols import INDEX_CATALOG, index_label, parse
from ._common import _finish_with_error, _print_json

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def cmd_index_kline(args) -> None:
    """生成指数K线蜡烛图 HTML + 终端摘要, 支持 --json."""
    import webbrowser

    from .. import charting

    try:
        p = parse(args.symbol)
    except ValueError as e:
        _finish_with_error(str(e))
    if p.type != "index":
        _finish_with_error(f"{args.symbol}: 不是指数代码 (规范 IX.<KEY>, 见 --list)")
    try:
        df = prices.get_index_history(
            p.yahoo, months=args.months, refresh=args.refresh
        )
    except Exception as e:
        _finish_with_error(f"{p.yahoo}: 指数K线数据获取失败 ({e})")
    if args.period != "daily":
        df = charting.resample_ohlc(df, args.period)
    if df.empty:
        _finish_with_error(f"{p.yahoo}: 无有效K线数据")

    mas = charting.parse_ma_periods(args.ma)
    s = charting.summarize_ohlc(df, mas)
    label = index_label(p.yahoo[3:])
    if args.json:
        recs = df.copy()
        recs["date"] = recs["date"].dt.strftime("%Y-%m-%d")
        _print_json(
            {
                "symbol": p.yahoo,
                "name": label,
                "currency": p.currency,
                "period": args.period,
                "months": args.months,
                "bars": len(df),
                "summary": s,
                "data": recs.round(6).to_dict(orient="records"),
            }
        )
        return

    period_label = charting.PERIOD_LABELS.get(args.period, args.period)
    print(
        f"\n=== 指数K线 {label} ({p.yahoo}) · {period_label} · 近 {args.months} 个月"
        f" ({s['bars']} 根) ==="
    )
    chg = f" ({s['change_pct']:+.2f}%)" if s["change_pct"] is not None else ""
    print(
        f"区间: {s['first_date']} → {s['last_date']}"
        f"  · 收 {s['first_close']:,.3f} → {s['last_close']:,.3f}{chg}"
    )
    print(
        f"最新 {s['last_date']}: 开 {s['last_open']:,.3f}  高 {s['last_high']:,.3f}"
        f"  低 {s['last_low']:,.3f}  收 {s['last_close']:,.3f}"
    )
    if s["ma"]:
        parts = [
            f"MA{n[2:]} {v:,.3f}" if v is not None else f"MA{n[2:]} —"
            for n, v in s["ma"].items()
        ]
        print("均线: " + "  ".join(parts))
    print(
        f"区间最高 {s['period_high']:,.3f} ({s['period_high_date']})"
        f"  · 最低 {s['period_low']:,.3f} ({s['period_low_date']})"
    )

    fig = charting.build_candlestick_fig(
        df, f"{label} ({p.yahoo})", currency=None, mas=mas,
        show_volume=args.volume, period=args.period,
    )
    out = Path(args.output) if args.output else DATA_DIR / f"index_kline_{p.yahoo.replace('.', '_')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(charting.fig_to_html(fig), encoding="utf-8")
    print(f"\n✅ 指数K线图已生成: {out}")
    if args.open_browser:
        webbrowser.open(out.resolve().as_uri())
        print("已在浏览器中打开。")


def print_index_catalog() -> None:
    """打印指数目录 (IX.<KEY> 全表)."""
    print("已收录指数 (代码规范 IX.<KEY>):")
    for key, e in INDEX_CATALOG.items():
        print(f"  IX.{key:<12} {e['name']}")
