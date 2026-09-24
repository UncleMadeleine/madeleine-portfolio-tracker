"""compare 子命令: 多股走势对比 (归一化折线, --json 输出序列 / HTML 输出图表)."""
from __future__ import annotations

import sys
from pathlib import Path
from .. import prices
from ..search import resolve_symbol
from ._common import _finish_with_error, _print_json

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def resolve_inputs(symbols: list[str]) -> tuple[list[str], list[str]]:
    """宽松输入 → 规范代码列表 (合法代码归一; 其余走搜索精确回退).

    返回 (codes, warnings); 无法解析的输入进 warnings, 不中断其余代码。
    """
    codes: list[str] = []
    warnings: list[str] = []
    for raw in symbols:
        s = (raw or "").strip()
        if not s:
            continue
        hits = resolve_symbol(s)
        if not hits:
            warnings.append(f"{s}: 无法识别, 已跳过")
            continue
        code = hits[0]["code"]
        # 搜索无果时 resolve_symbol 会回退 parse 放行结果 (中文名/裸数字原样
        # 当美股) —— 该 code 取数必失败。判定: 归一化后仍与输入完全一致
        # (规范代码归一必变形, 如 600519.SH→600519.SS / BTCUSD→BTC-USD;
        #  唯一例外是本身已规范的输入, 但那时代码形态必合法 —— 用 parse 校验)
        if code == s and hits[0]["name"] == s:
            from ..symbols import parse as _parse

            try:
                _parse(code)
            except ValueError:
                warnings.append(f"{s}: 无法识别, 已跳过")
                continue
        if code not in codes:
            codes.append(code)
        if hits[0]["name"] != s and hits[0]["code"] != s.upper():
            warnings.append(f"{s} → {code} ({hits[0]['name']})")
    return codes, warnings


def cmd_compare(args) -> None:
    """多代码 K线收盘价对比: --json 出归一化序列, 默认出交互式 HTML."""
    import webbrowser

    from .. import charting

    if len(args.symbols) < 2:
        _finish_with_error("至少需要 2 个代码, 如: tracker compare AAPL 0700.HK")
    codes, warnings = resolve_inputs(args.symbols)
    for w in warnings:
        print(f"⚠ {w}", file=sys.stderr)  # 诊断信息走 stderr, 保证 --json 的 stdout 纯净
    if len(codes) < 2:
        _finish_with_error("有效代码不足 2 个, 无法对比")
    frames: dict[str, object] = {}
    for code in codes:
        try:
            df = prices.get_ohlc(code, months=args.months, prefer_akshare=args.akshare)
            if df is None or df.empty:
                print(f"⚠ {code}: 无有效K线数据, 已跳过", file=sys.stderr)
            else:
                frames[code] = df
        except Exception as e:
            print(f"⚠ {code}: {e}, 已跳过", file=sys.stderr)
    if len(frames) < 2:
        _finish_with_error("取到数据的代码不足 2 个, 无法对比")

    period = args.period
    if args.json:
        payload = charting.compare_payload(frames, normalize=True, period=period)
        lines = []
        for ln in payload["lines"]:
            vals = [p["value"] for p in ln["data"]]
            first, last = vals[0], vals[-1]
            lines.append(
                {
                    "code": ln["name"],
                    "points": len(vals),
                    "first": first,
                    "last": last,
                    "change_pct": round((last / first - 1) * 100, 4) if first else None,
                    "series": ln["data"],
                }
            )
        _print_json(
            {
                "period": period,
                "months": args.months,
                "normalized": True,
                "codes": list(frames.keys()),
                "lines": lines,
            }
        )
        return

    # HTML: plotly 折线图; 默认归一化 (起点=100) 便于跨币种/量级, --raw 原币种收盘价
    import plotly.graph_objects as go

    fig = go.Figure()
    palette = charting.COMPARE_PALETTE
    for i, (code, df) in enumerate(frames.items()):
        d = charting.resample_ohlc(df, period) if period != "daily" else df
        y = d["close"].astype(float)
        if not args.raw:
            y = y / y.iloc[0] * 100
        fig.add_trace(
            go.Scatter(
                x=d["date"], y=y, mode="lines", name=code,
                line=dict(color=palette[i % len(palette)], width=1.8, shape="spline"),
                hovertemplate="%{y:.2f}<extra>" + code + " %{x|%Y-%m-%d}</extra>",
            )
        )
    fig.update_layout(
        title=f"走势对比 · 近 {args.months} 个月 · {' vs '.join(codes)}"
        + ("" if args.raw else " (起点=100)"),
        template="plotly_white",
        yaxis_title="收盘价 (各代码原币种)" if args.raw else "归一化 (起点=100)",
        hovermode="x unified", height=560,
    )
    out = Path(args.output) if args.output else DATA_DIR / f"compare_{'-'.join(c[:6] for c in codes)}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(charting.fig_to_html(fig), encoding="utf-8")
    # 终端摘要: 区间涨跌
    chg_parts = []
    for code, df in frames.items():
        d = charting.resample_ohlc(df, period) if period != "daily" else df
        close = d["close"].astype(float)
        pct = (close.iloc[-1] / close.iloc[0] - 1) * 100
        chg_parts.append(f"{code} {pct:+.2f}%")
    print(f"\n=== 走势对比 (近 {args.months} 个月, {period}) ===")
    print("区间涨跌: " + " · ".join(chg_parts))
    print(f"\n✅ 对比图已生成: {out}")
    if args.open_browser:
        webbrowser.open(out.resolve().as_uri())
        print("已在浏览器中打开。")
