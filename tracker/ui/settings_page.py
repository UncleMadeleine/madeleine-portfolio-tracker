"""「设置」页面: 基础货币 / 涨跌配色 / 数据源偏好, 全局生效并持久化到 settings.json."""

from __future__ import annotations

import streamlit as st

from . import settings as S
from tracker import eodhd as eodhd_mod
from tracker import storage
from tracker.storage import CorruptDataError, DataFileLocked

_COLOR_SAMPLE_PCT = "+2.35%"


def render_settings_page() -> None:
    """设置页: 即改即存 (on_change 回调落盘), 其余页面每次 rerun 读 settings.json."""
    settings = storage.load_settings()
    eodhd_cfg = eodhd_mod.load_config()
    eodhd_ok = eodhd_mod.available(eodhd_cfg)
    # 带 key 的 widget 一旦进入 session_state, 之后每次渲染都忽略 value=/index=,
    # 只用 session_state 里的值。若 settings.json 被别处改动 (另一标签页 / CLI /
    # 手改文件), 不同步就会显示旧值, 且 on_change 回调按 session_state 旧值写回,
    # 把别处的改动静默覆盖。这里在渲染前用最新 settings 校正 session_state:
    # 本会话 on_change 是「即改即存 + rerun」, 重读的 settings 已与 session_state
    # 一致 (同步为 no-op); 只有外部改动才会触发校正, 不会回退用户正在做的修改。
    for _k, _v in (
        ("set_color_scheme", settings["color_scheme"]),
        ("set_prefer_akshare", settings["prefer_akshare"]),
        ("set_use_ibkr", settings["use_ibkr"]),
        ("set_use_longport", bool(settings.get("use_longport"))),
        ("set_use_eodhd", bool(eodhd_cfg.get("enabled"))),
        ("set_crypto_source", settings["crypto_source"]),
    ):
        if st.session_state.get(_k) != _v:
            st.session_state[_k] = _v
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
    eod_hist_primary = eodhd_ok and eodhd_cfg["priority"] == "primary"
    eod_quote_primary = eodhd_ok and eodhd_cfg["quotes"] == "primary"
    eod_quote_fallback = eodhd_ok and eodhd_cfg["quotes"] == "fallback"
    if eod_quote_primary:
        global_main = front_txt + "EODHD → yfinance"
        global_fb = "港股 " + hk_order
    else:
        global_main = front_txt + ("EODHD → " if eod_hist_primary else "") + "yfinance"
        global_fb = "港股 " + hk_order + (" → EODHD" if eod_quote_fallback else "")
    st.caption(
        "当前生效链路 (失败自动落到下一源):  \n"
        + _chain("美股/港股/全球", global_main, global_fb)
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
    st.toggle(
        "EODHD 付费数据源 (BYOK)",
        value=bool(eodhd_cfg.get("enabled")),
        key="set_use_eodhd",
        on_change=_save_eodhd,
        help="需自备 EODHD API token (写入 eodhd.json 或 env TRACKER_EODHD_TOKEN)。"
        "默认用其历史K线 (在 yfinance 之前, IBKR/长桥之后), 实时行情仅在默认源失败时兜底;"
        "角色可在 eodhd.json 的 priority / quotes 中调整。未配置 token 时开关无效。",
    )
    if eodhd_ok:
        st.caption(
            f"EODHD 已就绪 (token {eodhd_mod.status().get('token_masked') or '已配置'}): "
            f"历史 {eodhd_cfg['priority']} / 行情 {eodhd_cfg['quotes']}; "
            "可用 `tracker eodhd test AAPL` 自检。"
        )
    else:
        st.caption(
            f"EODHD 未启用: {eodhd_mod.unavailable_reason(eodhd_cfg)}。"
            f"配置见 {eodhd_cfg['_path']} (或 `tracker eodhd status`)。"
        )
    with st.expander("EODHD token (BYOK)", expanded=False):
        st.caption(
            "token 只写入本地 eodhd.json (文件权限 0600), 不会上传到任何地方; "
            "也可用环境变量 TRACKER_EODHD_TOKEN 注入 (优先生效)。"
        )
        _tok = st.text_input(
            "API token",
            type="password",
            key="set_eodhd_token_input",
            placeholder="粘贴 token 后点击下方保存",
            help="EODHD Dashboard → API token; 免费套餐每天 20 次调用。",
        )
        _c1, _c2 = st.columns(2)
        if _c1.button("保存 token", key="set_eodhd_save"):
            if not str(_tok or "").strip():
                st.warning("token 为空: 如需清除请点「清除 token」。")
            else:
                try:
                    eodhd_mod.set_token(str(_tok))
                except OSError as e:
                    st.error(f"eodhd.json 写入失败: {e}")
                else:
                    _clear_source_caches()
                    st.toast("EODHD token 已保存")
                    st.rerun()
        if _c2.button("清除 token", key="set_eodhd_clear"):
            try:
                eodhd_mod.set_token("")
            except OSError as e:
                st.error(f"eodhd.json 写入失败: {e}")
            else:
                _clear_source_caches()
                st.toast("EODHD token 已清除")
                st.rerun()
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
    if st.session_state.get("set_base_currency") != saved_base:
        st.session_state["set_base_currency"] = saved_base
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


def _clear_source_caches() -> None:
    """数据源偏好/凭据变化: 清行情、汇率、K线磁盘缓存与进程内负缓存."""
    from tracker.cache import clear_fundamentals, clear_fx, clear_ohlc, clear_quotes

    clear_quotes()
    # EODHD fundamentals 角色/PB 数据源切换后, 6h 基本面缓存也要失效
    clear_fundamentals()
    # 汇率磁盘缓存同 key 不含数据源偏好: IBKR 开关变化后不清会继续用
    # CFETS/yfinance 旧汇率, 必须一并清
    clear_fx()
    # K线(OHLC) 磁盘缓存键只有 symbol+months、不含源偏好: 不清会命中上一个
    # 源的旧K线 (含跨会话/刷新后的首次查询), 必须一并清
    clear_ohlc()
    # 进程内负缓存只按代码记、不记源链路: 旧偏好下失败的代码会在 TTL 内
    # 继续挡住新链路 ("近期全部数据源失败 (负缓存)"), 必须一起清
    from tracker.providers.orchestration import neg_clear_all

    neg_clear_all()
    from tracker.ui.portfolio_page import cached_fx, cached_quotes

    cached_quotes.clear()
    # 汇率页面缓存 (st.cache_data) 同样要清: 磁盘汇率缓存已清, 不清这个
    # 的话 IBKR 开关切换后 10 分钟内仍返回旧源 (CFETS/yfinance) 汇率
    cached_fx.clear()


def _save_eodhd() -> None:
    """设置页 EODHD 开关: 只改 eodhd.json 的 enabled, 不碰 token."""
    enabled = bool(st.session_state.get("set_use_eodhd"))
    try:
        eodhd_mod.set_enabled(enabled)
    except OSError as e:
        st.error(f"eodhd.json 写入失败: {e}")
        return
    _clear_source_caches()
    if enabled and not eodhd_mod.status()["token_present"]:
        st.warning("EODHD 已启用, 但尚未配置 token —— 取数仍走默认源。")
    st.toast("EODHD 设置已保存")


def _save_display() -> None:
    """保存显示与数据源设置; 基础货币以外的改动只影响前端渲染."""
    old = storage.load_settings()
    source_keys = ("prefer_akshare", "use_ibkr", "use_longport", "crypto_source")
    crypto_source = str(st.session_state.get("set_crypto_source") or "auto")
    if crypto_source not in storage.CRYPTO_SOURCES:
        crypto_source = "auto"
    try:
        storage.save_settings(
            {
                "color_scheme": st.session_state.get("set_color_scheme", S.SCHEME_CN),
                "prefer_akshare": bool(st.session_state.get("set_prefer_akshare")),
                "use_ibkr": bool(st.session_state.get("set_use_ibkr")),
                "use_longport": bool(st.session_state.get("set_use_longport")),
                "crypto_source": crypto_source,
            }
        )
    except (DataFileLocked, CorruptDataError, OSError) as e:
        st.error(f"设置保存失败: {e}")
        return
    changed = {
        k: st.session_state.get(f"set_{k}", old.get(k)) for k in source_keys
    }
    if any(old.get(k) != changed[k] for k in source_keys):
        # 数据源偏好变化: 清缓存, 下次取数按新链路执行并重新打标来源;
        # 页面缓存 (st.cache_data) 由组合页「重载」或 TTL 兜底
        _clear_source_caches()
    st.toast("设置已保存")


def _save_base() -> None:
    """切换基础货币立即写入 portfolio.json (与原侧栏行为一致)."""
    new_base = st.session_state.get("set_base_currency", "CNY")
    try:
        with storage.edit_portfolio(storage.PORTFOLIO_PATH) as data:
            data["base_currency"] = new_base
    except (DataFileLocked, CorruptDataError, OSError) as e:
        st.error(f"基础货币保存失败: {e}")
        return
    st.toast(f"基础货币已保存为 {new_base}")
