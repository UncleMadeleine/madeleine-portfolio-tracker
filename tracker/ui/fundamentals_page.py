"""「基本面分析」页面: 股票估值指标 (PE / PB / 股息率) + A+H 股票 AH 溢价.

两个子视图用 `segmented_control` 懒加载, 不用 `st.tabs`: st.tabs 会把所有
tab body 都渲染 (仅用 CSS 隐藏), 打开页面就会同时拉估值快照、A/H 历史与
汇率。分段控件只实例化当前子视图, 未激活的控件不发请求。

- 估值指标: 任意股票的最新 PE(TTM)/PB/股息率快照, 以及 A股/港股/美股
  的历史 PE/PB 曲线 (百度股市通)。数据口径见 tracker.valuation。
- AH 溢价: 保留原有单只 A+H 股票能力。A 股收盘 ÷ (H 股收盘 × HKD→CNY
  汇率) − 1, 以 A 股交易日为时间轴, H 股/汇率取当日或之前最近收盘。
  曲线用与 K线页相同的 lightweight-charts 组件, 0% 处画同价参考线。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from . import settings, theme

from tracker import ah_premium, charting, valuation
from tracker.symbols import Market, normalize, parse

# 与 K线页共用组件实现, 但用独立注册名: 同一 Streamlit 进程内不同组件实例
# 各自持有 DOM/视口状态, 避免与 K线页的图表互相影响。
_LINE_CHART = st.components.v2.component(
    "lwc_fundamentals_line",
    html=charting.KLINE_COMPONENT_HTML,
    css=charting.KLINE_COMPONENT_CSS,
    js=charting.kline_component_js(),
)

_MONTHS = [3, 6, 12, 24, 36, 60]
_VIEW_VALUATION = "估值指标"
_VIEW_AH = "AH 溢价"
_VIEW_KEY = "fundamentals_view"


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


@st.cache_data(ttl=21600, show_spinner=False)
def cached_valuation(symbol: str, refresh: bool = False):
    """最新估值快照 (PE/PB/股息率; 底层模块另有 6h 进程内缓存)."""
    return valuation.fetch_snapshot(symbol, refresh=refresh)


@st.cache_data(ttl=21600, show_spinner=False)
def cached_valuation_history(
    symbol: str,
    metric: str,
    months: int,
    refresh: bool = False,
):
    """历史 PE/PB 曲线 (底层 series_cache 6h + 百度接口)."""
    return valuation.fetch_history(
        symbol,
        metric=metric,
        months=months,
        refresh=refresh,
    )


def clear_ah_caches() -> None:
    """强制刷新 AH 溢价: 清页面缓存, 并让底层取数穿透磁盘/汇率缓存."""
    cached_ah_pairs.clear()
    cached_ah_premium.clear()
    # list_pairs() 在 st.cache_data 之外还有 1h 的模块级缓存; 不清掉的话
    # 上面的 cached_ah_pairs.clear() 只会重新命中旧目录, 强制刷新失效。
    ah_premium.clear_pair_cache()


def clear_valuation_caches() -> None:
    """强制刷新估值指标: 清页面缓存与最新快照的进程内缓存."""
    cached_valuation.clear()
    cached_valuation_history.clear()
    valuation.clear_cache()


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


def _fmt_ratio(value: float | None) -> str:
    """PE/PB 倍数: 无意义/缺失统一显示破折号 (不把 None 显示成 0)."""
    return "—" if value is None else f"{float(value):.2f}"


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.2f}%"


def _fmt_snapshot_time(ts: float) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def _normalize_valuation_input(raw: str) -> str:
    """宽松代码输入 → Yahoo 规范代码.

    支持 601398 (补 .SS) / 1398 (补 .HK) 这类纯数字简写; 带后缀或美股
    ticker 走统一 normalize。空输入/畸形代码抛 ValueError。
    """
    s = str(raw or "").strip()
    if not s:
        raise ValueError("股票代码不能为空")
    if "." not in s and s.isdigit():
        if len(s) == 6:
            symbol = ah_premium.normalize_a_symbol(s)
        else:
            symbol = ah_premium.normalize_h_symbol(s)
    else:
        symbol = normalize(s)
    p = parse(symbol)
    if p.market in (Market.CRYPTO, Market.INDEX):
        raise ValueError(f"{p.yahoo}: 估值指标仅支持股票, 不支持 {p.market_label}")
    return p.yahoo


def render_fundamentals_page() -> None:
    """「基本面分析」页入口: 估值指标 / AH 溢价 二选一."""
    theme.page_header("基本面分析", eyebrow="FUNDAMENTALS")
    st.caption(
        "估值指标: PE(TTM)、PB、股息率与历史 PE/PB 分位; "
        "A+H 股票可在「AH 溢价」查看两地价差。"
    )

    st.session_state.setdefault(_VIEW_KEY, _VIEW_VALUATION)
    view = st.segmented_control(
        "基本面功能",
        [_VIEW_VALUATION, _VIEW_AH],
        key=_VIEW_KEY,
        label_visibility="collapsed",
    )
    if view == _VIEW_AH:
        _render_ah_premium()
    else:
        _render_valuation()


# ---------- 估值指标子视图 ----------


def _render_valuation_metrics(snap: valuation.ValuationSnapshot) -> None:
    """最新估值卡片 + 口径说明."""
    with st.container(horizontal=True):
        st.metric(
            "PE (TTM)",
            _fmt_ratio(snap.pe_ttm),
            help="滚动市盈率 = 股价 ÷ 最近 12 个月每股收益; 亏损时无意义显示 —",
        )
        st.metric(
            "PB",
            _fmt_ratio(snap.pb),
            help="市净率 = 股价 ÷ 每股净资产; 负净资产时无意义显示 —",
        )
        st.metric(
            "股息率",
            _fmt_pct(snap.dividend_yield_pct),
            help="最近 12 个月每股分红 ÷ 股价; 不分红显示 0.00%",
        )
        st.metric(
            "5 年平均股息率",
            _fmt_pct(snap.dividend_yield_5y_avg_pct),
            help="数据源提供的近 5 年平均股息率, 用于判断当前股息率高低",
        )

    bits = [f"{snap.market_label} · {snap.currency}"]
    if snap.forward_pe is not None:
        bits.append(f"Forward PE {_fmt_ratio(snap.forward_pe)}")
    if snap.eps_ttm is not None:
        bits.append(f"EPS(TTM) {_fmt_price(snap.eps_ttm)}")
    if snap.book_value is not None:
        bits.append(f"BVPS {_fmt_price(snap.book_value)}")
    if snap.payout_ratio_pct is not None:
        bits.append(f"派息率 {_fmt_pct(snap.payout_ratio_pct)}")
    st.caption(" · ".join(bits))
    st.caption(
        f"快照时间 {_fmt_snapshot_time(snap.fetched_at)} · 数据源 {snap.source} "
        "(OpenBB)。PE/PB 为负 (亏损/负净资产) 时按「无意义」显示 —。"
    )


def _render_valuation_history(symbol: str, months: int, force: bool) -> None:
    """历史 PE/PB 曲线 + 区间摘要 + 明细."""
    labels = {v: k for k, v in valuation.METRIC_LABELS.items()}
    label = st.segmented_control(
        "历史估值指标",
        list(labels),
        default=valuation.METRIC_LABELS[valuation.METRIC_PE],
        key="valuation_metric",
    )
    metric = labels.get(label or "", valuation.METRIC_PE)
    metric_label = valuation.METRIC_LABELS[metric]

    if not valuation.history_supported(symbol):
        st.info(
            "历史 PE/PB 曲线目前支持 A 股 / 港股 / 美股; "
            "当前快照仍可用于 yfinance 覆盖的其它股票。"
        )
        return

    try:
        with st.spinner(f"拉取 {symbol} 近 {months} 个月 {metric_label}..."):
            hist = cached_valuation_history(
                symbol,
                metric,
                months,
                refresh=force,
            )
    except Exception as e:  # noqa: BLE001 - 历史曲线失败只提示, 不影响快照
        st.warning(f"{symbol}: {metric_label} 历史取数失败 ({e})")
        return

    if not isinstance(hist, pd.DataFrame) or hist.empty:
        st.warning(f"{symbol}: 无有效 {metric_label} 历史数据。")
        return

    payload = charting.line_payload(
        hist,
        value_col="value",
        title=f"{symbol} · {metric_label} · 近 {months} 个月",
        series_name=metric_label,
        precision=2,
        unit="x",
        green_up=settings.green_up(),
        height=520,
        init_bars=max(1, len(hist)),
        query_id=f"{symbol}|{metric}|{months}",
    )
    _LINE_CHART(key="valuation_chart", data=payload, height=520)

    summary = valuation.summarize_history(hist)
    with st.container(horizontal=True):
        st.metric("最新", _fmt_ratio(summary["latest"]))
        st.metric("区间均值", _fmt_ratio(summary["mean"]))
        st.metric(
            f"区间最高 ({summary['high_date']})",
            _fmt_ratio(summary["high"]),
        )
        st.metric(
            f"区间最低 ({summary['low_date']})",
            _fmt_ratio(summary["low"]),
        )

    st.caption(
        f"区间 {summary['first_date']} ~ {summary['last_date']} "
        f"({summary['bars']} 个交易日)，当前 {metric_label} 处于区间 "
        f"{summary['percentile']:.0f}% 分位。"
        "历史曲线来自百度股市通；分位越低表示相对自身历史越便宜。"
    )

    with st.expander("查看历史明细 (最近 60 个交易日)"):
        detail = hist.tail(60).copy()
        detail["date"] = pd.to_datetime(detail["date"]).dt.strftime("%Y-%m-%d")
        detail = detail.rename(columns={"date": "日期", "value": metric_label})
        st.dataframe(detail, width="stretch", hide_index=True)


def _render_valuation() -> None:
    """估值指标子视图: 任意股票的最新 PE/PB/股息率 + 历史 PE/PB."""
    st.caption(
        "输入 A股/港股/美股代码, 查看最新 PE(TTM)、PB、股息率, "
        "以及历史 PE/PB 曲线与自身历史分位。"
    )

    c_code, c_range, c_force = st.columns(
        [3, 1, 1.2], vertical_alignment="bottom"
    )
    raw = c_code.text_input(
        "股票代码",
        value="601398.SS",
        key="valuation_symbol",
        placeholder="如: 601398 / 1398 / 0700.HK / AAPL",
    )
    months = c_range.selectbox(
        "范围",
        _MONTHS,
        index=3,
        format_func=lambda m: f"近 {m} 个月",
        key="valuation_months",
    )
    force = c_force.button(
        ":material/refresh: 强制刷新",
        key="valuation_force",
        help="忽略页面缓存、估值快照缓存与历史序列缓存, 立即重新取数",
    )
    if force:
        clear_valuation_caches()

    try:
        symbol = _normalize_valuation_input(raw)
    except ValueError as e:
        st.error(str(e))
        return

    snap = None
    try:
        with st.spinner(f"拉取 {symbol} 最新估值..."):
            snap = cached_valuation(symbol, refresh=force)
    except Exception as e:  # noqa: BLE001 - 快照失败仍可尝试历史曲线
        st.warning(f"{symbol}: 估值快照取数失败 ({e})")

    if snap is not None:
        _render_valuation_metrics(snap)

    st.divider()
    _render_valuation_history(symbol, months, force)

    with st.expander("指标口径与数据源"):
        st.markdown(
            "- **PE (TTM)**: 滚动市盈率 = 股价 ÷ 最近 12 个月每股收益。"
            "亏损公司的 PE 为负, 不具备估值比较意义, 页面显示「—」。\n"
            "- **PB**: 市净率 = 股价 ÷ 每股净资产。负净资产时同样不展示。\n"
            "- **股息率**: 最近 12 个月每股分红 ÷ 股价; "
            "「5 年平均股息率」用于判断当前股息率相对自身历史的高低。\n"
            "- **最新快照**: OpenBB / yfinance, 6 小时缓存。\n"
            "- **历史曲线**: akshare 百度股市通 (A股 / 港股 / 美股), "
            "按所选范围截断并缓存 6 小时。历史曲线目前提供 PE(TTM) 与 PB; "
            "股息率仅展示最新快照。\n"
            "- **A+H 股票**: 同一公司在 A 股与 H 股会因两地价差出现不同的 "
            "PE/PB/股息率; 价差本身请切到「AH 溢价」。"
        )


# ---------- AH 溢价子视图 (保留原有能力) ----------


def _render_ah_premium() -> None:
    """单只 A+H 股票的 AH 溢价率曲线 (原有页面能力)."""
    st.caption(
        "单只 A+H 股票的 AH 溢价率曲线。"
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
