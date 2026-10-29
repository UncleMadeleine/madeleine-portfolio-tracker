"""compare 子命令: 多股走势对比 (归一化折线, --json 输出序列 / HTML 输出图表)."""

from __future__ import annotations

import sys
from pathlib import Path
from .. import prices
from ..paths import var_dir
from ..search import resolve_symbol
from ._common import _finish_with_error, _print_json

# 对比图 HTML 默认输出目录 (数据目录 var/, 安装位置无关)
VAR_DIR = var_dir()


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


def _build_ratio(frames: dict[str, object], ratio_args, period: str, fx_adjust: bool):
    """--ratio: 两个代码 → 比例线 payload (默认原币比值, 可选历史汇率调整)."""
    from .. import charting, fx as fx_mod
    from ..symbols import parse as _parse

    ratio_codes, warnings = resolve_inputs(list(ratio_args))
    for w in warnings:
        print(f"⚠ {w}", file=sys.stderr)
    if len(ratio_codes) != 2:
        _finish_with_error("--ratio 需要两个可识别的代码")
    numerator, denominator = ratio_codes
    missing = [c for c in ratio_codes if c not in frames]
    if missing:
        _finish_with_error(
            "比例线代码需同时出现在对比代码中: " + ", ".join(missing)
        )

    fx_df = None
    fx_pair = None
    fx_missing = False
    if fx_adjust:
        cur_num = _parse(numerator).currency
        cur_den = _parse(denominator).currency
        if cur_num != cur_den:
            import pandas as pd

            fx_pair = f"{cur_den}{cur_num}"
            start_ts = min(
                frames[numerator]["date"].min(), frames[denominator]["date"].min()
            ) - pd.Timedelta(days=10)
            fx_df = fx_mod.get_fx_history(
                cur_den,
                cur_num,
                start_ts.strftime("%Y-%m-%d"),
                pd.Timestamp.today().strftime("%Y-%m-%d"),
            )
            fx_missing = fx_df is None
            if fx_missing:
                print(
                    f"⚠ {fx_pair}: 汇率历史取数失败, 比例线未做汇率调整",
                    file=sys.stderr,
                )
    try:
        return charting.build_ratio_series(
            frames,
            numerator,
            denominator,
            period=period,
            fx_df=fx_df,
            fx_pair=fx_pair,
            fx_missing=fx_missing,
        )
    except ValueError as e:
        _finish_with_error(f"比例线: {e}")


def cmd_compare(args) -> None:
    """多代码 K线收盘价对比: --json 出归一化序列, 默认出交互式 HTML."""
    import webbrowser

    from .. import charting

    if len(args.symbols) < 2:
        _finish_with_error("至少需要 2 个代码, 如: tracker compare AAPL 0700.HK")
    codes, warnings = resolve_inputs(args.symbols)
    for w in warnings:
        print(
            f"⚠ {w}", file=sys.stderr
        )  # 诊断信息走 stderr, 保证 --json 的 stdout 纯净
    if len(codes) < 2:
        _finish_with_error("有效代码不足 2 个, 无法对比")
    frames: dict[str, object] = {}
    for code in codes:
        try:
            df = prices.get_ohlc(
                code,
                months=args.months,
                prefer_akshare=args.akshare,
                use_ibkr=getattr(args, "ibkr", False),
                use_longport=getattr(args, "longport", False),
                crypto_source=getattr(args, "crypto_source", "auto"),
            )
            if df is None or df.empty:
                print(f"⚠ {code}: 无有效K线数据, 已跳过", file=sys.stderr)
            else:
                frames[code] = df
        except Exception as e:
            print(f"⚠ {code}: {e}, 已跳过", file=sys.stderr)
    if len(frames) < 2:
        _finish_with_error("取到数据的代码不足 2 个, 无法对比")

    period = args.period
    raw = getattr(args, "raw", False)
    # 市值对比 (可选指标): frames (股价) → 历史市值 frames (统一折算币种),
    # 下游归一化/JSON/HTML/比例线逻辑全部复用 (close=市值, K线同构)。
    metric = getattr(args, "metric", "price") or "price"
    mcap_meta: dict[str, dict] = {}
    target_ccy: str | None = None
    if metric == "mcap":
        from .. import mcap as mcap_mod
        from .. import storage

        target_ccy = (
            getattr(args, "currency", None) or storage.load_portfolio()["base_currency"]
        ).upper()
        mcap_frames, mcap_warnings, mcap_meta = mcap_mod.compare_market_caps(
            frames, target_ccy
        )
        for w in mcap_warnings:
            print(f"⚠ {w}", file=sys.stderr)
        if len(mcap_frames) < 2:
            _finish_with_error("取到历史市值数据的代码不足 2 个, 无法市值对比")
        frames = mcap_frames
    ratio_payload = (
        _build_ratio(
            frames,
            args.ratio,
            period,
            fx_adjust=getattr(args, "ratio_fx", False) and metric != "mcap",
        )
        if getattr(args, "ratio", None)
        else None
    )
    if args.json:
        payload = charting.compare_payload(
            frames, normalize=not raw, period=period, ratio=ratio_payload
        )
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
        ratio_out = None
        if ratio_payload:
            rvals = [p["value"] for p in ratio_payload["data"]]
            first, last = rvals[0], rvals[-1]
            ratio_out = {
                "numerator": ratio_payload["numerator"],
                "denominator": ratio_payload["denominator"],
                "fx_adjusted": ratio_payload["fxAdjusted"],
                "fx_pair": ratio_payload["fxPair"],
                "mean": ratio_payload["mean"],
                "precision": ratio_payload["precision"],
                "points": len(rvals),
                "first": first,
                "last": last,
                "change_pct": round((last / first - 1) * 100, 4) if first else None,
                "series": ratio_payload["data"],
            }
        _print_json(
            {
                "period": period,
                "months": args.months,
                "metric": metric,
                "currency": target_ccy,
                "sources": mcap_meta,
                "normalized": not raw,
                "codes": list(frames.keys()),
                "lines": lines,
                "ratio": ratio_out,
            }
        )
        return

    # HTML: plotly 折线图; 默认归一化 (起点=100) 便于跨币种/量级, --raw 原币种收盘价
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    # 双纵轴: 右侧对比折线, 左侧比例线 (比例线是具体比值, 不归一化)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    palette = charting.COMPARE_PALETTE
    for i, (code, df) in enumerate(frames.items()):
        d = charting.resample_ohlc(df, period) if period != "daily" else df
        y = d["close"].astype(float)
        if not args.raw:
            y = y / y.iloc[0] * 100
        elif metric == "mcap":
            # 绝对市值 plot 时折算到亿 (raw 大数轴可读性差; JSON 输出仍为原值)
            y = y / 1e8
        fig.add_trace(
            go.Scatter(
                x=d["date"],
                y=y,
                mode="lines",
                name=code,
                line=dict(color=palette[i % len(palette)], width=1.8, shape="spline"),
                hovertemplate="%{y:.2f}<extra>" + code + " %{x|%Y-%m-%d}</extra>",
            )
        )
    if ratio_payload:
        rd = ratio_payload["data"]
        fig.add_trace(
            go.Scatter(
                x=[p["time"] for p in rd],
                y=[p["value"] for p in rd],
                mode="lines",
                name=ratio_payload["name"],
                line=dict(color=ratio_payload["color"], width=1.8),
                hovertemplate="%{y:.4f}<extra>比例 "
                + ratio_payload["name"]
                + "</extra>",
            ),
            secondary_y=True,
        )
        fig.add_hline(
            y=ratio_payload["mean"],
            line_dash="dot",
            line_color="#8b949e",
            yref="y2",
            annotation_text="均值",
            annotation_position="top left",
        )
    label_head = "市值对比" if metric == "mcap" else "走势对比"
    label_y = (
        "归一化 (起点=100)"
        if not args.raw
        else (
            f"市值 (亿 {target_ccy})"
            if metric == "mcap"
            else "收盘价 (各代码原币种)"
        )
    )
    layout = dict(
        title=f"{label_head} · 近 {args.months} 个月 · {period}"
        + (f" · 统一 {target_ccy}" if metric == "mcap" else "")
        + " · " + " vs ".join(codes)
        + ("" if args.raw else " (起点=100)"),
        template="plotly_white",
        yaxis_title=label_y,
        hovermode="x unified",
        height=560,
    )
    if ratio_payload:
        # 比例线: 与折线同图叠加, 但使用独立的左侧纵轴 (具体比值, 不归一化);
        # 对比折线的归一化轴放在右侧 (与页面组件的布局一致)
        layout["yaxis"] = dict(side="right")
        layout["yaxis2"] = dict(
            overlaying="y",
            side="left",
            showgrid=False,
            automargin=True,
            title=f"比例 {ratio_payload['numerator']}/{ratio_payload['denominator']} (未归一化)",
        )
    fig.update_layout(**layout)
    out = (
        Path(args.output)
        if args.output
        else VAR_DIR
        / (
            f"compare_{'mcap_' if metric == 'mcap' else ''}{'-'.join(c[:6] for c in codes)}.html"
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_dir():
        _finish_with_error(f"输出路径是一个目录, 请指定文件: {out}")
    try:
        out.write_text(charting.fig_to_html(fig), encoding="utf-8")
    except OSError as e:
        # 目录/坏父路径/只读: 与 report/export 同口径给可读错误 + 退出码,
        # 而不是 IsADirectoryError 裸 traceback
        _finish_with_error(f"对比图写入失败: {out} ({e})")
    # 终端摘要: 区间涨跌 (+ 市值模式的最新市值与口径)
    mcap_mod = None
    if metric == "mcap":
        from .. import mcap as mcap_mod  # noqa: F811  本地别名供摘要格式化
    chg_parts = []
    mc_parts = []
    for code, df in frames.items():
        d = charting.resample_ohlc(df, period) if period != "daily" else df
        close = d["close"].astype(float)
        pct = (close.iloc[-1] / close.iloc[0] - 1) * 100
        chg_parts.append(f"{code} {pct:+.2f}%")
        if metric == "mcap":
            src = (mcap_meta.get(code) or {}).get("source_label")
            mc_parts.append(
                f"{code} {mcap_mod.describe_amount(float(close.iloc[-1]))} {target_ccy}"
                + (f" ({src})" if src else "")
            )
    print(f"\n=== {label_head} (近 {args.months} 个月, {period}) ===")
    print("区间涨跌: " + " · ".join(chg_parts))
    if metric == "mcap":
        print("最新市值: " + " · ".join(mc_parts))
        print(f"市值已统一折算为 {target_ccy} (历史汇率按日折算; crypto/指数无市值语义)")
    if ratio_payload:
        last = ratio_payload["data"][-1]["value"]
        mean = ratio_payload["mean"]
        dev = (last / mean - 1) * 100 if mean else 0
        fx_txt = (
            f" (按 {ratio_payload['fxPair']} 汇率调整)"
            if ratio_payload.get("fxAdjusted")
            else ""
        )
        print(
            f"比例线 {ratio_payload['name']} (具体比值, 未归一化):"
            f" 最新 {last:.{ratio_payload['precision']}f}"
            f" · 均值 {mean:.{ratio_payload['precision']}f}"
            f" · 较均值 {dev:+.2f}%{fx_txt}"
        )
    print(f"\n✅ 对比图已生成: {out}")
    if args.open_browser:
        webbrowser.open(out.resolve().as_uri())
        print("已在浏览器中打开。")
