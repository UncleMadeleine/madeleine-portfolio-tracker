"""Streamlit 投资组合追踪页面 (持仓 + 自选股价格提醒 + K线查询)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import kline_page

from tracker import charting, prices
from tracker.analytics import build_view, summarize
from tracker.fx import get_fx_rates
from tracker.symbols import parse
from tracker.watchlist import (
    _normalize_entry,
    build_watchlist_view,
    entries_for,
    list_names,
    load_watchlist,
    merge_entries,
    parse_lists,
    save_watchlist,
    sort_watchlist,
    triggered_entries,
)

PORTFOLIO_PATH = Path(__file__).parent / "portfolio.json"
WATCHLIST_PATH = Path(__file__).parent / "watchlist.json"
BASE_CURRENCIES = ["CNY", "USD", "EUR", "HKD"]

st.set_page_config(page_title="投资组合追踪", page_icon="📈", layout="wide")

if "app_page" not in st.session_state:
    st.session_state.app_page = "portfolio"


def load_portfolio_file() -> dict:
    if PORTFOLIO_PATH.exists():
        return json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return {"base_currency": "CNY", "holdings": []}


def _clean_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        clean = {}
        for k, v in r.items():
            if isinstance(v, float) and pd.isna(v):
                continue
            if v is None:
                continue
            clean[k] = v
        out.append(clean)
    return out


def save_portfolio_file(data: dict) -> None:
    data = dict(data)
    data["holdings"] = _clean_rows(data.get("holdings", []))
    PORTFOLIO_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


@st.cache_data(ttl=300, show_spinner=False)
def cached_quotes(symbols: tuple[str, ...], prefer_akshare: bool, use_ibkr: bool):
    return prices.get_quotes(list(symbols), prefer_akshare=prefer_akshare, use_ibkr=use_ibkr)


@st.cache_data(ttl=600, show_spinner=False)
def cached_fx(base: str, currencies: tuple[str, ...]):
    return get_fx_rates(base, list(currencies))


def fmt(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{digits}f}"


UP_COLOR, DOWN_COLOR = "#ef232a", "#14b143"  # 与 K线默认一致: 红涨绿跌


def _norm_sym(s) -> str:
    """规范化为 Yahoo 代码; 无法解析时返回大写原文。"""
    try:
        return parse(str(s)).yahoo
    except ValueError:
        return str(s).strip().upper()


def _pnl_color(v) -> str:
    """涨跌单元格字体色: 涨红跌绿 (0/缺失不着色)。"""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == 0:
        return ""
    return f"color: {UP_COLOR}" if v > 0 else f"color: {DOWN_COLOR}"


def _on_base_change() -> None:
    """切换基础货币立即落盘, 不依赖「保存持仓」。"""
    new_base = st.session_state.get("base_currency", "CNY")
    data = load_portfolio_file()
    save_portfolio_file({"base_currency": new_base, "holdings": data.get("holdings", [])})
    st.toast(f"基础货币已保存为 {new_base}")


portfolio = load_portfolio_file()
saved_base = portfolio.get("base_currency", "CNY")

watch = load_watchlist(WATCHLIST_PATH)
if not watch.get("watchlist"):
    watch["watchlist"] = []

with st.sidebar:
    st.title("📈 组合追踪")
    page = st.radio(
        "页面",
        ["📊 投资组合", "🕯 K线"],
        index=0 if st.session_state.app_page == "portfolio" else 1,
        key="app_page_radio",
    )
    st.session_state.app_page = "portfolio" if page == "📊 投资组合" else "kline"
    base = st.selectbox(
        "基础货币", BASE_CURRENCIES,
        index=BASE_CURRENCIES.index(saved_base) if saved_base in BASE_CURRENCIES else 0,
        key="base_currency",
        on_change=_on_base_change,
    )
    prefer_akshare = st.checkbox("A股/港股优先 akshare (国内网络)", value=False)
    use_ibkr = st.checkbox(
        "🔗 IBKR 行情 (需本机 IB Gateway)",
        value=False,
        help="启用后优先从 IBKR 获取行情 (有订阅则为实时), 失败自动回退 Yahoo/akshare。连接参数见 ibkr.json (mode: paper=4002 / live=4001)",
    )

    if st.session_state.app_page == "portfolio":
        with st.expander("💼 持仓管理", expanded=True):
            with st.expander("📋 股票列表", expanded=True):
                st.caption("代码规范: AAPL · SAP.DE · BP.L · RY.TO · BHP.AX · 0700.HK · 600519.SS")
                df_h = pd.DataFrame(
                    portfolio.get("holdings", []), columns=["symbol", "quantity", "avg_cost"]
                )
                edited = st.data_editor(
                    df_h,
                    num_rows="dynamic",
                    key="holdings_editor",
                    width="stretch",
                    column_config={
                        "symbol": st.column_config.TextColumn("代码", help="Yahoo 规范代码"),
                        "quantity": st.column_config.NumberColumn("数量", min_value=0.0),
                        "avg_cost": st.column_config.NumberColumn("成本(当地货币)", min_value=0.0),
                    },
                )
            c1, c2 = st.columns(2)
            if c1.button("💾 保存持仓", width="stretch"):
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
                    save_portfolio_file({"base_currency": base, "holdings": clean})
                    cached_quotes.clear()
                    cached_fx.clear()
                    if dups:
                        st.toast("持仓已保存 (重复行已移除)")
                    else:
                        st.toast("持仓已保存")
            if c2.button("↩️ 重载持仓", width="stretch"):
                st.session_state.pop("holdings_editor", None)
                st.rerun()

        with st.expander("🎯 自选提醒", expanded=False):
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
            c3, c4 = st.columns(2)
            if c3.button("💾 保存自选", width="stretch"):
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
                    e = _normalize_entry(r)
                    e["symbol"] = key
                    e["lists"] = parse_lists(e.get("lists"))
                    for k in ("upper_1", "upper_2", "lower_1", "lower_2"):
                        v = e.get(k)
                        if isinstance(v, float) and pd.isna(v):
                            e.pop(k, None)
                    rows.append(e)
                if bad or dups:
                    if bad:
                        st.error(f"无法识别: {', '.join(bad)}")
                    if dups:
                        st.error(f"重复代码 (已去重): {', '.join(sorted(set(dups)))}")
                if not bad:
                    save_watchlist({"watchlist": rows}, WATCHLIST_PATH)
                    cached_quotes.clear()
                    if dups:
                        st.toast("自选已保存 (重复行已移除)")
                    else:
                        st.toast("自选已保存")
            if c4.button("↩️ 重载自选", width="stretch"):
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
        st.info("在左侧添加持仓或自选股并保存。支持 A股/港股/美股/德股/英股/加股/澳股。")
        st.stop()

    holding_symbols = [str(s) for s in holdings["symbol"].tolist()] if not holdings.empty else []
    watch_symbols = [str(e["symbol"]) for e in all_watch_entries]
    all_symbols = tuple(dict.fromkeys(holding_symbols + watch_symbols))

    kline_page.set_quick_symbols(list(all_symbols))

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
        col1, col2, col3, col4 = st.columns(4)
        col1.metric(f"总市值 ({base})", fmt(m["total_value"]))
        cov = m.get("cost_coverage")
        col2.metric(
            f"浮动盈亏 ({base})",
            fmt(m["total_pnl"]),
            (
                f"{m['total_pnl_pct']:+.2%} (覆盖 {cov:.0%})"
                if m["total_pnl_pct"] is not None and cov is not None and cov < 1.0
                else f"{m['total_pnl_pct']:+.2%}" if m["total_pnl_pct"] is not None
                else None
            ),
            delta_color="inverse",  # 红涨绿跌: 盈亏为正显示红色
        )
        col3.metric(
            f"今日估算 ({base})",
            fmt(m["today_pnl"]),
            None if m["today_pnl"] is None else f"{m['today_pnl']:+,.0f}",
            delta_color="inverse",
        )
        col4.metric("持仓", f"{len(view)} / {len(holding_symbols)}")
    else:
        view = pd.DataFrame()

    wview_all, wissues_all = build_watchlist_view(all_watch_entries, quotes)
    issues += wissues_all
    trig_all = triggered_entries(wview_all)

    st.caption(f"数据时间: {datetime.now():%Y-%m-%d %H:%M} · 免费数据源有延迟, 仅供参考")

    if notes:
        for n in notes:
            st.info(n)

    if errors or issues:
        with st.expander(f"⚠ 数据问题 ({len(errors) + len(issues)})"):
            for k, v in errors.items():
                st.write(f"- {k}: {v}")
            for i in issues:
                st.write(f"- {i}")

    tab1, tab4, tab2, tab3 = st.tabs(
        [
            "💼 持仓明细",
            f"🎯 自选观察{' 🔔' + str(len(trig_all)) if len(trig_all) else ''}",
            "🥧 资产配置",
            "📈 走势对比",
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
                hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("代码"),
                    "name": st.column_config.TextColumn("名称"),
                    "market": st.column_config.TextColumn("市场"),
                    "currency": st.column_config.TextColumn("币种"),
                    "price": st.column_config.NumberColumn("现价", format="%.3f"),
                    "change_pct": st.column_config.NumberColumn("涨跌%", format="%.2f%%"),
                    "quantity": st.column_config.NumberColumn("数量", format="%.6g"),
                    "avg_cost": st.column_config.NumberColumn("成本(当地)", format="%.4f"),
                    "market_value": st.column_config.NumberColumn(f"市值({base})", format="%.2f"),
                    "cost": st.column_config.NumberColumn(f"成本({base})", format="%.2f"),
                    "pnl": st.column_config.NumberColumn(f"盈亏({base})", format="%.2f"),
                    "pnl_pct": st.column_config.NumberColumn("盈亏%", format="%.2f%%"),
                    "weight": None,
                    "weight_pct": st.column_config.ProgressColumn(
                        "权重", min_value=0, max_value=100, format="%.1f%%"
                    ),
                    "today_pnl": st.column_config.NumberColumn(f"今日({base})", format="%.2f"),
                },
            )
            st.download_button(
                "导出 CSV",
                view.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"portfolio_{datetime.now():%Y%m%d}.csv",
                mime="text/csv",
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

    with tab3:
        chart_symbols = list(
            dict.fromkeys(
                (view["symbol"].tolist() if not view.empty else [])
                + (wview_all["symbol"].tolist() if not wview_all.empty else [])
            )
        )
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
            y = kline_page.normalize_or_none(s)
            (sel if y is not None else bad).append(y if y is not None else s)
        sel = list(dict.fromkeys(sel))
        if bad:
            st.error(f"无法识别: {', '.join(bad)}")
        if not sel:
            st.info("选择持仓/自选代码, 或直接输入任意代码 (如 NVDA · 600519.SS) 开始对比。")
        else:
            frames = {}
            with st.spinner(f"拉取 {len(sel)} 只代码近 {cmp_months} 个月 K线..."):
                for s in sel:
                    try:
                        d = kline_page.cached_kline(s, cmp_months, prefer_akshare)
                        if d.empty:
                            st.warning(f"{s}: 无有效K线数据")
                        else:
                            frames[s] = d
                    except Exception as e:
                        st.warning(f"{s}: {e}")
            if frames:
                kline_page.render_compare_chart(
                    charting.compare_payload(
                        frames, normalize=norm, period=cmp_period
                    ),
                    height=560,
                )
                chg = []
                for sym, d in frames.items():
                    dd = charting.resample_ohlc(d, cmp_period) if cmp_period != "daily" else d
                    if len(dd) >= 2:
                        pct = float(dd["close"].iloc[-1]) / float(dd["close"].iloc[0]) - 1
                        chg.append(
                            f"{sym} {':red' if pct >= 0 else ':green'}[{pct:+.2%}]"
                        )
                if chg:
                    st.markdown("区间涨跌: " + " · ".join(chg))
                if norm:
                    st.caption(
                        "各代码按自身区间首个收盘归一化 (=100); 不同市场按各自交易日绘制, "
                        "拖动平移 / 滚轮缩放, 悬停查看当日各代码取值。"
                    )

    with tab4:
        if not watch.get("watchlist"):
            st.info("自选为空。在左侧「自选提醒」中添加代码与价格阈值。")
        else:
            scope = st.selectbox("查看范围", ["全部"] + list_names(watch))
            display_entries = entries_for(watch, None if scope == "全部" else scope)
            wview, wissues_scope = build_watchlist_view(display_entries, quotes)
            trig = triggered_entries(wview)
            for i in wissues_scope:
                st.caption(f"⚠ {i}")
            if not trig.empty:
                st.warning(f"🔔 {len(trig)} 只自选触及价格阈值:")
                for _, r in trig.iterrows():
                    st.write(
                        f"- **{r['symbol']}** {r['status']}  现价 "
                        f"{r['price']:,.2f} {r['currency']}"
                        + (f"  · {r['note']}" if r["note"] else "")
                    )
            else:
                st.success("自选中暂无阈值触发。")
            st.caption(
                f"当前查看: {scope} · 阈值按当地货币; 两级触发: I 为预警线 / II 为强提醒线; "
                "距离 = 还需变动百分之几才触发 (负值=已越过)。"
            )
            if not wview.empty:
                sort_mode = st.selectbox(
                    "排序方式",
                    ["默认 (触发优先 + 代码)", "阈值等级 (严重→温和)", "当日涨跌幅 ↓", "当日涨跌幅 ↑"],
                    label_visibility="collapsed",
                )
                mode_map = {
                    "默认 (触发优先 + 代码)": "default",
                    "阈值等级 (严重→温和)": "severity",
                    "当日涨跌幅 ↓": "change_desc",
                    "当日涨跌幅 ↑": "change_asc",
                }
                wview = sort_watchlist(wview, mode_map.get(sort_mode, "default"))
            styled_w = wview.style.map(
                _pnl_color, subset=["change_pct"]
            ).map(
                lambda v: "color: #ef232a; font-weight: 600" if str(v).startswith(("🟠", "🔴")) else (
                    "color: #f0a30a; font-weight: 600" if str(v).startswith("🟡") else (
                        "color: #14b143; font-weight: 600" if str(v).startswith("🟢") else ""
                    )
                ),
                subset=["status"],
            )
            st.dataframe(
                styled_w,
                width="stretch",
                hide_index=True,
                column_config={
                    "symbol": st.column_config.TextColumn("代码"),
                    "name": st.column_config.TextColumn("名称"),
                    "market": st.column_config.TextColumn("市场"),
                    "currency": st.column_config.TextColumn("币种"),
                    "price": st.column_config.NumberColumn("现价", format="%.3f"),
                    "change_pct": st.column_config.NumberColumn("涨跌%", format="%.2f%%"),
                    "upper_1": st.column_config.NumberColumn("上限 I", format="%.2f"),
                    "upper_2": st.column_config.NumberColumn("上限 II", format="%.2f"),
                    "lower_1": st.column_config.NumberColumn("下限 I", format="%.2f"),
                    "lower_2": st.column_config.NumberColumn("下限 II", format="%.2f"),
                    "status": st.column_config.TextColumn("状态"),
                    "dist_upper_1_pct": st.column_config.NumberColumn("距上限 I %", format="%.1f"),
                    "dist_upper_2_pct": st.column_config.NumberColumn("距上限 II %", format="%.1f"),
                    "dist_lower_1_pct": st.column_config.NumberColumn("距下限 I %", format="%.1f"),
                    "dist_lower_2_pct": st.column_config.NumberColumn("距下限 II %", format="%.1f"),
                    "note": st.column_config.TextColumn("备注"),
                    "triggered": None,
                },
            )

elif st.session_state.app_page == "kline":
    kline_page.render_kline_controls(prefer_akshare=prefer_akshare)
