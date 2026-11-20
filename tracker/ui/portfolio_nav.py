"""组合页「净值走势」区: 历史净值读取与 Plotly 曲线."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tracker import nav

from . import theme

NAV_RANGES = {
    "30 天": 30,
    "90 天": 90,
    "180 天": 180,
    "365 天": 365,
    "全部": None,
}


def _render_nav_section(base: str) -> None:
    """组合页「净值走势」: 读取历史净值, 画总市值 + 成本两条线.

    历史由 snapshot --save / 组合页每次成功渲染时自动 upsert; 没有历史时
    给出保存/回填命令提示, 而不是画一张空图。
    """
    with st.container(border=True, key="fp_nav"):
        head_l, head_r = st.columns([3, 2], vertical_alignment="center")
        head_l.markdown("**:material/show_chart: 净值走势**")
        range_label = head_r.radio(
            "净值区间",
            list(NAV_RANGES),
            index=2,
            horizontal=True,
            key="nav_range",
            label_visibility="collapsed",
        )
        rows = nav.load_history(base, days=NAV_RANGES[range_label])
        if not rows:
            st.caption(
                "尚无历史净值。执行 `tracker nav --save` 保存今天，或 "
                "`tracker nav --backfill --days 365` 按当前持仓回填近似历史。"
            )
            return

        df = pd.DataFrame(rows)
        df["day"] = pd.to_datetime(df["day"])
        first, last = rows[0], rows[-1]
        change = (last["total_value"] or 0) - (first["total_value"] or 0)
        pct = (
            change / abs(first["total_value"])
            if first["total_value"] not in (None, 0)
            else None
        )
        delta = f"{change:+,.0f} {base}" + (f" ({pct:+.2%})" if pct is not None else "")
        head_l.caption(
            f"{first['day']} → {last['day']} · 区间 **{delta}** · "
            f"最新 {last['total_value']:,.0f} {base}"
        )

        fig = go.Figure()
        # 只有 1-2 个快照时纯 lines 在图上几乎不可见; 补圆点让用户知道数据点在哪。
        nav_mode = "lines+markers" if len(df) <= 2 else "lines"
        fig.add_trace(
            go.Scatter(
                x=df["day"],
                y=df["total_value"],
                mode=nav_mode,
                marker=dict(size=7),
                name=f"总市值 ({base})",
                line=dict(width=2),
            )
        )
        if df["total_cost"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=df["day"],
                    y=df["total_cost"],
                    mode=nav_mode,
                    marker=dict(size=7),
                    name="成本",
                    line=dict(width=1.5, dash="dash"),
                )
            )
        fig.update_layout(
            height=280,
            margin=dict(l=0, r=0, t=10, b=0),
            hovermode="x unified",
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
            ),
            yaxis=dict(tickformat=",.0f", hoverformat=",.2f"),
        )
        # 统一日期刻度: 避免单点数据时 Plotly 自动生成 "23:59:59.999" 这类
        # 无意义刻度 (手机上尤其明显), 同时限制刻度数量防止窄屏挤成一团。
        tick_fmt, hover_fmt = "%m-%d", "%Y-%m-%d"
        fig.update_xaxes(tickformat=tick_fmt, hoverformat=hover_fmt, nticks=6)
        if len(df) == 1:
            d0 = df["day"].iloc[0]
            fig.update_xaxes(
                range=[d0 - pd.Timedelta(days=1), d0 + pd.Timedelta(days=1)],
                tickvals=[d0],
                ticktext=[d0.strftime("%Y-%m-%d")],
            )
        st.plotly_chart(
            theme.style_chart(fig), width="stretch", config={"displayModeBar": False}
        )
        if any(r.get("source") == "backfill" for r in rows):
            st.caption(
                "回填口径: 当前持仓 × 历史收盘价 (含历史汇率折算), 不含历史调仓; "
                "只覆盖能取到历史行情的持仓, 缺数据标的见 CLI `nav --backfill` 的问题列表。"
            )


