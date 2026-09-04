"""Streamlit 投资组合追踪页面 (持仓 + 自选股价格提醒)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from tracker import prices
from tracker.analytics import build_view, summarize
from tracker.fx import get_fx_rates
from tracker.symbols import parse
from tracker.watchlist import (
    _normalize_entry,
    build_watchlist_view,
    load_watchlist,
    save_watchlist,
    triggered_entries,
)

PORTFOLIO_PATH = Path(__file__).parent / "portfolio.json"
WATCHLIST_PATH = Path(__file__).parent / "watchlist.json"
BASE_CURRENCIES = ["CNY", "USD", "EUR", "HKD"]

st.set_page_config(page_title="投资组合追踪", page_icon="📈", layout="wide")


def load_portfolio_file() -> dict:
    if PORTFOLIO_PATH.exists():
        return json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return {"base_currency": "CNY", "holdings": []}


def save_portfolio_file(data: dict) -> None:
    PORTFOLIO_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@st.cache_data(ttl=300, show_spinner=False)
def cached_quotes(symbols: tuple[str, ...], prefer_akshare: bool):
    return prices.get_quotes(list(symbols), prefer_akshare=prefer_akshare)


@st.cache_data(ttl=600, show_spinner=False)
def cached_fx(base: str, currencies: tuple[str, ...]):
    return get_fx_rates(base, list(currencies))


@st.cache_data(ttl=1800, show_spinner=False)
def cached_history(symbol: str, prefer_akshare: bool):
    return prices.get_history(symbol, months=12, prefer_akshare=prefer_akshare)


def fmt(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{digits}f}"


with st.sidebar:
    st.title("📈 组合追踪")
    portfolio = load_portfolio_file()
    saved_base = portfolio.get("base_currency", "CNY")
    base = st.selectbox(
        "基础货币", BASE_CURRENCIES,
        index=BASE_CURRENCIES.index(saved_base) if saved_base in BASE_CURRENCIES else 0,
    )
    prefer_akshare = st.checkbox("A股/港股优先 akshare (国内网络)", value=False)
    use_ibkr = st.checkbox(
        "🔗 IBKR 行情 (需本机 TWS/IB Gateway)",
        value=False,
        help="启用后优先从 IBKR 获取行情 (有订阅则为实时), 失败自动回退 Yahoo/akshare。连接参数见 ibkr.json",
    )

    st.divider()
    st.subheader("💼 持仓")
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
        bad = []
        for r in rows:
            try:
                parse(str(r["symbol"]))
            except ValueError:
                bad.append(str(r["symbol"]))
        if bad:
            st.error(f"无法识别: {', '.join(bad)}")
        else:
            save_portfolio_file({"base_currency": base, "holdings": rows})
            st.cache_data.clear()
            st.toast("持仓已保存")
    if c2.button("↩️ 重载持仓", width="stretch"):
        st.session_state.pop("holdings_editor", None)
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("🎯 自选提醒")
    watch = load_watchlist(WATCHLIST_PATH)
    with st.expander("编辑自选 / 价格阈值", expanded=False):
        st.caption(
            "阈值按当地货币 (与现价同币种)。支持两级: "
            "upper_1 / upper_2（上限 I / II，II 更严格）、lower_1 / lower_2（下限 I / II，II 更严格）；"
            "可只设一侧。旧格式 upper / lower 会自动迁至 upper_1 / lower_1。"
        )
        df_w = pd.DataFrame(
            watch.get("watchlist", []),
            columns=["symbol", "upper_1", "upper_2", "lower_1", "lower_2", "note"],
        )
        edited_w = st.data_editor(
            df_w,
            num_rows="dynamic",
            key="watchlist_editor",
            width="stretch",
            column_config={
                "symbol": st.column_config.TextColumn("代码"),
                "upper_1": st.column_config.NumberColumn("上限 I", help="当地货币"),
                "upper_2": st.column_config.NumberColumn("上限 II", help="更严格的触发线"),
                "lower_1": st.column_config.NumberColumn("下限 I", help="当地货币"),
                "lower_2": st.column_config.NumberColumn("下限 II", help="更严格的触发线"),
                "note": st.column_config.TextColumn("备注"),
            },
        )
        c3, c4 = st.columns(2)
        if c3.button("💾 保存自选", width="stretch"):
            rows = [_normalize_entry(r) for r in edited_w.dropna(subset=["symbol"]).to_dict("records")]
            bad = []
            for r in rows:
                try:
                    parse(str(r["symbol"]))
                except ValueError:
                    bad.append(str(r["symbol"]))
            if bad:
                st.error(f"无法识别: {', '.join(bad)}")
            else:
                save_watchlist({"watchlist": rows}, WATCHLIST_PATH)
                st.cache_data.clear()
                st.toast("自选已保存")
        if c4.button("↩️ 重载自选", width="stretch"):
            st.session_state.pop("watchlist_editor", None)
            st.cache_data.clear()
            st.rerun()

    st.divider()
    st.caption(
        "数据源: Yahoo Finance (OpenBB) + akshare 兜底; 汇率 CFETS + yfinance。"
        "非美股行情一般延迟 15-30 分钟, 仅供个人参考。"
    )

holdings = edited.dropna(subset=["symbol"]) if not edited.empty else edited
watch_rows = edited_w.dropna(subset=["symbol"]) if not edited_w.empty else edited_w
if holdings.empty and watch_rows.empty:
    st.info("在左侧添加持仓或自选股并保存。支持 A股/港股/美股/德股/英股/加股/澳股。")
    st.stop()

holding_symbols = [str(s) for s in holdings["symbol"].tolist()] if not holdings.empty else []
watch_symbols = [str(s) for s in watch_rows["symbol"].tolist()] if not watch_rows.empty else []
all_symbols = tuple(dict.fromkeys(holding_symbols + watch_symbols))

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
    currencies = tuple(sorted({q.currency for q in quotes.values()}))
    fx, fx_missing = cached_fx(base, currencies)
    view, view_issues = build_view(holdings.to_dict("records"), quotes, fx)
    issues += view_issues + [f"汇率缺失: {c}" for c in fx_missing]
    summary = summarize(view)
    m = summary
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(f"总市值 ({base})", fmt(m["total_value"]))
    col2.metric(
        f"浮动盈亏 ({base})",
        fmt(m["total_pnl"]),
        f"{m['total_pnl_pct']:+.2%}" if m["total_pnl_pct"] is not None else None,
    )
    col3.metric(f"今日估算 ({base})", fmt(m["today_pnl"]))
    col4.metric("持仓", f"{len(view)} / {len(holding_symbols)}")
else:
    view = pd.DataFrame()

wview, wissues = build_watchlist_view(
    watch_rows.to_dict("records") if not watch_rows.empty else [], quotes
)
issues += wissues
trig = triggered_entries(wview)

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

badge = f" 🔔{len(trig)}" if not trig.empty else ""
tab1, tab2, tab3, tab4 = st.tabs(
    ["💼 持仓明细", "🥧 资产配置", "📈 走势对比", f"🎯 自选观察{badge}"]
)

with tab1:
    if view.empty:
        st.info("暂无持仓数据。")
    else:
        st.dataframe(
            view,
            width="stretch",
            hide_index=True,
            column_config={
                "symbol": st.column_config.TextColumn("代码"),
                "name": st.column_config.TextColumn("名称"),
                "market": st.column_config.TextColumn("市场"),
                "currency": st.column_config.TextColumn("币种"),
                "price": st.column_config.NumberColumn("现价", format="%.3f"),
                "change_pct": st.column_config.NumberColumn("涨跌%", format="%.2f"),
                "quantity": st.column_config.NumberColumn("数量", format="%.6g"),
                "avg_cost": st.column_config.NumberColumn("成本(当地)", format="%.4f"),
                "market_value": st.column_config.NumberColumn(f"市值({base})", format="%.2f"),
                "cost": st.column_config.NumberColumn(f"成本({base})", format="%.2f"),
                "pnl": st.column_config.NumberColumn(f"盈亏({base})", format="%.2f"),
                "pnl_pct": st.column_config.NumberColumn("盈亏%", format="%.2f"),
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
            + (wview["symbol"].tolist() if not wview.empty else [])
        )
    )
    if not chart_symbols:
        st.info("暂无可展示的代码。")
    else:
        default_sel = chart_symbols[:3]
        sel = st.multiselect("选择代码 (近 12 个月收盘价)", chart_symbols, default=default_sel)
        norm = st.toggle("归一化 (起点=100)", value=True)
        frames = {}
        for s in sel:
            try:
                h = cached_history(s, prefer_akshare)
                srs = pd.Series(
                    h["close"].astype(float).tolist(),
                    index=pd.to_datetime(h["date"]),
                    name=s,
                )
                frames[s] = srs
            except Exception as e:
                st.warning(f"{s}: {e}")
        if frames:
            px_df = pd.DataFrame(frames)
            px_df = px_df.ffill().dropna(how="all")
            if norm and not px_df.empty:
                px_df = px_df / px_df.iloc[0] * 100
            y_label = "归一化" if norm else "收盘价 (当地货币)"
            fig = px.line(px_df, labels={"value": y_label, "variable": "代码"})
            fig.update_layout(legend_title="代码")
            st.plotly_chart(fig, width="stretch")

with tab4:
    if wview.empty:
        st.info("自选为空。在左侧「自选提醒」中添加代码与价格阈值。")
    else:
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
            "阈值按当地货币; 两级触发: I 为预警线 / II 为强提醒线; 距离 = 还需变动百分之几才触发 (负值=已越过)。"
        )
        st.dataframe(
            wview,
            width="stretch",
            hide_index=True,
            column_config={
                "symbol": st.column_config.TextColumn("代码"),
                "name": st.column_config.TextColumn("名称"),
                "market": st.column_config.TextColumn("市场"),
                "currency": st.column_config.TextColumn("币种"),
                "price": st.column_config.NumberColumn("现价", format="%.3f"),
                "change_pct": st.column_config.NumberColumn("涨跌%", format="%.2f"),
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
