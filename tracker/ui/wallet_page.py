"""「钱包净值」页面: 按链上转账记录回放单个地址的历史净值曲线.

只读查询, 不需要先把钱包导入 portfolio.json。查询是显式按钮触发的 (链上
分页 + 逐笔交易 + 历史行情可能耗时数秒到数十秒), 结果缓存在 session_state,
参数变化后自动失效, 避免每次 rerun 都重新抓一遍。
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tracker import storage, wallet_history
from tracker.ui import theme
from tracker.wallet_nav import compute_wallet_nav

_CHAIN_LABELS = {
    "eth": "Ethereum",
    "bsc": "BNB Chain",
    "polygon": "Polygon",
    "arbitrum": "Arbitrum One",
    "avalanche": "Avalanche C-Chain",
    "tron": "TRON",
    "solana": "Solana",
}

_CHAIN_NOTES = {
    "eth": "Routescan / Blockscout, 零 API Key",
    "avalanche": "Snowtrace / Routescan, 零 API Key",
    "tron": "TronGrid, 零 API Key (只计可用 TRX)",
    "solana": "公共 RPC, 零 API Key (逐笔交易上限见配置)",
    "polygon": "Blockscout 或 Etherscan V2 key",
    "arbitrum": "Blockscout 或 Etherscan V2 key",
    "bsc": "需要 Etherscan V2 key 或自定义浏览器",
}

_RANGES = {
    "30 天": 30,
    "90 天": 90,
    "180 天": 180,
    "365 天": 365,
}

_BASES = ["USD", "CNY", "EUR", "HKD", "JPY"]

_SESSION_KEY = "_wallet_nav_result"


def _default_base() -> str:
    try:
        portfolio = storage.load_portfolio(storage.PORTFOLIO_PATH)
        base = str(portfolio.get("base_currency") or "USD").upper()
    except Exception:  # noqa: BLE001 - 读失败只影响默认币种
        base = "USD"
    return base if base in _BASES else "USD"


def _render_chart(result: dict) -> None:
    rows = result.get("rows") or []
    if not rows:
        st.warning("未生成净值数据, 请查看下方说明。")
        return
    base = result.get("base") or "USD"
    df = pd.DataFrame(rows)
    df["day"] = pd.to_datetime(df["day"])
    first, last = rows[0], rows[-1]
    change = (last["total_value"] or 0) - (first["total_value"] or 0)
    pct = (
        change / abs(first["total_value"])
        if first["total_value"] not in (None, 0)
        else None
    )

    c1, c2, c3 = st.columns(3)
    c1.metric(f"最新净值 ({base})", f"{last['total_value']:,.2f}")
    c2.metric(
        "区间变化",
        f"{change:+,.2f}",
        delta=f"{pct:+.2%}" if pct is not None else None,
    )
    c3.metric("区间", f"{result.get('days')} 天")

    fig = go.Figure()
    mode = "lines+markers" if len(df) <= 2 else "lines"
    fig.add_trace(
        go.Scatter(
            x=df["day"],
            y=df["total_value"],
            mode=mode,
            marker=dict(size=7),
            name=f"净值 ({base})",
            line=dict(width=2),
            fill="tozeroy",
            fillcolor="rgba(56, 189, 248, 0.08)",
        )
    )
    fig.update_layout(
        height=340,
        margin=dict(l=0, r=0, t=10, b=0),
        hovermode="x unified",
        yaxis=dict(tickformat=",.0f", hoverformat=",.2f"),
    )
    fig.update_xaxes(tickformat="%m-%d", hoverformat="%Y-%m-%d", nticks=6)
    if len(df) == 1:
        d0 = df["day"].iloc[0]
        fig.update_xaxes(
            range=[d0 - pd.Timedelta(days=1), d0 + pd.Timedelta(days=1)],
            tickvals=[d0],
            ticktext=[d0.strftime("%Y-%m-%d")],
        )
    st.plotly_chart(
        theme.style_chart(fig), width="stretch", config={"displayModeBar": False}
    )
    parts = [f"{first['day']} → {last['day']}"]
    if result.get("providers"):
        parts.append("数据源: " + ", ".join(result["providers"]))
    if result.get("truncated"):
        parts.append("⚠ 历史记录已截断, 只回放最近部分")
    st.caption(" · ".join(parts))


def _render_assets(result: dict) -> None:
    assets = result.get("assets") or []
    if not assets:
        return
    with st.expander("资产构成 / 定价覆盖", expanded=False):
        df = pd.DataFrame(assets)
        show = pd.DataFrame(
            {
                "代码": df["symbol"],
                "当前数量": df["quantity"],
                "当前估值(USD)": df["value_usd"].map(
                    lambda v: "—" if pd.isna(v) else f"{float(v):,.2f}"
                ),
                "价格代码": df["price_symbol"].fillna("稳定币"),
                "有历史价": df["priced"].map({True: "✅", False: "⚠"}),
                "首次出现": df["first_day"].fillna("—"),
                "最后出现": df["last_day"].fillna("—"),
            }
        )
        st.dataframe(show, width="stretch", hide_index=True)
        if not df["priced"].all():
            st.caption(
                "⚠ 无历史价的代币未计入曲线。可在 `tracker wallet-nav ... --json` "
                "的 issues 中查看具体原因。"
            )


def _render_issues(result: dict) -> None:
    issues = result.get("issues") or []
    notes = result.get("notes") or []
    if issues or notes:
        with st.expander(
            f"查询说明 ({len(issues) + len(notes)})", expanded=bool(issues)
        ):
            for msg in issues:
                st.markdown(f"- ⚠ {msg}")
            for msg in notes:
                st.markdown(f"- ℹ {msg}")


def _render_help() -> None:
    st.info(
        "输入链和地址后点「查询净值曲线」。数据来自链上转账记录: 用当前余额"
        "反推每天收盘余额, 再按历史币价与历史汇率折算。",
        icon=":material/info:",
    )
    st.markdown(
        "**零 API Key 可用**: Ethereum / Avalanche / TRON / Solana。  \n"
        "**可能需要配置**: Polygon / Arbitrum / BNB Chain —— 免费公共浏览器接口"
        "不稳定或需要 key, 可在 `wallet_history.json` 配置 `explorer_key`"
        "（Etherscan V2）或 `explorers` / `blockscout_urls` 指向自建服务。"
    )
    st.caption(
        "口径限制: 质押/解冻中的 TRX 不计入；rebasing / fee-on-transfer 代币、"
        "以及浏览器索引缺失可能造成少量偏差。曲线用于看趋势, 不作为审计口径。"
    )


def render_wallet_page() -> None:
    """页面入口 (app.py 路由 /wallet)."""
    theme.page_header("钱包净值", eyebrow="WALLET NAV")
    st.caption(
        "按链上转账记录回放单个地址的历史余额, 再折算成每日净值曲线。只读查询, 不接触私钥。"
    )

    default_base = _default_base()
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([1.1, 2.1, 0.9, 1.2], vertical_alignment="bottom")
        chain = c1.selectbox(
            "链",
            list(_CHAIN_LABELS),
            format_func=lambda c: _CHAIN_LABELS.get(c, c),
            key="wallet_nav_chain",
        )
        address = c2.text_input(
            "地址",
            placeholder="EVM: 0x… / TRON: T… / Solana: base58",
            key="wallet_nav_address",
        )
        base = c3.selectbox(
            "计价",
            _BASES,
            index=_BASES.index(default_base),
            key="wallet_nav_base",
        )
        range_label = c4.selectbox(
            "区间",
            list(_RANGES),
            index=2,
            key="wallet_nav_range",
        )
        days = _RANGES[range_label]
        st.caption(f"数据源: {_CHAIN_NOTES.get(chain, '—')}")

        if st.button(
            "查询净值曲线",
            icon=":material/monitoring:",
            type="primary",
            key="wallet_nav_query",
        ):
            signature = (chain, address.strip(), base, days)
            if not address.strip():
                st.warning("请输入链上地址/公钥")
            else:
                with st.spinner("正在读取链上转账并回放历史余额..."):
                    try:
                        result = compute_wallet_nav(
                            chain,
                            address.strip(),
                            base=base,
                            days=days,
                        )
                    except (
                        wallet_history.WalletHistoryError,
                        ValueError,
                        OSError,
                    ) as e:
                        st.session_state.pop(_SESSION_KEY, None)
                        st.error(str(e))
                    except Exception as e:  # noqa: BLE001 - 页面不暴露裸 traceback
                        st.session_state.pop(_SESSION_KEY, None)
                        st.error(f"查询失败: {e}")
                    else:
                        st.session_state[_SESSION_KEY] = {
                            "signature": signature,
                            "result": result,
                        }

    signature = (chain, address.strip(), base, days)
    payload = st.session_state.get(_SESSION_KEY)
    if payload and payload.get("signature") != signature:
        st.session_state.pop(_SESSION_KEY, None)
        st.info("查询参数已改变, 请重新点击「查询净值曲线」。")
        payload = None

    if not payload:
        _render_help()
        return

    result = payload["result"]
    _render_chart(result)
    _render_assets(result)
    _render_issues(result)
