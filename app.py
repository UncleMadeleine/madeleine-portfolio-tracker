"""Streamlit 投资组合追踪页面."""
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

PORTFOLIO_PATH = Path(__file__).parent / "portfolio.json"
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
    st.divider()
    st.subheader("持仓")
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
    if c1.button("💾 保存", width="stretch"):
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
            st.toast("已保存")
    if c2.button("↩️ 重新加载", width="stretch"):
        st.session_state.pop("holdings_editor", None)
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.caption(
        "数据源: Yahoo Finance (OpenBB) + akshare 兜底; 汇率 yfinance + CFETS。"
        "非美股行情一般延迟 15-30 分钟, 仅供个人参考。"
    )

holdings = edited.dropna(subset=["symbol"]) if not edited.empty else edited
if holdings.empty:
    st.info("在左侧添加持仓并保存。支持 A股/港股/美股/德股/英股/加股/澳股。")
    st.stop()

symbols = tuple(str(s) for s in holdings["symbol"].tolist())
with st.spinner("拉取行情 (首次加载需初始化数据引擎)..."):
    quotes, errors = cached_quotes(symbols, prefer_akshare)
if not quotes:
    st.error(
        "未能获取任何行情。请检查网络: Yahoo 需可访问 finance.yahoo.com; "
        "国内网络可勾选 akshare 优先。"
    )
    if errors:
        with st.expander("错误详情"):
            for k, v in errors.items():
                st.write(f"{k}: {v}")
    st.stop()

currencies = tuple(sorted({q.currency for q in quotes.values()}))
fx, fx_missing = cached_fx(base, currencies)
holding_records = holdings.to_dict("records")
view, issues = build_view(holding_records, quotes, fx)
summary = summarize(view)
all_issues = (
    [f"{k}: {v}" for k, v in errors.items()]
    + issues
    + [f"汇率缺失: {c}" for c in fx_missing]
)

m = summary
col1, col2, col3, col4 = st.columns(4)
col1.metric(f"总市值 ({base})", fmt(m["total_value"]))
col2.metric(
    f"浮动盈亏 ({base})",
    fmt(m["total_pnl"]),
    f"{m['total_pnl_pct']:+.2%}" if m["total_pnl_pct"] is not None else None,
)
col3.metric(f"今日估算 ({base})", fmt(m["today_pnl"]))
col4.metric("持仓", f"{len(view)} / {len(symbols)}")
st.caption(f"数据时间: {datetime.now():%Y-%m-%d %H:%M} · 免费数据源有延迟, 仅供参考")

if all_issues:
    with st.expander(f"⚠ 数据问题 ({len(all_issues)})"):
        for i in all_issues:
            st.write(f"- {i}")

if view.empty:
    st.warning("没有可展示的持仓 (行情或汇率全部失败)。")
    st.stop()

tab1, tab2, tab3 = st.tabs(["持仓明细", "资产配置", "走势对比"])

with tab1:
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
    default_sel = view["symbol"].head(3).tolist()
    sel = st.multiselect(
        "选择代码 (近 12 个月收盘价)", view["symbol"].tolist(), default=default_sel
    )
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
