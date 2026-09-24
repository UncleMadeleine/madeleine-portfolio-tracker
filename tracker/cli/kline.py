"""kline 子命令: K线蜡烛图 (交互式 HTML + 摘要, 含成交量/均线/周月K)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .. import prices
from ..symbols import parse
from ._common import _finish_with_error, _print_json

VAR_DIR = Path(__file__).resolve().parent.parent.parent / "var"


def _fmt_vol(v) -> str:
    """成交量格式化 (亿/万单位)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    for unit, div in (("亿", 1e8), ("万", 1e4)):
        if v >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:,.0f}"


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
        "." in p.yahoo
        or "-" in p.yahoo
        or (p.yahoo.isascii() and p.yahoo.isalpha())
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
    import webbrowser

    from .. import charting

    p = _resolve_or_fail(args.symbol)
    if p.yahoo != args.symbol.strip().upper():
        print(f"ℹ {args.symbol} → {p.yahoo} ({p.market_label})")
    try:
        df = prices.get_ohlc(
            p.yahoo, months=args.months,
            prefer_akshare=args.akshare, refresh=args.refresh,
            use_ibkr=args.ibkr,
        )
    except Exception as e:
        _finish_with_error(f"{p.yahoo}: K线数据获取失败 ({e})")
    if args.period != "daily":
        df = charting.resample_ohlc(df, args.period)
    if df.empty:
        _finish_with_error(f"{p.yahoo}: 无有效K线数据")

    mas = charting.parse_ma_periods(args.ma)
    s = charting.summarize_ohlc(df, mas)
    if args.json:
        recs = df.copy()
        recs["date"] = recs["date"].dt.strftime("%Y-%m-%d")
        _print_json(
            {
                "symbol": p.yahoo,
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
        f"\n=== K线 {p.yahoo} · {period_label} · 近 {args.months} 个月"
        f" ({s['bars']} 根) ==="
    )
    chg = f" ({s['change_pct']:+.2f}%)" if s["change_pct"] is not None else ""
    print(
        f"区间: {s['first_date']} → {s['last_date']}"
        f"  · 收 {s['first_close']:,.3f} → {s['last_close']:,.3f}{chg}"
    )
    print(
        f"最新 {s['last_date']}: 开 {s['last_open']:,.3f}  高 {s['last_high']:,.3f}"
        f"  低 {s['last_low']:,.3f}  收 {s['last_close']:,.3f}  量 {_fmt_vol(s['last_volume'])}"
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
        df, p.yahoo, currency=p.currency, mas=mas,
        show_volume=not args.no_volume, period=args.period,
    )
    out = Path(args.output) if args.output else VAR_DIR / f"kline_{p.yahoo.replace('.', '_')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(charting.fig_to_html(fig), encoding="utf-8")
    print(f"\n✅ K线图已生成: {out}")
    if args.open_browser:
        webbrowser.open(out.resolve().as_uri())
        print("已在浏览器中打开。")
