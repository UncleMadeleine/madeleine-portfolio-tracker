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


def _range_labels(rows: list[dict]) -> list[str]:
    """按已有净值跨度裁剪区间选项: 不提供比历史更长的回看窗口.

    只有 3 天数据时, 90/180/365 天与「全部」完全等价, 选项越多越误导;
    只保留真正有意义的档位 (数据不足 30 天时仅「全部」), 历史积累后再逐步
    放出 30/90/180/365 天。
    """
    if not rows:
        return list(NAV_RANGES)
    first = pd.Timestamp(rows[0]["day"])
    last = pd.Timestamp(rows[-1]["day"])
    span = max(1, int((last - first).days) + 1)  # 含首尾两天
    return [
        label
        for label, days in NAV_RANGES.items()
        if days is None or days <= span
    ]


def _default_range_label(labels: list[str]) -> str:
    """默认区间: 优先 180 天; 历史不足时取可用的最长有限档, 否则「全部」."""
    if "180 天" in labels:
        return "180 天"
    bounded = [
        label
        for label, days in NAV_RANGES.items()
        if days is not None and label in labels
    ]
    return bounded[-1] if bounded else "全部"


def _rows_in_range(rows: list[dict], days: int | None) -> list[dict]:
    """按最后一个快照日向前取 N 个自然日 (含首尾).

    以最后一个快照而非「今天」为锚: 长期没打开页面时, 以今天为锚会让
    「30 天」窗口落到没有数据的区间, 曲线直接空白。窗口内无数据时回退到
    全部, 避免选项存在却画不出东西。
    """
    if not rows or not days:
        return rows
    last = pd.Timestamp(rows[-1]["day"])
    cutoff = last - pd.Timedelta(days=int(days) - 1)
    out = [r for r in rows if pd.Timestamp(r["day"]) >= cutoff]
    return out or rows


def _render_nav_section(base: str) -> None:
    """组合页「净值走势」: 读取历史净值, 画总市值 + 成本两条线.

    历史由 snapshot --save / 组合页每次成功渲染时自动 upsert; 没有历史时
    给出保存/回填命令提示, 而不是画一张空图。
    """
    with st.container(border=True, key="fp_nav"):
        head_l, head_r = st.columns([3, 2], vertical_alignment="center")
        head_l.markdown("**:material/show_chart: 净值走势**")
        all_rows = nav.load_history(base)
        if not all_rows:
            st.caption(
                "尚无历史净值。执行 `tracker nav --save` 保存今天，或 "
                "`tracker nav --backfill --days 365` 按当前持仓回填近似历史。"
            )
            return
        labels = _range_labels(all_rows)
        # 选项随历史增长而变化: 旧的已选档位若已不在列表 (历史被清理) 会
        # 让 radio 状态失效, 先清掉让默认值重新生效。
        if st.session_state.get("nav_range") not in labels:
            st.session_state.pop("nav_range", None)
        if len(labels) > 1:
            range_label = head_r.radio(
                "净值区间",
                labels,
                index=labels.index(_default_range_label(labels)),
                horizontal=True,
                key="nav_range",
                label_visibility="collapsed",
            )
        else:
            range_label = labels[0]  # 只有一档时不必占一行控件
        rows = _rows_in_range(all_rows, NAV_RANGES[range_label])

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
            f"{first['day']} → {last['day']} · {len(rows)} 个快照 · "
            f"区间 **{delta}** · 最新 {last['total_value']:,.0f} {base}"
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


