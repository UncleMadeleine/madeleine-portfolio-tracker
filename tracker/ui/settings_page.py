"""「设置」页面: 基础货币 / 涨跌配色 / 数据源偏好, 全局生效并持久化到 settings.json."""

from __future__ import annotations

import streamlit as st

from . import settings as S
from tracker import storage

_COLOR_SAMPLE_PCT = "+2.35%"


def render_settings_page() -> None:
    """设置页: 即改即存 (on_change 回调落盘), 其余页面每次 rerun 读 settings.json."""
    settings = storage.load_settings()
    st.markdown("### :material/settings: 设置")
    st.caption(
        "设置保存到项目根目录 settings.json, 全部页面生效; 基础货币另存于 portfolio.json。"
    )

    # ---- 显示 ----
    st.markdown("#### 显示")
    up_tag, down_tag = S.up_down_tags(settings)
    up_hex, down_hex = S.up_down_colors(settings)
    cur = f":{up_tag}[▲ {_COLOR_SAMPLE_PCT}] · :{down_tag}[▼ −1.20%]"
    st.markdown(
        f"当前配色: {cur}", help=f"涨 {up_hex} / 跌 {down_hex} · 与 K线图配色一致"
    )
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

    def _chain(domain: str, main: str, fallback: str = "") -> str:
        parts = [main] + ([fallback] if fallback else [])
        return f"**{domain}**: " + " → ".join(parts)

    if settings["use_longport"] or settings["use_ibkr"]:
        front = []
        if settings["use_longport"]:
            front.append("长桥")
        if settings["use_ibkr"]:
            front.append("IBKR")
        front_txt = " → ".join(front) + " → "
    else:
        front_txt = ""
    hk_order = (
        "akshare → yfinance" if settings["prefer_akshare"] else "yfinance → akshare"
    )
    crypto_order = S.CRYPTO_SOURCE_CHAINS[settings["crypto_source"]]
    st.caption(
        "当前生效链路 (失败自动落到下一源):  \n"
        + _chain("美股/港股/全球", front_txt + "yfinance", "港股 " + hk_order)
        + "  \n"
        + _chain("A股/北交所", "akshare (东财)", "yfinance")
        + "  \n"
        + _chain("加密货币", crypto_order)
        + "  \n"
        + _chain("汇率", "IBKR" if settings["use_ibkr"] else "CFETS", "yfinance")
    )
    st.caption(
        "每个代码实际命中的源见「组合」页持仓/自选表的**数据来源**列; "
        "实时行情缓存 5 分钟, 下方开关切换时自动清空以立即生效。"
    )
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
    st.toggle(
        "长桥行情 (需长桥账户)",
        value=bool(settings.get("use_longport")),
        key="set_use_longport",
        on_change=_save_display,
        help="启用后优先从长桥 OpenAPI 获取美股/港股/沪深行情与历史K线, 失败自动回退默认源。"
        "需先登录: 在「导入」页长桥标签完成 OAuth 授权; 连接配置见 longport.json。",
    )
    st.selectbox(
        "加密货币数据源优先",
        list(S.CRYPTO_SOURCE_LABELS),
        format_func=lambda v: S.CRYPTO_SOURCE_LABELS[v],
        index=list(S.CRYPTO_SOURCE_LABELS).index(settings["crypto_source"]),
        key="set_crypto_source",
        on_change=_save_display,
        help="只影响加密货币代码 (BTC-USD / ETH-USDT): 指定源前置, 失败自动落到链上其余源。"
        "Hyperliquid 为永续合约价 (仅 USD 系计价, 与现货有基差)。",
    )
    with st.expander(
        "长桥账户登录",
        expanded=bool(settings.get("use_longport")) and not settings.get("use_ibkr"),
    ):
        from .longport_login import login_state_label, render_login_section

        st.caption(f"当前状态: {login_state_label()}")
        render_login_section(key_prefix="set_lp")

    # ---- 基础货币 ----
    st.markdown("#### 基础货币")
    portfolio = storage.load_portfolio(storage.PORTFOLIO_PATH)
    saved_base = portfolio.get("base_currency", "CNY")
    st.selectbox(
        "基础货币",
        S.BASE_CURRENCIES,
        index=S.BASE_CURRENCIES.index(saved_base)
        if saved_base in S.BASE_CURRENCIES
        else 0,
        key="set_base_currency",
        on_change=_save_base,
        help="组合市值/盈亏的换算货币; CLI snapshot 默认也读取该值",
    )
    if saved_base not in S.BASE_CURRENCIES:
        st.warning(
            f"portfolio.json 中的基础货币 {saved_base} 不在常用列表, 保存后将被覆盖。"
        )


def _save_display() -> None:
    """保存显示与数据源设置; 基础货币以外的改动只影响前端渲染."""
    old = storage.load_settings()
    source_keys = ("prefer_akshare", "use_ibkr", "use_longport", "crypto_source")
    crypto_source = str(st.session_state.get("set_crypto_source") or "auto")
    if crypto_source not in storage.CRYPTO_SOURCES:
        crypto_source = "auto"
    storage.save_settings(
        {
            "color_scheme": st.session_state.get("set_color_scheme", S.SCHEME_CN),
            "prefer_akshare": bool(st.session_state.get("set_prefer_akshare")),
            "use_ibkr": bool(st.session_state.get("set_use_ibkr")),
            "use_longport": bool(st.session_state.get("set_use_longport")),
            "crypto_source": crypto_source,
        }
    )
    changed = {
        k: st.session_state.get(f"set_{k}", old.get(k)) for k in source_keys
    }
    if any(old.get(k) != changed[k] for k in source_keys):
        # 数据源偏好变化: 清实时行情缓存 (保留K线/基本面), 下次取数按新链路执行
        # 并重新打标来源; 页面缓存 (st.cache_data) 由组合页「重载」或 TTL 兜底
        from tracker.cache import clear_quotes

        clear_quotes()
        from tracker.ui.portfolio_page import cached_quotes

        cached_quotes.clear()
    st.toast("设置已保存")


def _save_base() -> None:
    """切换基础货币立即写入 portfolio.json (与原侧栏行为一致)."""
    new_base = st.session_state.get("set_base_currency", "CNY")
    data = storage.load_portfolio(storage.PORTFOLIO_PATH)
    storage.save_portfolio(
        {"base_currency": new_base, "holdings": data.get("holdings", [])}
    )
    st.toast(f"基础货币已保存为 {new_base}")
