"""Streamlit 投资组合追踪页面 (持仓 + 自选股价格提醒 + K线查询).

运行: python -m streamlit run tracker/ui/app.py (仓库根执行)。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# streamlit 以裸脚本执行本文件, 此时仓库根不在 sys.path — 手动引导,
# 否则 `import tracker` (及其导入的相对包路径) 失败。
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tracker import prices  # noqa: E402
from tracker.analytics import build_view, summarize  # noqa: E402
from tracker.fx import get_fx_rates  # noqa: E402
from tracker.symbols import parse  # noqa: E402
from tracker import storage  # noqa: E402
from tracker.ui import settings as S  # noqa: E402
from tracker.ui.import_page import render_import_page  # noqa: E402
from tracker.ui.kline_page import render_kline_page, set_quick_symbols  # noqa: E402
from tracker.ui.index_page import render_index_page  # noqa: E402
from tracker.ui.settings_page import render_settings_page  # noqa: E402
from tracker.services.watchlist import (  # noqa: E402
    build_watchlist_view,
    entries_for,
    list_names,
    merge_entries,
    sort_watchlist,
    triggered_entries,
)
from tracker.watchlist import (  # noqa: E402
    normalize_watch_entry,
    parse_lists,
)

WATCHLIST_PATH = storage.WATCHLIST_PATH

st.set_page_config(page_title="投资组合追踪", page_icon="📈", layout="wide")

if "app_page" not in st.session_state:
    st.session_state.app_page = "portfolio"


@st.cache_data(ttl=300, show_spinner=False)
def cached_quotes(symbols: tuple[str, ...], prefer_akshare: bool, use_ibkr: bool):
    return prices.get_quotes(list(symbols), prefer_akshare=prefer_akshare, use_ibkr=use_ibkr)


@st.cache_data(ttl=600, show_spinner=False)
def cached_fx(base: str, currencies: tuple[str, ...]):
    return get_fx_rates(base, list(currencies))


_PAGES = {
    "portfolio": ":material/pie_chart: 组合",
    "kline": ":material/candlestick_chart: K线",
    "index": ":material/insights: 指数K线",
    "import": ":material/download: 导入",
    "settings": ":material/settings: 设置",
}


def _post_import() -> None:
    """导入写盘后清行情/汇率缓存, 让新持仓立即取数."""
    cached_quotes.clear()
    cached_fx.clear()


def fmt(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{digits}f}"


def _norm_sym(sym: str) -> str:
    """代码 → Yahoo 规范形式; 无法识别时退回原始大写 (调用方随后报错)."""
    try:
        return parse(sym).yahoo
    except ValueError:
        return sym.strip().upper()


def _pnl_color(v) -> str:
    """涨跌单元格字体色: 涨/跌按全局配色 (0/缺失不着色)。"""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == 0:
        return ""
    up_c, down_c = S.up_down_colors()
    return f"color: {up_c}" if v > 0 else f"color: {down_c}"


watch = storage.load_watchlist(WATCHLIST_PATH)
if not watch.get("watchlist"):
    watch["watchlist"] = []

with st.sidebar:
    st.markdown(
        ":material/candlestick_chart: **组合追踪**",
        help="多市场持仓 + 自选提醒 · 数据源 Yahoo/akshare",
    )
    settings = storage.load_settings()
    portfolio = storage.load_portfolio(storage.PORTFOLIO_PATH)
    page = st.segmented_control(
        "页面",
        list(_PAGES.values()),
        default=[_PAGES[st.session_state.app_page]],
        label_visibility="collapsed",
        key="app_page_radio",
    )
    if page is None:  # segmented_control 允许取消选中: 保持原页面
        page = _PAGES[st.session_state.app_page]
    st.session_state.app_page = {v: k for k, v in _PAGES.items()}[str(page)]
    base = portfolio.get("base_currency", "CNY")
    prefer_akshare = settings["prefer_akshare"]
    use_ibkr = settings["use_ibkr"]

    if st.session_state.app_page == "portfolio":
        with st.expander(":material/edit_note: 持仓管理", expanded=True):
            st.caption("代码规范: AAPL · SAP.DE · BP.L · RY.TO · BHP.AX · 0700.HK · 600519.SS · BTC-USD")
            df_h = pd.DataFrame(
                portfolio.get("holdings", []), columns=["symbol", "quantity", "avg_cost"]
            )
            edited = st.data_editor(
                df_h,
                num_rows="dynamic",
                key="holdings_editor",
                column_config={
                    "symbol": st.column_config.TextColumn("代码", help="Yahoo 规范代码"),
                    "quantity": st.column_config.NumberColumn("数量", min_value=0.0),
                    "avg_cost": st.column_config.NumberColumn("成本 (当地货币)", min_value=0.0),
                },
            )
            c1, c2 = st.columns(2)
            if c1.button("保存", icon=":material/save:", width="stretch", key="save_holdings"):
                rows = edited.dropna(subset=["symbol"]).to_dict("records")
                bad, dups = [], []
                seen = set()
                clean = []
                for r in rows:
                    sym = str(r["symbol"]).strip()
                    if not sym:  # 整行已填 symbol 后又清空的残留行
                        continue
                    key = _norm_sym(sym)
                    try:
                        parse(sym)
                    except ValueError:
                        bad.append(sym)
                        continue
                    if key in seen:
                        dups.append(key)
                        continue
                    seen.add(key)
                    r = dict(r)
                    r["symbol"] = key
                    clean.append(r)
                if bad or dups:
                    if bad:
                        st.error(f"无法识别: {', '.join(bad)}")
                    if dups:
                        st.error(f"重复代码 (已去重): {', '.join(sorted(set(dups)))}")
                if not bad:
                    # 保留编辑器不展示的溯源键 (如钱包导入的 import_source), 否则页面
                    # 保存一次就丢掉, 后续多钱包导入会退化成同代码互相覆盖
                    extra = {
                        _norm_sym(h.get("symbol", "")): {
                            k: v for k, v in h.items()
                            if k not in ("symbol", "quantity", "avg_cost", "type")
                        }
                        for h in portfolio.get("holdings", [])
                    }
                    for r in clean:
                        r.update(extra.get(r["symbol"], {}))
                    storage.save_portfolio({"base_currency": base, "holdings": clean})
                    cached_quotes.clear()
                    cached_fx.clear()
                    if dups:
                        st.toast("持仓已保存 (重复行已移除)")
                    else:
                        st.toast("持仓已保存")
            if c2.button("重载", icon=":material/refresh:", width="stretch", key="reload_holdings"):
                st.session_state.pop("holdings_editor", None)
                st.rerun()

        with st.expander(":material/notifications: 自选提醒", expanded=False):
            if not watch.get("watchlist"):
                watch["watchlist"] = []
            st.caption(
                "同一个代码可属于多个列表（「所属列表」列用逗号分隔，如: 科技,美股）。"
                "阈值按当地货币; 两级: upper_1/upper_2、lower_1/lower_2; 可只设一侧。"
                "旧格式自动迁移。"
            )
            st.caption("现有列表: " + "、".join(list_names(watch)))
            entries = watch.get("watchlist", [])
            df_w = pd.DataFrame(
                entries,
                columns=["symbol", "lists", "upper_1", "upper_2", "lower_1", "lower_2", "note"],
            )
            if not df_w.empty and "lists" in df_w:
                df_w["lists"] = df_w["lists"].apply(
                    lambda v: ", ".join(v) if isinstance(v, list) else (str(v) if v else "")
                )
            edited_w = st.data_editor(
                df_w,
                num_rows="dynamic",
                key="watchlist_editor",
                column_config={
                    "symbol": st.column_config.TextColumn("代码", help="Yahoo 规范代码"),
                    "lists": st.column_config.TextColumn("所属列表 (逗号分隔)"),
                    "upper_1": st.column_config.NumberColumn("上限 I", format="%.2f"),
                    "upper_2": st.column_config.NumberColumn("上限 II", format="%.2f"),
                    "lower_1": st.column_config.NumberColumn("下限 I", format="%.2f"),
                    "lower_2": st.column_config.NumberColumn("下限 II", format="%.2f"),
                    "note": st.column_config.TextColumn("备注"),
                },
            )
            c3, c4 = st.columns(2)
            if c3.button("保存", icon=":material/save:", width="stretch", key="save_watchlist"):
                rows = []
                bad, dups = [], []
                seen = set()
                for r in edited_w.dropna(subset=["symbol"]).to_dict("records"):
                    sym = str(r["symbol"]).strip()
                    if not sym:
                        continue
                    key = _norm_sym(sym)
                    try:
                        parse(sym)
                    except ValueError:
                        bad.append(sym)
                        continue
                    if key in seen:
                        dups.append(key)
                        continue
                    seen.add(key)
                    e = normalize_watch_entry(r)
                    e["symbol"] = key
                    e["lists"] = parse_lists(e.get("lists"))
                    # data_editor 清空单元格会产生 NaN; note 若是 NaN 会让
                    # save_watchlist 的 allow_nan=False 直接抛 ValueError
                    for k in ("upper_1", "upper_2", "lower_1", "lower_2", "note"):
                        v = e.get(k)
                        if v is None or (isinstance(v, float) and pd.isna(v)):
                            e.pop(k, None)
                    rows.append(e)
                if bad or dups:
                    if bad:
                        st.error(f"无法识别: {', '.join(bad)}")
                    if dups:
                        st.error(f"重复代码 (已去重): {', '.join(sorted(set(dups)))}")
                if not bad:
                    storage.save_watchlist({"watchlist": rows}, WATCHLIST_PATH)
                    cached_quotes.clear()
                    if dups:
                        st.toast("自选已保存 (重复行已移除)")
                    else:
                        st.toast("自选已保存")
            if c4.button("重载", icon=":material/refresh:", width="stretch", key="reload_watchlist"):
                st.session_state.pop("watchlist_editor", None)
                st.rerun()

    st.divider()
    st.caption(
        "数据源: Yahoo Finance (OpenBB) + akshare 兜底; 汇率 CFETS + yfinance。"
        "非美股行情一般延迟 15-30 分钟, 仅供个人参考。"
    )

if st.session_state.app_page == "portfolio":
    holdings = edited.dropna(subset=["symbol"]) if not edited.empty else edited
    all_watch_entries = merge_entries(watch)
    if holdings.empty and not all_watch_entries:
        st.info("在左侧添加持仓或自选股并保存。支持 A股/港股/美股/德股/英股/加股/澳股/加密货币。")
        st.stop()

    holding_symbols = [str(s) for s in holdings["symbol"].tolist()] if not holdings.empty else []
    watch_symbols = [str(e["symbol"]) for e in all_watch_entries]
    all_symbols = tuple(dict.fromkeys(holding_symbols + watch_symbols))

    set_quick_symbols(list(all_symbols))

    with st.spinner("拉取行情 (首次加载需初始化数据引擎)..."):
        quotes, errors, notes = cached_quotes(all_symbols, prefer_akshare, use_ibkr)
    if not quotes:
        st.error(
            "未能获取任何行情。请检查网络: Yahoo 需可访问 finance.yahoo.com; "
            "国内网络可勾选 akshare 优先。"
        )
        with st.expander("错误详情"):
            for k, v in errors.items():
                st.write(f"{k}: {v}")
        st.stop()

    issues: list[str] = []
    if not holdings.empty:
        holding_currencies = set()
        for s in holding_symbols:
            try:
                key = parse(s).yahoo
            except ValueError:
                key = s
            if key in quotes:
                holding_currencies.add(quotes[key].currency)
        currencies = tuple(sorted(holding_currencies))
        fx, fx_missing = cached_fx(base, currencies)
        view, view_issues = build_view(holdings.to_dict("records"), quotes, fx)
        issues += view_issues + [f"汇率缺失: {c}" for c in fx_missing]
        summary = summarize(view)
        m = summary
    else:
        view = pd.DataFrame()

    wview_all, wissues_all = build_watchlist_view(all_watch_entries, quotes)
    issues += wissues_all
    trig_all = triggered_entries(wview_all)

    # ---- 顶部: 页头 + KPI 卡片 (券商 App 风格: 关键数字前置) ----
    head_l, head_r = st.columns([3, 1])
    head_l.markdown("### 组合总览")
    head_r.caption(
        f":material/schedule: {datetime.now():%m-%d %H:%M}",
        help="免费数据源有延迟 (非美股 15-30 分钟), 仅供参考",
    )
    if notes:
        for n in notes:
            st.caption(f":material/info: {n}")

    if not view.empty:
        cov = m.get("cost_coverage")
        pnl_delta = (
            f"{m['total_pnl_pct']:+.2%} (覆盖 {cov:.0%})"
            if m["total_pnl_pct"] is not None and cov is not None and cov < 1.0
            else f"{m['total_pnl_pct']:+.2%}" if m["total_pnl_pct"] is not None
            else None
        )
        with st.container(horizontal=True):
            st.metric(f"总市值 ({base})", fmt(m["total_value"]), border=True)
            st.metric(
                f"浮动盈亏 ({base})",
                fmt(m["total_pnl"]),
                pnl_delta,
                delta_color=S.delta_color(),  # 涨跌配色跟随设置: 红↑绿↓ 或 绿↑红↓
                border=True,
            )
            st.metric(
                f"今日估算 ({base})",
                fmt(m["today_pnl"]),
                None if m["today_pnl"] is None else f"{m['today_pnl']:+,.0f}",
                delta_color=S.delta_color(),
                border=True,
            )
            st.metric(
                "持仓 / 自选",
                f"{len(view)} / {len(wview_all)}",
                border=True,
            )

    # ---- 阈值告警前置 (最关键信息, 类似券商推送条) ----
    if not trig_all.empty:
        with st.container(border=True):
            top, more = st.columns([5, 1], vertical_alignment="center")
            top.markdown(
                f":material/notifications_active: **{len(trig_all)} 只自选触及价格阈值** · "
                + " · ".join(
                    f"**:{S.up_down_tags()[0]}[{r['symbol']}]** {r['status'].split(' ', 1)[1]}"
                    for _, r in trig_all.iterrows()
                )
            )
            with more.popover("详情", icon=":material/expand_more:", width="stretch"):
                for _, r in trig_all.iterrows():
                    st.markdown(
                        f"**{r['symbol']}** {r['status']}  \n"
                        f"现价 {r['price']:,.2f} {r['currency']}"
                        + (f" · {r['note']}" if r["note"] else "")
                    )


    if errors or issues:
        with st.expander(f":material/warning: 数据问题 ({len(errors) + len(issues)})", icon=":material/warning:"):
            for k, v in errors.items():
                st.write(f"- {k}: {v}")
            for i in issues:
                st.write(f"- {i}")

    tab1, tab4, tab2 = st.tabs(
        [
            ":material/table_chart: 持仓明细",
            f":material/visibility: 自选观察{' :red[🔔' + str(len(trig_all)) + ']' if len(trig_all) else ''}",
            ":material/donut_large: 资产配置",
        ]
    )
    with tab1:
        if view.empty:
            st.info("暂无持仓数据。")
        else:
            styled = view.style.map(_pnl_color, subset=["change_pct", "pnl", "pnl_pct", "today_pnl"])
            st.dataframe(
                styled,
                width="stretch",
                height=min(120 + 35 * len(view), 560),
                hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("代码", pinned=True, width="small"),
                    "name": st.column_config.TextColumn("名称", width="medium"),
                    "market": st.column_config.TextColumn("市场", width="small"),
                    "currency": st.column_config.TextColumn("币种", width="small"),
                    "price": st.column_config.NumberColumn("现价", format="%.3f", width="small"),
                    "change_pct": st.column_config.NumberColumn(
                        "涨跌%", format="%+.2f%%", width="small"
                    ),
                    "quantity": st.column_config.NumberColumn("数量", format="%,!.6f", width="small"),
                    "avg_cost": st.column_config.NumberColumn("成本(当地)", format="%.4f", width="small"),
                    "market_value": st.column_config.NumberColumn(
                        f"市值({base})", format="%,.0f", width="small"
                    ),
                    "cost": st.column_config.NumberColumn(f"成本({base})", format="%,.0f", width="small"),
                    "pnl": st.column_config.NumberColumn(f"盈亏({base})", format="%+,.0f", width="small"),
                    "pnl_pct": st.column_config.NumberColumn("盈亏%", format="%+.2f%%", width="small"),
                    "weight": None,
                    "weight_pct": st.column_config.ProgressColumn(
                        "权重", min_value=0, max_value=100, format="%.1f%%", width="small"
                    ),
                    "today_pnl": st.column_config.NumberColumn(
                        f"今日({base})", format="%+,.0f", width="small"
                    ),
                },
            )
            dl_l, dl_r = st.columns([1, 3])
            dl_l.download_button(
                "导出 CSV",
                view.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"portfolio_{datetime.now():%Y%m%d}.csv",
                mime="text/csv",
                icon=":material/download:",
            )

    with tab2:
        if view.empty:
            st.info("暂无持仓数据。")
        else:
            c1, c2 = st.columns(2)
            with c1:
                st.plotly_chart(
                    px.pie(view, names="market", values="market_value", title="按市场"),
                    width="stretch",
                )
            with c2:
                st.plotly_chart(
                    px.pie(view, names="currency", values="market_value", title="按币种"),
                    width="stretch",
                )
            st.plotly_chart(
                px.bar(
                    view.head(15),
                    x="symbol",
                    y="market_value",
                    color="market",
                    title=f"持仓市值 Top 15 ({base})",
                ),
                width="stretch",
            )

    with tab4:
        if not watch.get("watchlist"):
            st.info("自选为空。在左侧「自选提醒」中添加代码与价格阈值。")
        else:
            names = list_names(watch)
            fl1, fl2 = st.columns([3, 2])
            scope = fl1.selectbox(
                "查看范围", ["全部"] + names, help="按所属列表过滤 (在左侧自选提醒中维护)",
            )
            sort_mode = fl2.selectbox(
                "排序",
                ["默认 (触发优先)", "阈值等级", "当日涨跌幅 ↓", "当日涨跌幅 ↑"],
                label_visibility="collapsed",
            )
            display_entries = entries_for(watch, None if scope == "全部" else scope)
            wview, wissues_scope = build_watchlist_view(display_entries, quotes)
            trig = triggered_entries(wview)
            for i in wissues_scope:
                st.caption(f":material/warning: {i}")
            if not trig.empty:
                for _, r in trig.iterrows():
                    st.warning(
                        f"**{r['symbol']}** {r['status']} · 现价 {r['price']:,.2f} {r['currency']}"
                        + (f" · {r['note']}" if r["note"] else ""),
                        icon=":material/notifications_active:",
                    )
            else:
                st.caption(":material/check_circle: 自选中暂无阈值触发")
            st.caption(
                f"当前查看: {scope} · 阈值按当地货币; I 为预警线 / II 为强提醒线; "
                "距离 = 还需变动百分之几才触发 (负值=已越过)。"
            )
            if not wview.empty:
                mode_map = {
                    "默认 (触发优先)": "default",
                    "阈值等级": "severity",
                    "当日涨跌幅 ↓": "change_desc",
                    "当日涨跌幅 ↑": "change_asc",
                }
                wview = sort_watchlist(wview, mode_map.get(sort_mode, "default"))
            styled_w = wview.style.map(
                _pnl_color, subset=["change_pct"]
            ).map(
                lambda v: f"color: {S.up_down_colors()[0]}; font-weight: 600" if str(v).startswith(("🟠", "🔴")) else (
                    "color: #f0a30a; font-weight: 600" if str(v).startswith("🟡") else (
                        f"color: {S.up_down_colors()[1]}; font-weight: 600" if str(v).startswith("🟢") else ""
                    )
                ),
                subset=["status"],
            )
            st.dataframe(
                styled_w,
                width="stretch",
                height=min(120 + 35 * len(wview), 560) if not wview.empty else None,
                hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("代码", pinned=True, width="small"),
                    "name": st.column_config.TextColumn("名称", width="medium"),
                    "market": st.column_config.TextColumn("市场", width="small"),
                    "currency": st.column_config.TextColumn("币种", width="small"),
                    "price": st.column_config.NumberColumn("现价", format="%.3f", width="small"),
                    "change_pct": st.column_config.NumberColumn(
                        "涨跌%", format="%+.2f%%", width="small"
                    ),
                    "upper_1": st.column_config.NumberColumn("上限 I", format="%.2f", width="small"),
                    "upper_2": st.column_config.NumberColumn("上限 II", format="%.2f", width="small"),
                    "lower_1": st.column_config.NumberColumn("下限 I", format="%.2f", width="small"),
                    "lower_2": st.column_config.NumberColumn("下限 II", format="%.2f", width="small"),
                    "status": st.column_config.TextColumn("状态", width="small"),
                    "dist_upper_1_pct": st.column_config.NumberColumn("距上限 I %", format="%.1f", width="small"),
                    "dist_upper_2_pct": st.column_config.NumberColumn("距上限 II %", format="%.1f", width="small"),
                    "dist_lower_1_pct": st.column_config.NumberColumn("距下限 I %", format="%.1f", width="small"),
                    "dist_lower_2_pct": st.column_config.NumberColumn("距下限 II %", format="%.1f", width="small"),
                    "note": st.column_config.TextColumn("备注", width="medium"),
                    "triggered": None,
                },
            )

elif st.session_state.app_page == "import":
    render_import_page(on_saved=_post_import)

elif st.session_state.app_page == "settings":
    render_settings_page()

elif st.session_state.app_page == "kline":
    render_kline_page(prefer_akshare)
elif st.session_state.app_page == "index":
    render_index_page()
