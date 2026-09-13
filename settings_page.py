"""「设置」页面: 基础货币 / 涨跌配色 / 数据源偏好, 全局生效并持久化到 settings.json."""
from __future__ import annotations

import streamlit as st

import app_settings as S

_COLOR_SAMPLE_PCT = "+2.35%"


def render_settings_page() -> None:
    """设置页: 即改即存 (on_change 回调落盘), 其余页面每次 rerun 读 settings.json."""
    settings = S.load_settings()
    st.markdown("### :material/settings: 设置")
    st.caption("设置保存到项目根目录 settings.json, 全部页面生效; 基础货币另存于 portfolio.json。")

    # ---- 显示 ----
    st.markdown("#### 显示")
    up_tag, down_tag = S.up_down_tags(settings)
    up_hex, down_hex = S.up_down_colors(settings)
    cur = f":{up_tag}[▲ {_COLOR_SAMPLE_PCT}] · :{down_tag}[▼ −1.20%]"
    st.markdown(f"当前配色: {cur}", help=f"涨 {up_hex} / 跌 {down_hex} · 与 K线图配色一致")
    st.radio(
        "涨跌配色",
        list(S.SCHEME_LABELS),
        format_func=lambda v: S.SCHEME_LABELS[v],
        index=0 if settings["color_scheme"] == S.SCHEME_CN else 1,
        key="set_color_scheme",
        on_change=_save_display,
        help="全局生效: K线图、多股对比、盈亏数字与自选状态颜色",
    )

    # ---- 数据源 ----
    st.markdown("#### 数据源")
    st.toggle(
        "A股/港股优先 akshare (国内网络)",
        value=settings["prefer_akshare"],
        key="set_prefer_akshare",
        on_change=_save_display,
        help="开启后港股数据源顺序翻转 (A股域本就固定 akshare 优先); 关闭时港股走 Yahoo",
    )
    st.toggle(
        "IBKR 行情 (需本机 IB Gateway)",
        value=settings["use_ibkr"],
        key="set_use_ibkr",
        on_change=_save_display,
        help="启用后优先从 IBKR 获取行情 (有订阅则为实时), 失败自动回退 Yahoo/akshare。"
        "连接参数见 ibkr.json (mode: paper=4002 / live=4001)",
    )

    # ---- 基础货币 ----
    st.markdown("#### 基础货币")
    portfolio = S.load_portfolio_file()
    saved_base = portfolio.get("base_currency", "CNY")
    st.selectbox(
        "基础货币",
        S.BASE_CURRENCIES,
        index=S.BASE_CURRENCIES.index(saved_base) if saved_base in S.BASE_CURRENCIES else 0,
        key="set_base_currency",
        on_change=_save_base,
        help="组合市值/盈亏的换算货币; CLI snapshot 默认也读取该值",
    )
    if saved_base not in S.BASE_CURRENCIES:
        st.warning(f"portfolio.json 中的基础货币 {saved_base} 不在常用列表, 保存后将被覆盖。")


def _save_display() -> None:
    """保存显示与数据源设置; 基础货币以外的改动只影响前端渲染."""
    S.save_settings(
        {
            "color_scheme": st.session_state.get("set_color_scheme", S.SCHEME_CN),
            "prefer_akshare": bool(st.session_state.get("set_prefer_akshare")),
            "use_ibkr": bool(st.session_state.get("set_use_ibkr")),
        }
    )
    st.toast("设置已保存")


def _save_base() -> None:
    """切换基础货币立即写入 portfolio.json (与原侧栏行为一致)."""
    new_base = st.session_state.get("set_base_currency", "CNY")
    data = S.load_portfolio_file()
    S.save_portfolio_file({"base_currency": new_base, "holdings": data.get("holdings", [])})
    st.toast(f"基础货币已保存为 {new_base}")
