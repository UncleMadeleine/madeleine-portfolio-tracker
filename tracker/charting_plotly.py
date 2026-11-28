"""Plotly 蜡烛图构建与 HTML 导出 (plotly 仍在函数内惰性导入)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from .charting_core import (
    DEFAULT_MA,
    MA_PALETTE,
    PERIOD_LABELS,
    _non_trading_days,
    _price_precision,
    clean_ohlc,
    compute_ma,
    up_down_colors,
)

if TYPE_CHECKING:  # 仅类型标注用 (运行时不依赖 plotly, 见构建函数内惰性导入)
    import plotly.graph_objects as go

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
    allow_nonpositive: bool = False,
    reference: float | None = None,
    reference_label: str | None = None,
) -> go.Figure:
    """构建蜡烛图: 价格主图 + 成交量副图 + MA 均线 + 非交易日断轴 + 区间快捷按钮.

    allow_nonpositive 透传给 clean_ohlc, 供利率/利差等可负值序列使用。
    reference / reference_label: 主图水平参考线 (如利差的 0 倒挂分界)。
    """
    # plotly 惰性导入: CLI 的 --json 数据路径 (无图表) 不需要它,
    # 装核心依赖 (yfinance/akshare/pandas) 的 Agent 用户无需拖 plotly
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    df = clean_ohlc(df, allow_nonpositive=allow_nonpositive)
    if df.empty:
        raise ValueError(f"{symbol}: 无有效K线数据")
    up, down = up_down_colors(green_up)
    dates = df["date"]
    # 与 K线组件同一套精度规则: 固定 .2f 会把 SHIB/PEPE 这类低价币的
    # 悬停 OHLC 和最新价都显示成 0.00。
    fmt = f".{_price_precision(float(df['close'].median()))}f"

    if show_volume:
        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            row_heights=[0.74, 0.26],
            vertical_spacing=0.03,
        )
        price_row, vol_row = 1, 2
    else:
        fig = make_subplots(rows=1, cols=1)
        price_row, vol_row = 1, None

    candle = go.Candlestick(
        x=dates,
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name=PERIOD_LABELS.get(period, "日K"),
        increasing=dict(line=dict(color=up, width=1.5), fillcolor=up),
        decreasing=dict(line=dict(color=down, width=1.5), fillcolor=down),
        whiskerwidth=0.2,
        hovertemplate=(
            "<b>%{x|%Y-%m-%d}</b><br>"
            f"开 %{{open:{fmt}}}  高 %{{high:{fmt}}}<br>"
            f"低 %{{low:{fmt}}}  收 %{{close:{fmt}}}<extra></extra>"
        ),
    )
    fig.add_trace(candle, row=price_row, col=1)
    for i, (n, srs) in enumerate(compute_ma(df, mas).items()):
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=srs,
                mode="lines",
                name=f"MA{n}",
                line=dict(
                    color=MA_PALETTE[i % len(MA_PALETTE)], width=1.6, shape="spline"
                ),
                connectgaps=False,
                hovertemplate=f"MA{n} %{{y:{fmt}}}<extra></extra>",
            ),
            row=price_row,
            col=1,
        )
    if vol_row is not None:
        vol_colors = np.where(df["close"] >= df["open"], up, down)
        fig.add_trace(
            go.Bar(
                x=dates,
                y=df["volume"],
                name="成交量",
                marker_color=vol_colors,
                opacity=0.5,
                showlegend=False,
                hovertemplate="量 %{y:,.0f}<extra></extra>",
            ),
            row=vol_row,
            col=1,
        )

    breaks = [dict(values=_non_trading_days(dates))]
    buttons = [
        dict(count=1, label="1个月", step="month", stepmode="backward"),
        dict(count=3, label="3个月", step="month", stepmode="backward"),
        dict(count=6, label="6个月", step="month", stepmode="backward"),
        dict(count=1, label="1年", step="year", stepmode="backward"),
        dict(count=5, label="5年", step="year", stepmode="backward"),
        dict(step="all", label="全部"),
    ]
    if title is None:
        title = f"{symbol} · {PERIOD_LABELS.get(period, '日K')}" + (
            f" · {currency}" if currency else ""
        )
    last_close = float(df["close"].iloc[-1])
    # 最新价方向惯例相对前一根收盘 (非本根 open): 低开高收时应 ▲
    prev_close = (
        float(df["close"].iloc[-2]) if len(df) > 1 else float(df["open"].iloc[-1])
    )
    up_marker = "▲" if last_close >= prev_close else "▼"
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0a0a0f",
        plot_bgcolor="#0a0a0f",
        title=dict(text=title, x=0.5, xanchor="center", font=dict(size=15)),
        height=height,
        margin=dict(l=8, r=64, t=48, b=12),
        hovermode="x unified",
        legend=dict(
            orientation="h", yanchor="bottom", y=1.0, x=0, bgcolor="rgba(0,0,0,0)"
        ),
        font=dict(
            family="system-ui, -apple-system, sans-serif", size=12, color="#d7dcf0"
        ),
        dragmode="pan",
        xaxis=dict(
            showspikes=True,
            spikemode="across",
            spikethickness=1,
            spikecolor="rgba(120,140,180,0.4)",
            spikesnap="cursor",
        ),
        yaxis=dict(
            showspikes=True,
            spikemode="across",
            spikethickness=1,
            spikecolor="rgba(120,140,180,0.4)",
            spikesnap="cursor",
        ),
        shapes=[
            dict(
                type="line",
                xref="paper",
                x0=0,
                x1=1,
                yref="y",
                y0=last_close,
                y1=last_close,
                line=dict(color="rgba(200,210,230,0.5)", width=1, dash="dot"),
            ),
        ],
        annotations=[
            dict(
                x=1.0,
                y=last_close,
                xref="paper",
                yref="y",
                xanchor="left",
                yanchor="middle",
                text=f"{up_marker} {last_close:{fmt}}",
                showarrow=False,
                font=dict(size=11, color="#e8eaed"),
                bgcolor="rgba(40,44,52,0.85)",
                bordercolor="rgba(120,140,180,0.3)",
                borderwidth=1,
                borderpad=3,
            ),
        ],
    )
    fig.update_xaxes(
        rangeslider_visible=False,
        showgrid=False,
        rangebreaks=breaks,
        showline=True,
        linecolor="rgba(120,140,180,0.2)",
    )
    # 参考线必须在 update_layout(shapes=...) 之后追加, 否则会被最新价线覆盖
    if reference is not None:
        fig.add_hline(
            y=reference,
            line_width=1,
            line_dash="dash",
            line_color="#8b949e",
            annotation_text=reference_label or "",
            annotation_position="top left",
            row=price_row,
            col=1,
        )
    fig.update_xaxes(
        rangeselector=dict(buttons=buttons, bgcolor="rgba(30,34,40,0.8)"),
        row=price_row,
        col=1,
    )
    fig.update_yaxes(
        side="right",
        showgrid=True,
        gridcolor="rgba(120,140,180,0.06)",
        showline=True,
        linecolor="rgba(120,140,180,0.2)",
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


