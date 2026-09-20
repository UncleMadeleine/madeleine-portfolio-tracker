"""「指数K线」页面: 宏观/风险指数查询 (IX.<KEY>), 与股票 K线页完全独立.

复用 K线图表组件与渲染函数; 数据走 prices.get_index_history (provider 源链:
中国指数 akshare 优先, 其余 yfinance 主源), 缓存键 index: 前缀隔离。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from . import settings
from .kline_page import render_kline_view

from tracker import charting, prices
from tracker.symbols import INDEX_CATALOG, index_label


@st.cache_data(ttl=1800, show_spinner=False)
def cached_index_kline(symbol: str, months: int):
    """指数日线 (磁盘缓存 + 内存缓存双层, TTL 内切换参数不重复请求网络)."""
    return prices.get_index_history(symbol, months=months)


def render_index_page() -> None:
    """指数K线查询页: 分组选择指数 → 切换即拉取渲染 (无按钮, 选择驱动)."""
    st.markdown("### :material/insights: 指数K线")

    groups = {
        "风险/波动": ["VIX", "VIX3M", "MOVE"],
        "美元/利率": ["DXY", "US10Y", "US02Y"],
        "美股": ["SPX", "NDX", "DJI", "RUT"],
        "全球": ["DAX", "FTSE", "N225", "HSI"],
        "中国": ["CSI300", "CSI500", "CSI1000", "SSE", "SZSE", "CYB", "KECHUANG50"],
    }
    labels = {}
    for g, keys in groups.items():
        for k in keys:
            if k in INDEX_CATALOG:
                labels[f"{INDEX_CATALOG[k]['name']} ({g})"] = k
    extra = [k for k in INDEX_CATALOG if k not in labels.values()]
    for k in extra:
        labels[index_label(k)] = k

    sel_col, r1_col, r2_col = st.columns([3, 1, 1])
    chosen = sel_col.selectbox(
        "指数",
        list(labels.keys()),
        index=list(labels.keys()).index("美元指数 (美元/利率)") if "美元指数 (美元/利率)" in labels else 0,
        key="index_symbol",
    )
    imonths = r1_col.selectbox(
        "范围", [3, 6, 12, 24, 36], index=2,
        format_func=lambda m: f"近 {m} 个月", key="index_months",
    )
    iperiod = r2_col.selectbox(
        "周期", ["daily", "weekly", "monthly"], index=0,
        format_func=lambda v: charting.PERIOD_LABELS[v], key="index_period",
    )
    imas = st.multiselect(
        "均线", [5, 10, 20, 30, 60, 120, 250], default=[5, 20, 60], key="index_ma",
    )

    st.caption(
        "指数代码规范 IX.<KEY> (CLI: tracker index-kline IX.<KEY>); "
        "指数无成交量语义, 成交量副图自动省略。涨跌配色跟随「设置」页全局方案。"
    )

    key = labels[chosen]
    yahoo = f"IX.{key}"
    st.session_state["index_current_symbol"] = yahoo

    try:
        with st.spinner(f"拉取 {index_label(key)} K线..."):
            kdf = cached_index_kline(yahoo, imonths)
    except Exception as e:
        st.warning(f"{yahoo}: {e}")
        return
    if not isinstance(kdf, pd.DataFrame) or kdf.empty:
        st.warning(f"{yahoo}: 无有效K线数据。")
        return

    render_kline_view(
        kdf, f"{index_label(key)} ({yahoo})", period=iperiod, mas=imas,
        show_volume=False, green_up=settings.green_up(),
        currency=None,  # 指数以点数/%计价, 不标货币
        has_more=False,  # 指数页固定范围, 不做无限拖动扩展
    )
