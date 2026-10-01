"""「导入」页面: IBKR 账户 / 链上钱包 / 券商文件 三种来源统一入口.

每个来源独立 查询/解析 → 预览 → 确认导入; 导入方式全页共享:
  追加合并  按代码更新数量/成本, 新代码追加, 其余持仓保留
  覆盖全部  清空现有持仓后写入 (写前自动备份 portfolio.json.bak)

合并与写盘逻辑在 tracker.importer, 与 CLI `tracker import <来源>` 一致.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from tracker import importer, longport as longport_mod, storage

_CHAIN_LABELS = {
    "eth": "Ethereum",
    "bsc": "BNB Chain",
    "polygon": "Polygon",
    "arbitrum": "Arbitrum",
    "avalanche": "Avalanche",
    "tron": "TRON",
}
_MODE_LABELS = {
    importer.MODE_APPEND: "追加合并",
    importer.MODE_OVERWRITE: "覆盖全部",
}
_WALLET_BASES = ["USD", "USDT", "USDC", "EUR", "CNY"]


def render_import_page(on_saved=None) -> None:
    """导入页入口; on_saved: 写入成功后的回调 (app.py 用于清行情缓存)."""
    st.markdown("### :material/download: 数据导入")
    st.caption(
        "三个来源可分别导入: 「追加合并」按代码更新数量/成本并追加新代码 "
        "(其余持仓保留); 「覆盖全部」清空后重写 (写前自动备份 portfolio.json.bak)。"
    )
    choice = st.segmented_control(
        "导入方式",
        list(_MODE_LABELS.values()),
        default=_MODE_LABELS[importer.MODE_APPEND],
        key="import_mode",
    )
    mode = (
        importer.MODE_OVERWRITE
        if choice == _MODE_LABELS[importer.MODE_OVERWRITE]
        else importer.MODE_APPEND
    )
    if mode == importer.MODE_OVERWRITE:
        st.warning(
            "覆盖模式将清空现有持仓再写入, 写前自动备份 portfolio.json.bak。",
            icon=":material/warning:",
        )

    tab_ibkr, tab_lp, tab_wallet, tab_file = st.tabs(
        [
            ":material/account_balance: IBKR 账户",
            ":material/candlestick_chart: 长桥账户",
            ":material/account_balance_wallet: 链上钱包",
            ":material/description: 券商文件",
        ]
    )
    with tab_ibkr:
        _ibkr_section(mode, on_saved)
    with tab_lp:
        _longport_section(mode, on_saved)
    with tab_wallet:
        _wallet_section(mode, on_saved)
    with tab_file:
        _file_section(mode, on_saved)


# ---------------------------------------------------------------------------
# IBKR 账户
# ---------------------------------------------------------------------------


def _ibkr_section(mode: str, on_saved) -> None:
    st.caption("从本机 IB Gateway / TWS 读取账户股票持仓 (连接参数见 ibkr.json)。")
    gw = st.selectbox(
        "Gateway 模式",
        ["按配置", "paper 模拟 (4002)", "live 实盘 (4001)"],
        key="imp_ibkr_gw",
        help="对应 ibkr.json 的 mode; 「按配置」不覆盖配置文件",
    )
    if st.button("获取账户持仓", icon=":material/sync:", key="imp_ibkr_fetch"):
        gw_mode = {"paper 模拟 (4002)": "paper", "live 实盘 (4001)": "live"}.get(gw)
        with st.spinner("正在连接 IB Gateway..."):
            try:
                rows, skipped = importer.collect_ibkr(mode=gw_mode)
            except Exception as e:
                st.session_state.pop("imp_ibkr", None)
                st.error(f"{e} — 请确认 IB Gateway 已登录运行, 且 API 连接已启用。")
            else:
                st.session_state["imp_ibkr"] = {"rows": rows, "skipped": skipped}
    _preview_and_confirm(
        "imp_ibkr",
        mode,
        on_saved,
        note="avg_cost 为 IBKR 报告的合约货币每股均价 (含佣金), 仅供估算。",
    )


def _longport_section(mode: str, on_saved) -> None:
    st.caption(
        "从长桥 (LongPort) 账户读取证券持仓 (港股/美股/A股)。首次使用需登录长桥账户"
        " (OAuth 浏览器授权, token 本地保存并自动刷新); 连接配置见 longport.json。"
    )
    from .longport_login import render_login_section

    render_login_section(key_prefix="imp_lp")
    st.divider()
    if st.button("获取账户持仓", icon=":material/sync:", key="imp_lp_fetch"):
        with st.spinner("正在读取长桥账户..."):
            try:
                rows, skipped = importer.collect_longport()
            except Exception as e:
                st.session_state.pop("imp_longport", None)
                st.error(f"{e} — 请先完成上方登录, 或检查 longport.json 配置。")
            else:
                st.session_state["imp_longport"] = {"rows": rows, "skipped": skipped}
    _preview_and_confirm(
        "imp_longport",
        mode,
        on_saved,
        note="avg_cost 为长桥报告的合约货币成本价 (按账户设置的平均/摊薄口径), 仅供估算。",
    )

    st.divider()
    _longport_watchlist_section()


def _longport_watchlist_section() -> None:
    """长桥自选分组 → watchlist.json 导入 (独立于持仓导入的写盘目标)."""
    st.caption(
        "导入长桥账户**自选分组**: 每个分组映射为自选的一个列表, 代码归一为 "
        "Yahoo 规范 (700.HK→0700.HK / TAP.A.US→TAP-A 等); 已有条目的阈值/备注"
        "保留, 仅并列表归属; 重复导入幂等。"
    )
    if st.button(
        "获取自选分组", icon=":material/star:", key="imp_lp_wl_fetch"
    ):
        with st.spinner("正在读取长桥自选..."):
            try:
                groups = longport_mod.fetch_watchlist_groups()
                rows, skipped = longport_mod.watchlist_to_rows(groups)
            except Exception as e:
                st.session_state.pop("imp_lp_wl", None)
                st.error(f"{e} — 请先完成上方登录, 或检查 longport.json 配置。")
            else:
                st.session_state["imp_lp_wl"] = {
                    "rows": rows,
                    "skipped": skipped,
                    "groups": len(groups),
                }
    payload = st.session_state.get("imp_lp_wl")
    if not payload:
        return
    for s in payload.get("skipped", []):
        st.caption(f":material/warning: {s}")
    rows = payload.get("rows", [])
    if not rows:
        st.caption("未发现可导入的自选。")
        return
    df = pd.DataFrame(rows)
    df["lists"] = df["lists"].apply(lambda v: ", ".join(v))
    st.dataframe(df, width="stretch", hide_index=True)
    st.caption(f"共 {payload.get('groups', '?')} 组 {len(rows)} 个规范代码。")
    if st.button(
        f"确认导入自选 ({_MODE_LABELS[importer.MODE_APPEND]})",
        icon=":material/star_outline:",
        key="imp_lp_wl_confirm",
    ):
        data = storage.load_watchlist(storage.WATCHLIST_PATH)
        merged, stats = importer.merge_watchlist(
            data.get("watchlist", []), rows, importer.MODE_APPEND
        )
        storage.backup_file(storage.WATCHLIST_PATH)
        data["watchlist"] = merged
        storage.save_watchlist(data)
        st.session_state.pop("imp_lp_wl", None)
        st.toast(
            f"自选导入完成: 新增 {len(stats['added'])} · "
            f"更新 {len(stats['updated'])}, 共 {len(merged)} 条"
        )
        st.rerun()


def _wallet_section(mode: str, on_saved) -> None:
    st.caption(
        "通过链上公钥/地址查询余额后导入组合。仅读操作, 无需私钥。"
        "支持: eth, bsc, polygon, arbitrum, avalanche, tron"
    )
    from tracker import wallet as wallet_mod

    w_chain = st.selectbox(
        "链",
        sorted(wallet_mod._SUPPORTED_CHAINS),
        format_func=lambda c: _CHAIN_LABELS.get(c, c),
        key="imp_wallet_chain",
    )
    w_addr = st.text_input(
        "地址 (EVM: 0x+40位hex; TRON: 'T'开头34字符)",
        placeholder="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        key="imp_wallet_addr",
    )
    w_base = st.selectbox("计价货币", _WALLET_BASES, key="imp_wallet_base")
    if st.button("查询余额", icon=":material/search:", key="imp_wallet_query"):
        if not w_addr.strip():
            st.warning("请输入链上地址")
        else:
            with st.spinner(f"正在查询 {w_chain} 链..."):
                try:
                    rows, errors, raw = importer.collect_wallet(
                        w_chain, w_addr.strip(), base_currency=w_base
                    )
                except ValueError as e:
                    st.session_state.pop("imp_wallet", None)
                    st.error(str(e))
                except (RuntimeError, FileNotFoundError) as e:
                    st.session_state.pop("imp_wallet", None)
                    st.error(f"RPC 查询失败: {e}")
                else:
                    st.session_state["imp_wallet"] = {
                        "rows": rows,
                        "skipped": errors,
                        "preview": raw.get("holdings", []),
                    }
    _preview_and_confirm(
        "imp_wallet",
        mode,
        on_saved,
        note="avg_cost 未设置, 导入后请在持仓管理中补填。",
    )


# ---------------------------------------------------------------------------
# 券商文件
# ---------------------------------------------------------------------------


def _file_section(mode: str, on_saved) -> None:
    st.caption(
        "导入券商客户端导出的持仓文件 (同花顺/通达信/华泰/东财/QMT 等, "
        "CSV 或 Excel); A股代码自动映射为 Yahoo 后缀 (.SS/.SZ/.BJ)。"
    )
    up = st.file_uploader("持仓文件", type=["csv", "xlsx", "xls"], key="imp_file_up")
    if up is None:
        st.session_state.pop("imp_file", None)
        st.session_state.pop("imp_file_sig", None)
        return
    sig = (up.name, up.size)
    if st.session_state.get("imp_file_sig") != sig:
        suffix = Path(up.name).suffix or ".csv"
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(up.getbuffer())
                tmp_path = tmp.name
            rows, skipped = importer.collect_ashare_file(tmp_path)
        except Exception as e:
            rows, skipped = [], [str(e)]
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
        st.session_state["imp_file"] = {"rows": rows, "skipped": skipped}
        st.session_state["imp_file_sig"] = sig
    _preview_and_confirm(
        "imp_file",
        mode,
        on_saved,
        note="avg_cost 为券商导出的成本价 (当地货币), 仅供估算。",
    )


# ---------------------------------------------------------------------------
# 预览 + 确认导入 (三来源共用)
# ---------------------------------------------------------------------------


def _preview_and_confirm(state_key: str, mode: str, on_saved, note: str = "") -> None:
    payload = st.session_state.get(state_key)
    if not payload:
        return
    rows = payload.get("rows", [])
    for s in payload.get("skipped", []):
        st.caption(f":material/warning: {s}")
    if not rows:
        st.caption("未发现可导入的持仓。")
        return
    st.dataframe(
        pd.DataFrame(payload.get("preview") or rows),
        width="stretch",
        hide_index=True,
    )
    if note:
        st.caption(note)
    if st.button(
        f"确认导入 ({_MODE_LABELS[mode]})",
        icon=":material/download_done:",
        key=f"{state_key}_confirm",
    ):
        data = storage.load_portfolio(storage.PORTFOLIO_PATH)
        merged, stats = importer.merge_holdings(data.get("holdings", []), rows, mode)
        storage.backup_file(storage.PORTFOLIO_PATH)
        data["holdings"] = merged
        storage.save_portfolio(data)
        st.session_state.pop(state_key, None)
        if callable(on_saved):
            on_saved()
        st.toast(
            f"{_MODE_LABELS[mode]}完成: 新增 {len(stats['added'])} · "
            f"更新 {len(stats['updated'])}, 共 {len(merged)} 条持仓"
        )
        st.rerun()
