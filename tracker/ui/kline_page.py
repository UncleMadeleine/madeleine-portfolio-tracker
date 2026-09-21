"""「K线」页面: 任意代码实时查询 (持仓/自选 + 自由输入).

两种查询模式:
- 滑动 (默认): 一次加载上市以来全量历史, 图表内连续拖动 / 缩放全程纯前端。
- 范围: 先选范围再点「查询」的旧流程 (兜底, 与滑动模式同代码不同数据路径)。
"""
from __future__ import annotations


import pandas as pd
import streamlit as st

from . import settings

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
def cached_kline_all(symbol: str, prefer_akshare: bool):
    """上市以来全量日K (滑动模式唯一取数路径, 缓存键独立于范围模式月份参数)."""
    return prices.get_ohlc(symbol, months=1200, prefer_akshare=prefer_akshare)



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
    query_id: str | None = None,
) -> dict | None:
    """K线图 + 摘要指标 (供 K线页面与 CLI 内嵌使用, 纯渲染无取数)."""
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
            query_id=query_id,
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
        delta_color=(
            "off" if s["change_pct"] is None
            else ("normal" if green_up else "inverse")  # 跟随全局涨跌配色
        ),
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


def render_compare_controls(prefer_akshare: bool, quick_symbols: list[str]) -> None:
    """多股走势对比 (K线子功能): 任意代码同坐标系折线对比, 输入驱动无提交也拉取.

    代码候选 = 持仓/自选 (quick_symbols) + 自由输入; 数据复用 cached_kline。
    """
    chart_symbols = list(dict.fromkeys(quick_symbols))
    c1, c2, c3, c4 = st.columns([4, 1, 1, 1], vertical_alignment="bottom")
    sel_raw = c1.multiselect(
        "对比代码",
        chart_symbols,
        default=chart_symbols[:2],
        accept_new_options=True,
        key="compare_sel",
        placeholder="选择持仓/自选, 或直接输入任意代码 (如 NVDA)",
    )
    cmp_months = c2.selectbox(
        "范围", [3, 6, 12, 24, 36], index=2,
        format_func=lambda m: f"近 {m} 个月", key="compare_months",
    )
    cmp_period = c3.selectbox(
        "周期", ["daily", "weekly", "monthly"], index=0,
        format_func=lambda v: charting.PERIOD_LABELS[v], key="compare_period",
    )
    norm = c4.toggle("归一化 (起点=100)", value=True, key="compare_norm")

    sel, bad = [], []
    for s in sel_raw:
        y = normalize_or_none(s)
        (sel if y is not None else bad).append(y if y is not None else s)
    sel = list(dict.fromkeys(sel))
    if bad:
        st.error(f"无法识别: {', '.join(bad)}")
    if not sel:
        st.info("选择持仓/自选代码, 或直接输入任意代码 (如 NVDA · 600519.SS) 开始对比。")
        return
    frames = {}
    with st.spinner(f"拉取 {len(sel)} 只代码近 {cmp_months} 个月 K线..."):
        for s in sel:
            try:
                d = cached_kline(s, cmp_months, prefer_akshare)
                if d.empty:
                    st.warning(f"{s}: 无有效K线数据")
                else:
                    frames[s] = d
            except Exception as e:
                st.warning(f"{s}: {e}")
    if frames:
        render_compare_chart(
            charting.compare_payload(
                frames, normalize=norm, period=cmp_period,
                green_up=settings.green_up(),
            ),
            height=560,
        )
        chg = []
        for sym, d in frames.items():
            dd = charting.resample_ohlc(d, cmp_period) if cmp_period != "daily" else d
            if len(dd) >= 2:
                pct = float(dd["close"].iloc[-1]) / float(dd["close"].iloc[0]) - 1
                up_tag, down_tag = settings.up_down_tags()
                chg.append(
                    f"{sym} :{up_tag}[{pct:+.2%}]" if pct >= 0 else f"{sym} :{down_tag}[{pct:+.2%}]"
                )
        if chg:
            st.markdown("区间涨跌: " + " · ".join(chg))
        if norm:
            st.caption(
                "各代码按自身区间首个收盘归一化 (=100); 不同市场按各自交易日绘制, "
                "拖动平移 / 滚轮缩放, 悬停查看当日各代码取值。"
            )


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
    # selectbox 在 session_state 里存的是选项值 (字符串), 不是索引 → 用值反查代码
    code_by_option = {opt: r["code"] for opt, r in zip(options_list, results)}

    def _on_pick():
        code = code_by_option.get(st.session_state.get("kline_search_select"))
        if code:
            st.session_state["kline_symbol"] = code
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




def _render_slide_mode(
    yahoo: str,
    kperiod: str,
    kmas: list[int],
    kvol: bool,
    kgreen: bool,
    indicators: dict,
    prefer_akshare: bool,
) -> None:
    """滑动模式: 一次拉取上市以来全量历史, 图表内无限拖动 (纯前端, 不再取数).

    - 数据集存 session_state (kline_slide_df); 周期/均线/指标/成交量变化只重绘。
    """
    state_key = "kline_slide_df"
    state_qid_key = "kline_slide_qid"

    qid = yahoo
    qid_changed = st.session_state.get(state_qid_key) != qid
    if qid_changed:
        st.session_state[state_key] = None
        st.session_state[state_qid_key] = qid

    df_all: pd.DataFrame | None = st.session_state.get(state_key)
    if df_all is None:
        try:
            with st.spinner(f"拉取 {yahoo} 上市以来K线..."):
                kdf = cached_kline_all(yahoo, prefer_akshare)
        except Exception as e:
            st.warning(f"{yahoo}: {e}")
            return
        if not isinstance(kdf, pd.DataFrame) or kdf.empty:
            st.warning(f"{yahoo}: 无有效K线数据。")
            return
        st.session_state[state_key] = kdf
        st.session_state["kline_queried"] = True
        df_all = kdf

    try:
        kcur = parse(yahoo).currency
    except ValueError:
        kcur = None

    render_kline_view(
        df_all, yahoo, period=kperiod, mas=kmas,
        show_volume=kvol, green_up=kgreen, currency=kcur,
        indicators=indicators or None,
        query_id=qid,
    )




def render_kline_controls(prefer_akshare: bool) -> None:
    """查询控件 + 拉取/渲染 (滑动 / 范围双模式, 输入驱动)."""
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
    kmode = st.segmented_control(
        "查询模式", ["滑动", "范围"], default="滑动", key="kline_mode",
        help="滑动: 一次加载上市以来全量历史, 图表内连续拖动/缩放; "
        "范围: 先选范围再查询 (兜底模式)",
    )
    ksym = (ksym or "").strip().upper()
    if kmode == "滑动":
        kdepth = None  # 滑动模式固定上市以来, 无范围选择
    else:
        kdepth = c2.selectbox(
            "范围", [3, 6, 12, 24, 36], index=2,
            format_func=lambda m: f"近 {m} 个月", key="kline_months",
        )
    kperiod = c3.selectbox(
        "周期", ["daily", "weekly", "monthly"], index=0,
        format_func=lambda v: charting.PERIOD_LABELS[v], key="kline_period",
    )
    # 范围模式提交判定: 代码变化 (回车/快捷 pill/搜索选择) 或点「查询」;
    # 首次渲染只记录输入框当前值, 不视为提交 (页面启动不预加载任何 K线)
    if "kline_last_symbol" not in st.session_state:
        st.session_state["kline_last_symbol"] = ksym
    submitted = st.session_state["kline_last_symbol"] != ksym
    if c4.button("查询", type="primary", icon=":material/search:"):
        submitted = True

    opt1, opt2 = st.columns([1, 1])
    kmas = opt1.multiselect(
        "均线", [5, 10, 20, 30, 60, 120, 250], default=[5, 20, 60], key="kline_ma",
    )
    kvol = opt2.toggle("成交量", value=True, key="kline_vol")
    kgreen = settings.green_up()  # 涨跌配色全局统一, 在「设置」页切换

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
        st.info("输入代码后回车获取数据 —— 页面启动不会预加载任何 K线。")
        return
    if not is_valid_symbol(ksym):
        st.error(f"无法识别的代码: {ksym} (参考上方代码规范, 如 600519.SS / 0700.HK)")
        return
    yahoo = normalize_or_none(ksym)
    if yahoo is None:
        return

    if kmode == "滑动":
        _render_slide_mode(
            yahoo, kperiod, kmas, kvol, kgreen, indicators, prefer_akshare,
        )
        return
    # ---- 范围模式 (旧流程兜底) ----
    submitted = submitted or st.session_state.get("kline_queried") is True
    if not submitted:
        st.info("输入代码后回车或点「查询」获取数据 —— 页面启动不会预加载任何 K线。")
        return
    st.session_state["kline_last_symbol"] = ksym
    st.session_state["kline_queried"] = True
    # 范围模式: 代码/范围变化才重新取数; 其余参数变化只重绘。
    data_changed = (
        st.session_state.get("kline_current_symbol") != yahoo
        or st.session_state.get("kline_last_months") != kdepth
    )
    if data_changed or st.session_state.get("kline_current_df") is None:
        try:
            with st.spinner(f"拉取 {yahoo} K线..."):
                kdf = cached_kline(yahoo, kdepth, prefer_akshare)
        except Exception as e:
            st.warning(f"{yahoo}: {e}")
            return
        if kdf.empty:
            st.warning(f"{yahoo}: 无有效K线数据。")
            return
        st.session_state["kline_current_df"] = kdf
        st.session_state["kline_current_symbol"] = yahoo
        st.session_state["kline_last_months"] = kdepth
    kdf = st.session_state["kline_current_df"]
    try:
        kcur = parse(yahoo).currency
    except ValueError:
        kcur = None
    render_kline_view(
        kdf, yahoo, period=kperiod, mas=kmas,
        show_volume=kvol, green_up=kgreen, currency=kcur,
        indicators=indicators or None,
    )


def render_kline_page(prefer_akshare: bool) -> None:
    """「K线」页入口: 单只查询 + 多股对比 两个子功能 (st.tabs)."""
    tab_single, tab_compare = st.tabs(
        [":material/candlestick_chart: 单只查询", ":material/show_chart: 走势对比"]
    )
    with tab_compare:
        render_compare_controls(prefer_akshare, quick_symbols())
    with tab_single:
        render_kline_controls(prefer_akshare)
