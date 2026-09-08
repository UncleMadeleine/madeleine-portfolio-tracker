"""K线图 (蜡烛图): 基于 plotly 的开源可视化, 含成交量 / 均线 / 周月K / 非交易日断轴.

纯函数模块: 输入 OHLC DataFrame (date/open/high/low/close/volume, 升序),
输出可渲染的 plotly Figure 或独立 HTML. 不做任何网络请求.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.tseries.frequencies import to_offset
import plotly.graph_objects as go
from plotly.subplots import make_subplots

OHLC_COLUMNS = ("date", "open", "high", "low", "close", "volume")
DEFAULT_MA: tuple[int, ...] = (5, 20, 60)

# 默认中国看盘软件配色: 红涨绿跌; green_up=True 时切换为国际配色 (绿涨红跌)
CN_UP_COLOR, CN_DOWN_COLOR = "#ef232a", "#14b143"
INTL_UP_COLOR, INTL_DOWN_COLOR = "#089981", "#f23648"
VOLUME_OPACITY = 0.75
MA_PALETTE = ("#f7d774", "#4ea1f3", "#c883f0", "#ff9f43", "#e15b64", "#2ccbc3", "#8d9db6")

PERIOD_LABELS = {"daily": "日K", "weekly": "周K", "monthly": "月K"}


def clean_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """清洗日线数据, 保证绘图与统计准确:

    - date 解析失败 / OHLC 任一缺失的行丢弃
    - 逻辑矛盾行丢弃 (high < low, high < max(open,close), low > min(open,close), 价格<=0)
    - 按日期升序排序, 重复日期保留最后一条
    """
    empty = pd.DataFrame(columns=list(OHLC_COLUMNS))
    if df is None or df.empty:
        return empty
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for c in ("open", "high", "low", "close"):
        if c not in out.columns:
            out[c] = np.nan
        out[c] = pd.to_numeric(out[c], errors="coerce")
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    body_hi = out[["open", "close"]].max(axis=1)
    body_lo = out[["open", "close"]].min(axis=1)
    ok = (
        (out["high"] >= out["low"] - 1e-9)
        & (out["high"] >= body_hi - 1e-9)
        & (out["low"] <= body_lo + 1e-9)
        & (out["close"] > 0)
    )
    out = out[ok]
    # 稳定排序: 重复日期保留输入中最后一条 (来源覆盖顺序有意义)
    out = out.sort_values("date", kind="stable").drop_duplicates(subset="date", keep="last")
    return out.reset_index(drop=True)[list(OHLC_COLUMNS)]


def parse_ma_periods(spec: str | int | list | tuple) -> tuple[int, ...]:
    """'5,20,60' -> (5, 20, 60); 无效/过小(<2)的周期忽略, 结果去重升序."""
    if isinstance(spec, str):
        parts = [p.strip() for p in spec.split(",") if p.strip()]
    elif isinstance(spec, (int, np.integer)):
        parts = [str(spec)]
    else:
        parts = [str(p).strip() for p in (spec or []) if p is not None]
    periods: set[int] = set()
    for p in parts:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if n >= 2:
            periods.add(n)
    return tuple(sorted(periods))


def compute_ma(df: pd.DataFrame, periods) -> dict[int, pd.Series]:
    """收盘价简单移动平均; 不足一个窗口的前段为 NaN, 窗口大于样本数则不计算."""
    close = df["close"].astype(float)
    out: dict[int, pd.Series] = {}
    for n in parse_ma_periods(periods):
        if n <= len(df):
            out[n] = close.rolling(window=n, min_periods=n).mean()
    return out


def _month_rule() -> str:
    """pandas 3.x 移除了 'M'; 用偏移解析探测而非版本字符串比较 (2.10+ 会误判)."""
    try:
        to_offset("ME")
        return "ME"
    except ValueError:
        return "M"


def resample_ohlc(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """日线重采样为周K (W-FRI) / 月K; open=首日开, high=最高, low=最低, close=末日收, volume=求和."""
    if period == "daily" or df is None or df.empty:
        return clean_ohlc(df)
    rule = "W-FRI" if period == "weekly" else _month_rule()
    out = (
        clean_ohlc(df)
        .set_index("date")
        .resample(rule)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return out[list(OHLC_COLUMNS)]


def _non_trading_days(dates: pd.Series) -> list[pd.Timestamp]:
    """区间内不在数据中的日历日 (周末+节假日), 用于 rangebreaks 消除图表空隙."""
    if len(dates) < 2:
        return []
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    full = pd.date_range(idx.min(), idx.max(), freq="D")
    return list(full.difference(idx))


def build_candlestick_fig(
    df: pd.DataFrame,
    symbol: str,
    currency: str | None = None,
    mas=DEFAULT_MA,
    show_volume: bool = True,
    green_up: bool = False,
    period: str = "daily",
    title: str | None = None,
    height: int = 680,
) -> go.Figure:
    """构建蜡烛图: 价格主图 + 成交量副图 + MA 均线 + 非交易日断轴 + 区间快捷按钮."""
    df = clean_ohlc(df)
    if df.empty:
        raise ValueError(f"{symbol}: 无有效K线数据")
    up, down = (INTL_UP_COLOR, INTL_DOWN_COLOR) if green_up else (CN_UP_COLOR, CN_DOWN_COLOR)
    dates = df["date"]

    if show_volume:
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.74, 0.26], vertical_spacing=0.03,
        )
        price_row, vol_row = 1, 2
    else:
        fig = make_subplots(rows=1, cols=1)
        price_row, vol_row = 1, None

    candle = go.Candlestick(
        x=dates, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name=PERIOD_LABELS.get(period, "日K"),
        increasing=dict(line=dict(color=up, width=1.5), fillcolor=up),
        decreasing=dict(line=dict(color=down, width=1.5), fillcolor=down),
        whiskerwidth=0.2,
        hovertemplate=(
            "<b>%{x|%Y-%m-%d}</b><br>"
            "开 %{open:.2f}  高 %{high:.2f}<br>"
            "低 %{low:.2f}  收 %{close:.2f}<extra></extra>"
        ),
    )
    fig.add_trace(candle, row=price_row, col=1)
    for i, (n, srs) in enumerate(compute_ma(df, mas).items()):
        fig.add_trace(
            go.Scatter(
                x=dates, y=srs, mode="lines", name=f"MA{n}",
                line=dict(color=MA_PALETTE[i % len(MA_PALETTE)], width=1.6, shape="spline"),
                connectgaps=False,
                hovertemplate=f"MA{n} %{{y:.2f}}<extra></extra>",
            ),
            row=price_row, col=1,
        )
    if vol_row is not None:
        vol_colors = np.where(df["close"] >= df["open"], up, down)
        fig.add_trace(
            go.Bar(
                x=dates, y=df["volume"], name="成交量",
                marker_color=vol_colors,
                opacity=0.5, showlegend=False,
                hovertemplate="量 %{y:,.0f}<extra></extra>",
            ),
            row=vol_row, col=1,
        )

    breaks = [dict(values=_non_trading_days(dates))]
    buttons = [
        dict(count=1, label="1个月", step="month", stepmode="backward"),
        dict(count=3, label="3个月", step="month", stepmode="backward"),
        dict(count=6, label="6个月", step="month", stepmode="backward"),
        dict(count=1, label="1年", step="year", stepmode="backward"),
        dict(step="all", label="全部"),
    ]
    if title is None:
        title = f"{symbol} · {PERIOD_LABELS.get(period, '日K')}" + (
            f" · {currency}" if currency else ""
        )
    last_close = float(df["close"].iloc[-1])
    up_marker = "▲" if last_close >= float(df["open"].iloc[-1]) else "▼"
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0e1117",
        plot_bgcolor="#0e1117",
        title=dict(text=title, x=0.5, xanchor="center", font=dict(size=15)),
        height=height,
        margin=dict(l=8, r=64, t=48, b=12),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, bgcolor="rgba(0,0,0,0)"),
        font=dict(family="system-ui, -apple-system, sans-serif", size=12, color="#d1d5db"),
        dragmode="zoom",
        xaxis=dict(
            showspikes=True, spikemode="across", spikethickness=1,
            spikecolor="rgba(120,140,180,0.4)", spikesnap="cursor",
        ),
        yaxis=dict(
            showspikes=True, spikemode="across", spikethickness=1,
            spikecolor="rgba(120,140,180,0.4)", spikesnap="cursor",
        ),
        shapes=[
            dict(
                type="line", xref="paper", x0=0, x1=1,
                yref="y", y0=last_close, y1=last_close,
                line=dict(color="rgba(200,210,230,0.5)", width=1, dash="dot"),
            ),
        ],
        annotations=[
            dict(
                x=1.0, y=last_close, xref="paper", yref="y",
                xanchor="left", yanchor="middle",
                text=f"{up_marker} {last_close:.2f}",
                showarrow=False,
                font=dict(size=11, color="#e8eaed"),
                bgcolor="rgba(40,44,52,0.85)",
                bordercolor="rgba(120,140,180,0.3)",
                borderwidth=1, borderpad=3,
            ),
        ],
    )
    fig.update_xaxes(
        rangeslider_visible=False, showgrid=False,
        rangebreaks=breaks,
        showline=True, linecolor="rgba(120,140,180,0.2)",
    )
    fig.update_xaxes(rangeselector=dict(buttons=buttons, bgcolor="rgba(30,34,40,0.8)"), row=price_row, col=1)
    fig.update_yaxes(
        side="right", showgrid=True, gridcolor="rgba(120,140,180,0.06)",
        showline=True, linecolor="rgba(120,140,180,0.2)",
        tickfont=dict(size=11),
    )
    if vol_row is not None:
        fig.update_yaxes(row=vol_row, col=1, tickformat="~s", showgrid=False)
    return fig


def fig_to_html(fig: go.Figure) -> str:
    """导出为自包含 HTML (内嵌 plotly.js, 离线可打开)."""
    return fig.to_html(
        include_plotlyjs=True,
        full_html=True,
        config={
            "displaylogo": False,
            "scrollZoom": True,
            "modeBarButtonsToRemove": ["select2d", "lasso2d"],
        },
    )


def summarize_ohlc(df: pd.DataFrame, mas=DEFAULT_MA) -> dict:
    """K线摘要统计 (用于终端输出 / --json / 页面指标)."""
    df = clean_ohlc(df)
    if df.empty:
        return {"bars": 0}
    first, last = df.iloc[0], df.iloc[-1]
    change_pct = (
        (float(last["close"]) / float(first["close"]) - 1) * 100
        if float(first["close"]) > 0
        else None
    )
    hi = df.loc[df["high"].idxmax()]
    lo = df.loc[df["low"].idxmin()]
    ma_vals: dict[str, float | None] = {}
    for n, srs in compute_ma(df, mas).items():
        v = srs.iloc[-1]
        ma_vals[f"ma{n}"] = None if pd.isna(v) else round(float(v), 4)
    return {
        "bars": int(len(df)),
        "first_date": first["date"].strftime("%Y-%m-%d"),
        "last_date": last["date"].strftime("%Y-%m-%d"),
        "first_close": round(float(first["close"]), 4),
        "last_open": round(float(last["open"]), 4),
        "last_high": round(float(last["high"]), 4),
        "last_low": round(float(last["low"]), 4),
        "last_close": round(float(last["close"]), 4),
        "last_volume": float(last["volume"]) if pd.notna(last["volume"]) else None,
        "change_pct": round(change_pct, 4) if change_pct is not None else None,
        "period_high": round(float(hi["high"]), 4),
        "period_high_date": hi["date"].strftime("%Y-%m-%d"),
        "period_low": round(float(lo["low"]), 4),
        "period_low_date": lo["date"].strftime("%Y-%m-%d"),
        "ma": ma_vals,
    }
