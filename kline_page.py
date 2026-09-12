"""「K线」页面: 任意代码实时查询 (持仓/自选 + 自由输入).

K线与投资组合、自选同级的独立功能 —— 不依赖持仓/自选配置,
任何合法的 Yahoo 规范代码都能查。数据不预加载, 用户提交后实时拉取
(30 分钟磁盘缓存 + 10 分钟会话内存缓存, 切换参数不重复请求网络)。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from tracker import charting, prices, search
from tracker.symbols import parse

# 顶部快捷代码 (来自当前持仓与自选, 不预取任何行情数据, 仅展示代码名)
KLINE_SYMBOLS_KEY = "kline_quick_symbols"

# TradingView lightweight-charts 组件 (st.components.v2, JS 不过 DOMPurify):
# 拖动平移 / 滚轮·捏合缩放 / 触控板双指手势, 券商 App 通用交互.
_KLINE_CHART = st.components.v2.component(
    "lwc_kline",
    html=charting.KLINE_COMPONENT_HTML,
    css=charting.KLINE_COMPONENT_CSS,
    js=charting.kline_component_js(),
)


def quick_symbols() -> list[str]:
    """持仓+自选代码 (页面启动时注入, K线页自身不发起任何网络请求)."""
    return list(st.session_state.get(KLINE_SYMBOLS_KEY, []))


def set_quick_symbols(symbols: list[str]) -> None:
    st.session_state[KLINE_SYMBOLS_KEY] = list(dict.fromkeys(symbols))


@st.cache_data(ttl=1800, show_spinner=False)
def cached_kline(symbol: str, months: int, prefer_akshare: bool):
    """K线日线 (磁盘缓存 + 内存缓存双层, TTL 内切换参数不重复请求网络)."""
    return prices.get_ohlc(symbol, months=months, prefer_akshare=prefer_akshare)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_symbol_entries() -> list[dict]:
    """股票列表 (代码+名称+市场), 本地缓存 TTL 1 小时刷新一次, 避免每次输入请求网络."""
    return [e.__dict__ for e in search.load_symbol_list()]


def is_valid_symbol(s: str) -> bool:
    if not s or not s.strip():
        return False
    try:
        parse(s)
    except ValueError:
        return False
    return True


def normalize_or_none(s: str) -> str | None:
    try:
        return parse(s).yahoo
    except ValueError:
        return None


def render_kline_view(
    kdf: pd.DataFrame,
    symbol: str,
    *,
    period: str,
    mas: list[int],
    show_volume: bool,
    green_up: bool,
    currency: str | None,
    indicators: dict | None = None,
    preserve_range: dict | None = None,
) -> dict | None:
    """K线图 + 摘要指标 (供 K线页面与 CLI 内嵌使用, 纯渲染无取数).

    返回组件交互结果 (need_more / range), 供无限拖动扩展数据使用.
    """
    if period != "daily":
        kdf = charting.resample_ohlc(kdf, period)
    # lightweight-charts (TradingView 内核): 拖动平移 / 滚轮·捏合缩放 /
    # 触控板双指手势, 券商 App 通用交互; 内嵌 JS 引擎无外部依赖.
    result = _KLINE_CHART(
        key="kline_chart",
        data=charting.kline_payload(
            kdf, symbol, currency=currency, mas=tuple(mas),
            show_volume=show_volume, green_up=green_up, period=period,
            indicators=indicators,
            preserve_range=preserve_range,
        ),
        height=680,
    )
    s = charting.summarize_ohlc(kdf, tuple(mas))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "最新收盘" + (f" ({currency})" if currency else ""),
        f"{s['last_close']:,.3f}",
    )
    m2.metric(
        "区间涨跌",
        f"{s['change_pct']:+.2f}%" if s["change_pct"] is not None else "—",
        delta=None if s["change_pct"] is None else round(s["change_pct"], 2),
        delta_color="off" if s["change_pct"] is None else "inverse",  # 红涨绿跌
    )
    m3.metric(
        f"区间最高 ({s['period_high_date']})",
        f"{s['period_high']:,.3f}",
    )
    m4.metric(
        f"区间最低 ({s['period_low_date']})",
        f"{s['period_low']:,.3f}",
    )
    ma_txt = "  ".join(f"MA{n[2:]} {v:,.3f}" for n, v in s["ma"].items())
    if ma_txt:
        st.caption(f"均线: {ma_txt} · 数据源与行情一致, 日K缓存 30 分钟")
    return result


def render_compare_chart(data: dict, *, height: int = 560) -> None:
    """多股对比图: 复用 K线组件 (拖动平移/滚轮缩放/十字光标), data 由 compare_payload 生成."""
    _KLINE_CHART(key="compare_chart", data=data, height=height)


def _render_symbol_search() -> None:
    """搜索框: 输入代码/名称片段 → 模糊匹配本地缓存 → 下拉选择自动填入代码.

    本地缓存 TTL 1 小时 (cached_symbol_entries), 输入时不请求网络;
    选中结果通过 session_state 同步到代码输入框 (kline_symbol)。
    """
    sq = st.text_input(
        "搜索股票 (代码或名称)",
        value="",
        key="kline_search_query",
        placeholder="如: 苹果 · 茅台 · AAPL · 0700",
        help="输入代码或名称片段, 从下拉结果中选择即可自动填入代码框",
    )
    sq = (sq or "").strip()
    if not sq:
        return
    # 本地缓存匹配 (不请求网络), 降级时仅做代码格式校验
    entries = [search.SymbolEntry(**e) for e in cached_symbol_entries()]
    results = search.search_symbols(sq, limit=15, entries=entries)
    if not results:
        st.caption("无匹配结果 —— 可直接在下方代码框输入完整代码")
        return
    # 下拉选择: 展示「代码 · 名称 (市场)」, 选中后回填代码输入框
    options_list = [
        f"{r['code']} · {r['name']} ({r['market']})" for r in results
    ]

    def _on_pick():
        idx = st.session_state.get("kline_search_select")
        if isinstance(idx, int) and 0 <= idx < len(results):
            st.session_state["kline_symbol"] = results[idx]["code"]
            # 触发查询 (与手动回车等价)
            st.session_state["kline_last_symbol"] = None

    st.selectbox(
        f"匹配结果 ({len(results)} 条)",
        options_list,
        index=None,
        key="kline_search_select",
        on_change=_on_pick,
        placeholder="选择以填入代码...",
    )


def render_kline_controls(prefer_akshare: bool) -> None:
    """查询控件 + 拉取/渲染 (输入驱动: 无提交不取数)."""
    qs = quick_symbols()
    st.markdown("### :material/candlestick_chart: K线查询")

    # ---- 搜索框: 按代码或名称模糊匹配, 选中后自动填入代码输入框 ----
    _render_symbol_search()

    c1, c2, c3, c4 = st.columns([3, 1, 1, 1], vertical_alignment="bottom")
    ksym = c1.text_input(
        "代码",
        value=qs[0] if qs else "AAPL",
        key="kline_symbol",
        placeholder="如: AAPL · 600519.SS · 0700.HK · SAP.DE · BP.L · BTC-USD",
    )
    ksym = (ksym or "").strip().upper()
    kmonths = c2.selectbox(
        "范围", [3, 6, 12, 24, 36], index=2,
        format_func=lambda m: f"近 {m} 个月", key="kline_months",
    )
    kperiod = c3.selectbox(
        "周期", ["daily", "weekly", "monthly"], index=0,
        format_func=lambda v: charting.PERIOD_LABELS[v], key="kline_period",
    )
    # 回车提交: text_input 回车 rerun 时 value 已变, 据此标记为已提交
    entered = st.session_state.get("kline_last_symbol") != ksym
    if c4.button("查询", type="primary", icon=":material/search:"):
        entered = True

    opt1, opt2, opt3 = st.columns([1, 1, 2])
    kmas = opt1.multiselect(
        "均线", [5, 10, 20, 30, 60, 120, 250], default=[5, 20, 60], key="kline_ma",
    )
    kvol = opt2.toggle("成交量", value=True, key="kline_vol")
    kgreen = opt3.toggle("绿涨红跌 (国际配色)", value=False, key="kline_color")

    # 技术指标选择 (多选 + 可调参数)
    ind_sel = st.multiselect(
        "技术指标", ["MACD", "RSI", "KDJ", "布林带"], default=[], key="kline_indicators",
    )
    indicators: dict = {}
    if ind_sel:
        ic1, ic2, ic3, ic4 = st.columns(4)
        if "RSI" in ind_sel:
            rsi_period = ic1.slider("RSI 周期", 2, 30, 14, key="kline_rsi_period")
            indicators["rsi"] = {"period": rsi_period}
        if "布林带" in ind_sel:
            boll_period = ic2.slider("布林带周期", 5, 60, 20, key="kline_boll_period")
            boll_std = ic3.slider("标准差倍数", 1.0, 4.0, 2.0, 0.5, key="kline_boll_std")
            indicators["boll"] = {"period": boll_period, "std": boll_std}
        if "MACD" in ind_sel:
            indicators["macd"] = {}  # 使用默认参数 12/26/9
        if "KDJ" in ind_sel:
            indicators["kdj"] = {}  # 使用默认参数 9/3/3

    st.caption(
        "代码规范: 美股 AAPL · A股 600519.SS · 港股 0700.HK · 德股 SAP.DE · "
        "英股 BP.L · 加股 RY.TO · 澳股 BHP.AX · 加密货币 BTC-USD"
    )
    def _pick_quick():
        v = st.session_state.get("kline_quick")
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if v:
            # 回调在 widget 实例化前运行, 可合法写 key → 输入框同步显示
            st.session_state["kline_symbol"] = str(v).strip().upper()

    if qs:
        # on_change 仅在 pill 真正被点击时触发 (状态保持的旧值不会重复触发),
        # 回调负责把选中代码同步进输入框; 查询一律以输入框的值为准,
        # 避免残留的 pill 选中项覆盖用户手动输入的代码。
        st.pills("常用 (持仓/自选)", qs, key="kline_quick", on_change=_pick_quick)

    if not ksym:
        st.info("输入代码后回车或点「查询」获取数据 —— 页面启动不会预加载任何 K线。")
        return
    if not is_valid_symbol(ksym):
        st.error(f"无法识别的代码: {ksym} (参考上方代码规范, 如 600519.SS / 0700.HK)")
        return
    if not entered and not st.session_state.get("kline_submitted"):
        st.info("回车或点「查询」获取 K线。")
        return

    st.session_state["kline_submitted"] = True
    st.session_state["kline_last_symbol"] = ksym

    yahoo = normalize_or_none(ksym)

    # ---- 无限拖动: 维护全量数据集 (session_state), 组件返回 need_more 时自动扩展 ----
    if (
        entered
        or st.session_state.get("kline_current_symbol") != yahoo
        or st.session_state.get("kline_last_months") != kmonths
    ):
        st.session_state["kline_full_df"] = None
        st.session_state["kline_preserve_range"] = None
        st.session_state["kline_last_range"] = None
        st.session_state["kline_fetching"] = False

    st.session_state["kline_current_symbol"] = yahoo
    st.session_state["kline_last_months"] = kmonths

    if st.session_state["kline_full_df"] is None:
        try:
            with st.spinner(f"拉取 {yahoo} K线..."):
                kdf = cached_kline(yahoo, kmonths, prefer_akshare)
        except Exception as e:
            st.warning(f"{yahoo}: {e}")
            return
        if kdf.empty:
            st.warning(f"{yahoo}: 无有效K线数据。")
            return
        st.session_state["kline_full_df"] = kdf

    full_df = st.session_state["kline_full_df"]
    preserve_range = st.session_state.get("kline_preserve_range")
    st.session_state["kline_preserve_range"] = None

    try:
        kcur = parse(yahoo).currency
    except ValueError:
        kcur = None

    result = render_kline_view(
        full_df, yahoo, period=kperiod, mas=kmas,
        show_volume=kvol, green_up=kgreen, currency=kcur,
        indicators=indicators or None,
        preserve_range=preserve_range,
    )

    # 处理无限拖动扩展数据
    if result and isinstance(result, dict) and result.get("need_more"):
        old_range = result.get("range", {})
        before_date = result.get("before")
        if before_date and not st.session_state.get("kline_fetching"):
            last_before = st.session_state.get("kline_last_before")
            if last_before == before_date:
                st.session_state["kline_preserve_range"] = None
                return
            st.session_state["kline_fetching"] = True
            st.session_state["kline_last_range"] = old_range
            st.session_state["kline_last_before"] = before_date
            try:
                before_dt = datetime.strptime(before_date, "%Y-%m-%d")
                fetch_start = (before_dt - timedelta(days=365)).strftime("%Y-%m-%d")
                with st.spinner(f"正在加载更早数据 ({fetch_start} -> {before_date})..."):
                    older_df = prices.get_ohlc(
                        yahoo,
                        start_date=fetch_start,
                        end_date=before_date,
                        prefer_akshare=prefer_akshare,
                    )
                if not older_df.empty:
                    combined = pd.concat([older_df, full_df])
                    combined = combined.drop_duplicates(subset="date", keep="first")
                    combined = combined.sort_values("date").reset_index(drop=True)
                    st.session_state["kline_full_df"] = combined
                    shift = len(older_df)
                    total_bars = len(combined)
                    old_from = old_range.get("from", 0)
                    old_to = old_range.get("to", 0)
                    preserve_from = max(old_from + shift, int(total_bars * 0.3))
                    preserve_to = preserve_from + (old_to - old_from)
                    st.session_state["kline_preserve_range"] = {"from": preserve_from, "to": preserve_to}
            except Exception as e:
                st.warning(f"扩展历史数据失败: {e}")
            finally:
                st.session_state["kline_fetching"] = False
                st.rerun()
