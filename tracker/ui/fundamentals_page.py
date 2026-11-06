"""「基本面分析」页面: 第一期只做单只 A+H 股票的 AH 溢价率曲线.

数据口径见 tracker.ah_premium: A 股收盘价 ÷ (H 股收盘价 × HKD→CNY 汇率) − 1,
以 A 股交易日为时间轴, H 股/汇率取当日或之前最近收盘。曲线用与 K线页相同的
lightweight-charts 组件 (拖动/缩放/十字光标), 0% 处画同价参考线。

页面与「K线」页一样按需取数: 打开页面先取一次股票对目录 (1h 缓存), 选中股票对
后才拉 A/H 历史; 换股票对/范围即时重取。后续基本面指标 (PE/PB/股息率等) 应作为
本页的独立子视图加入, 用 segmented_control 懒加载而不是 st.tabs, 避免隐藏 tab
在打开页面时就发起一批网络请求。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from . import settings, theme

from tracker import ah_premium, charting

# 与 K线页共用组件实现, 但用独立注册名: 同一 Streamlit 进程内不同组件实例
# 各自持有 DOM/视口状态, 避免与 K线页的图表互相影响。
_LINE_CHART = st.components.v2.component(
    "lwc_fundamentals_line",
    html=charting.KLINE_COMPONENT_HTML,
    css=charting.KLINE_COMPONENT_CSS,
    js=charting.kline_component_js(),
)

_MONTHS = [3, 6, 12, 24, 36, 60]


@st.cache_data(ttl=3600, show_spinner=False)
def cached_ah_pairs():
    """A+H 股票对目录 (磁盘无关的进程内缓存; 目录失败由模块内置兜底)."""
    return ah_premium.list_pairs()


@st.cache_data(ttl=1800, show_spinner=False)
def cached_ah_premium(
    a_symbol: str,
    h_symbol: str,
    months: int,
    refresh: bool = False,
):
    """AH 溢价曲线 (st.cache_data 层; 底层 K线/汇率本身还有磁盘缓存)."""
    return ah_premium.fetch_ah_premium(
        a_symbol,
        h_symbol,
        months=months,
        refresh=refresh,
    )


def clear_ah_caches() -> None:
    """强制刷新: 清页面缓存, 并让底层取数穿透磁盘缓存/汇率进程缓存."""
    cached_ah_pairs.clear()
    cached_ah_premium.clear()


def _pair_label(pair: ah_premium.AHPair) -> str:
    # 标签保持稳定 (不含实时溢价): 目录每小时刷新时溢价数值会变, 若进标签
    # 会让当前选中项匹配不上而回落到默认股票对
    return f"{pair.name} · {pair.a_symbol} / {pair.h_symbol}"


def _fmt_price(value: float) -> str:
    """价格精度随量级 (与 K线摘要同口径的简化版)."""
    v = float(value)
    if v >= 100:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.3f}"
    return f"{v:.4f}"


def render_fundamentals_page() -> None:
    """「基本面分析」页入口."""
    theme.page_header("基本面分析", eyebrow="FUNDAMENTALS")
    st.caption(
        "第一期: 单只 A+H 股票的 AH 溢价率曲线。"
        "A 股与 H 股收盘价按历史汇率折算到同一币种后比较; 0% 虚线 = 两地同价。"
    )

    with st.spinner("加载 A+H 股票对目录..."):
        pairs = cached_ah_pairs()
    manual = st.toggle(
        "手动输入代码",
        value=False,
        key="ah_manual",
        help="目录里没有想要的股票对, 或想查任意 A 股/港股组合时打开",
    )

    selected = pairs[0] if pairs else None
    if pairs:
        labels = [_pair_label(p) for p in pairs]
        default_idx = ah_premium.default_pair_index(pairs)
        current = st.session_state.get("ah_pair")
        if current in labels:
            default_idx = labels.index(current)
        chosen = st.selectbox(
            "A+H 股票对",
            labels,
            index=default_idx,
            key="ah_pair",
            disabled=manual,
        )
        selected = pairs[labels.index(chosen)]
    else:
        st.warning("股票对目录不可用, 请在下方手动输入 A 股 / H 股代码。")

    c_range, c_force = st.columns([1, 1.2], vertical_alignment="bottom")
    months = c_range.selectbox(
        "范围",
        _MONTHS,
        index=2,
        format_func=lambda m: f"近 {m} 个月",
        key="ah_months",
    )
    force = c_force.button(
        ":material/refresh: 强制刷新",
        key="ah_force",
        help="忽略页面缓存、K线磁盘缓存与汇率缓存, 立即重新取数",
    )
    if force:
        clear_ah_caches()

    # 手动模式: 两个输入框都填了才覆盖下拉选择; 留空则回退到选中的股票对
    a_raw = selected.a_symbol if selected else "601398.SS"
    h_raw = selected.h_symbol if selected else "1398.HK"
    if manual:
        c_a, c_h = st.columns(2)
        a_raw = c_a.text_input(
            "A 股代码",
            value=a_raw,
            key="ah_a_code",
            placeholder="如 601398.SS / 600036.SS",
        )
        h_raw = c_h.text_input(
            "H 股代码",
            value=h_raw,
            key="ah_h_code",
            placeholder="如 1398.HK / 3968.HK",
        )

    try:
        a_symbol = ah_premium.normalize_a_symbol(a_raw)
        h_symbol = ah_premium.normalize_h_symbol(h_raw)
    except ValueError as e:
        st.error(str(e))
        return

    pair_name = (
        selected.name
        if selected and (a_symbol, h_symbol) == (selected.a_symbol, selected.h_symbol)
        else f"{a_symbol} / {h_symbol}"
    )

    try:
        with st.spinner(f"拉取 {a_symbol} / {h_symbol} 近 {months} 个月行情..."):
            series, meta = cached_ah_premium(
                a_symbol,
                h_symbol,
                months,
                refresh=force,
            )
    except Exception as e:  # noqa: BLE001 - 单个股票对失败只提示, 页面可继续换代码
        st.warning(f"{a_symbol} / {h_symbol}: {e}")
        return

    if not isinstance(series, pd.DataFrame) or series.empty:
        st.warning(f"{a_symbol} / {h_symbol}: 无有效 AH 溢价数据。")
        return

    payload = charting.line_payload(
        series,
        value_col="premium_pct",
        title=f"{pair_name} · AH 溢价率 · 近 {months} 个月",
        series_name="AH 溢价率",
        precision=2,
        reference=0.0,
        reference_label="0% 同价",
        unit="%",
        green_up=settings.green_up(),
        height=520,
        # 曲线页默认展示所选范围的全貌 (K线页默认只露最近 140 根, 两者语义不同)
        init_bars=max(1, len(series)),
        query_id=f"{a_symbol}|{h_symbol}|{months}",
    )
    _LINE_CHART(key="ah_premium_chart", data=payload, height=520)

    summary = ah_premium.summarize_premium(series)
    # 与 K线页一致: 横向容器在窄屏自动排成 2×2, 而不是 4 张全宽卡片
    with st.container(horizontal=True):
        st.metric(
            "最新溢价率",
            f"{summary['last_premium']:+.2f}%",
            delta=f"{summary['change_pp']:+.2f}pp",
            delta_color=settings.delta_color(),
        )
        st.metric("区间均值", f"{summary['mean']:+.2f}%")
        st.metric(f"区间最高 ({summary['high_date']})", f"{summary['high']:+.2f}%")
        st.metric(f"区间最低 ({summary['low_date']})", f"{summary['low']:+.2f}%")

    fx_txt = (
        f" · {meta['fx_pair']} {summary['fx_rate']:.4f}"
        if meta.get("fx_adjusted")
        else ""
    )
    st.caption(
        f"最新: A 股 {_fmt_price(summary['a_close'])} {meta['a_currency']} · "
        f"H 股 {_fmt_price(summary['h_close'])} {meta['h_currency']}"
        f"{fx_txt} → H 股折合 {_fmt_price(summary['h_close_cny'])} "
        f"{meta['a_currency']} · 溢价 {summary['last_premium']:+.2f}%"
    )
    st.caption(
        f"区间 {summary['first_date']} ~ {summary['last_date']} "
        f"({summary['bars']} 个 A 股交易日)，当前溢价处于区间 "
        f"{summary['percentile']:.0f}% 分位。"
        "溢价率 = A 股收盘 ÷ (H 股收盘 × 汇率) − 1；正值 = A 股溢价。"
        "A/H 均优先 akshare 前复权口径，H 股休市日取最近一个收盘价。"
    )

    with st.expander("查看明细数据 (最近 60 个交易日)"):
        detail = series.tail(60).copy()
        detail["date"] = pd.to_datetime(detail["date"]).dt.strftime("%Y-%m-%d")
        detail = detail.rename(
            columns={
                "date": "日期",
                "a_close": f"A股收盘 ({meta['a_currency']})",
                "h_close": f"H股收盘 ({meta['h_currency']})",
                "fx_rate": meta.get("fx_pair") or "汇率",
                "premium_pct": "溢价率 (%)",
            }
        )
        st.dataframe(detail, width="stretch", hide_index=True)
