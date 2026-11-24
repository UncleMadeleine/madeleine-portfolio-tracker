"""「指数K线」页面: 宏观/风险指数查询 (IX.<KEY>), 与股票 K线页完全独立.

复用 K线图表组件与渲染函数; 数据走 prices.get_index_history (provider 源链:
中国指数 akshare 优先, 其余 yfinance 主源), 缓存键 index: 前缀隔离。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from . import settings, theme
from .issues import render_issue_summary
from .kline_page import render_kline_view

from tracker import charting, index_ratios, prices
from tracker.symbols import INDEX_CATALOG, index_groups, index_label


@st.cache_data(ttl=1800, show_spinner=False)
def cached_index_kline(symbol: str, months: int):
    """指数日线 (磁盘缓存 + 内存缓存双层, TTL 内切换参数不重复请求网络)."""
    return prices.get_index_history(symbol, months=months)


def render_index_page() -> None:
    """指数K线查询页: 分组选择指数 → 切换即拉取渲染 (无按钮, 选择驱动)."""
    theme.page_header("指数K线", eyebrow="INDEX CHARTS")

    labels = {}
    for group, keys in index_groups().items():
        for k in keys:
            if k in INDEX_CATALOG:
                labels[f"{index_label(k)} ({group})"] = k
    extra = [k for k in INDEX_CATALOG if k not in labels.values()]
    for k in extra:
        labels[index_label(k)] = k

    sel_col, r1_col, r2_col = st.columns([3, 1, 1])
    chosen = sel_col.selectbox(
        "指数",
        list(labels.keys()),
        index=list(labels.keys()).index("美元指数 (美元/利率)")
        if "美元指数 (美元/利率)" in labels
        else 0,
        key="index_symbol",
    )
    imonths = r1_col.selectbox(
        "范围",
        [3, 6, 12, 24, 36, 60],
        index=2,
        format_func=lambda m: f"近 {m} 个月",
        key="index_months",
    )
    imas = st.multiselect(
        "均线",
        [5, 10, 20, 30, 60, 120, 250],
        default=[5, 20, 60],
        key="index_ma",
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

    # 数据驱动兜底: 目录/源标注缺失或漂移时, 以实际数据原生粒度为准
    # (重采样只能日→周→月单向变粗, 细于原生粒度的周期一律禁用)
    native = charting.infer_native_period(kdf)
    periods = charting.available_periods(native)
    if st.session_state.get("index_period") not in periods:
        st.session_state["index_period"] = periods[0]
    iperiod = r2_col.selectbox(
        "周期",
        periods,
        format_func=lambda v: charting.PERIOD_LABELS[v],
        help=None
        if native == "daily"
        else f"该指数数据源为{charting.PERIOD_LABELS[native]}, 更细周期不可用",
        key="index_period",
    )
    # 可选叠加比率曲线 (默认关闭): 金银比 / 金油比, 独立左侧价轴.
    # 两条腿均取美元计价的指数域代码 (COMEX 黄金/白银、WTI 原油), 无需汇率换算。
    ratio_label = st.selectbox(
        "叠加比率",
        index_ratios.ratio_option_labels(),
        index=0,
        key="index_ratio",
        help="在指数K线上叠加一条比率曲线 (独立左侧纵轴): "
        "金银比 = COMEX 黄金 ÷ COMEX 白银, 金油比 = COMEX 黄金 ÷ WTI 原油; "
        "两条腿均为 Yahoo 美元计价连续合约 (换月跳空属正常构造)",
    )
    ratio_preset = index_ratios.preset_for_label(ratio_label)
    main_ratio_preset = index_ratios.preset_for_index(yahoo)
    if (
        ratio_preset is not None
        and main_ratio_preset is not None
        and ratio_preset.key == main_ratio_preset.key
    ):
        st.caption("当前指数本身就是该比率, 叠加曲线已忽略。")
        ratio_preset = None
    overlay = None
    if ratio_preset is not None:
        ratio_frames: dict[str, pd.DataFrame] = {}
        ratio_issues: list[str] = []
        with st.spinner(f"拉取 {ratio_preset.label} 两条腿数据..."):
            for leg in ratio_preset.symbols:
                try:
                    leg_df = cached_index_kline(leg, imonths)
                except Exception as e:  # noqa: BLE001 - 单腿失败只影响叠加线
                    ratio_issues.append(f"{leg}: {e}")
                    continue
                if isinstance(leg_df, pd.DataFrame) and not leg_df.empty:
                    ratio_frames[leg] = leg_df
                else:
                    ratio_issues.append(f"{leg}: 无有效K线数据")
        render_issue_summary(
            ratio_issues,
            noun="叠加比率取数问题",
            details_label="查看失败明细",
            key="index_ratio_issues",
        )
        if len(ratio_frames) == len(ratio_preset.symbols):
            try:
                overlay = index_ratios.build_ratio_curve(
                    ratio_frames, ratio_preset.key, period=iperiod
                )
            except ValueError as e:
                st.warning(f"叠加比率: {e}")

    render_kline_view(
        kdf,
        f"{index_label(key)} ({yahoo})",
        period=iperiod,
        mas=imas,
        show_volume=False,
        green_up=settings.green_up(),
        currency=None,  # 指数以点数/%计价, 不标货币
        overlay=overlay,
        # FRED 国债/信用利差可出现 0 或负值 (10Y-2Y 倒挂), 不能按股票价格清洗
        allow_nonpositive=bool(INDEX_CATALOG[key].get("fred")),
        reference=0.0 if key in ("US10Y2Y", "US10Y3M") else None,
        reference_label="倒挂分界" if key in ("US10Y2Y", "US10Y3M") else None,
    )
    if key in ("US10Y2Y", "US10Y3M"):
        st.caption(
            "利差 = 长端收益率 − 短端收益率: 负值即收益率曲线倒挂, "
            "0 轴为倒挂/正常的分界; 倒挂持续越久、负值越深, 通常越受关注。"
        )
    if main_ratio_preset is not None:
        st.caption(
            f"派生比率指数 {yahoo} = {main_ratio_preset.formula}; "
            "比率没有原生开高低, 日K按收盘点位绘制 (周/月K聚合区间高低)。"
        )
    if overlay:
        prec = int(overlay["precision"])
        cur_v = float(overlay["data"][-1]["value"])
        mean_v = float(overlay["mean"])
        dev = (cur_v / mean_v - 1) * 100 if mean_v else 0.0
        st.caption(
            f"叠加曲线 {ratio_preset.label} = {ratio_preset.formula}"
            f" ({overlay['name']}); 当前 {cur_v:.{prec}f}"
            f" · 均值 {mean_v:.{prec}f} · 较均值 {dev:+.2f}%"
            " · 使用左侧独立纵轴, 虚线为区间均值; "
            "两条腿为美元计价连续合约, 换月跳空属正常构造。"
        )
