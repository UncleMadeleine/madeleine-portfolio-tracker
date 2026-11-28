"""组合页侧边栏: 持仓编辑器 + 自选提醒编辑器."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from tracker import storage
from tracker.services.watchlist import list_names
from tracker.storage import CorruptDataError, DataFileLocked
from tracker.symbols import parse
from tracker.watchlist import normalize_watch_entry, parse_lists

from .portfolio_data import _clear_quote_caches
from .portfolio_format import _norm_sym


def render_sidebar(portfolio: dict, watch: dict, watchlist_path) -> pd.DataFrame:
    """渲染侧边栏, 返回持仓编辑器当前行 (供主区估值使用)."""
    with st.sidebar:
        st.markdown(
            ":material/candlestick_chart: **组合追踪**",
            help="多市场持仓 + 自选提醒 · 数据源按域路由: 全球 yfinance / A股 akshare / 加密 Binance; 可选长桥/IBKR 前置 (设置页)",
        )
        with st.expander(":material/edit_note: 持仓管理", expanded=True):
            st.caption(
                "代码规范: AAPL · SAP.DE · PRX.AS · BP.L · RY.TO · BHP.AX · 0700.HK · 600519.SS · BTC-USD"
            )
            df_h = pd.DataFrame(
                portfolio.get("holdings", []),
                columns=["symbol", "quantity", "avg_cost"],
            )
            edited = st.data_editor(
                df_h,
                num_rows="dynamic",
                key="holdings_editor",
                column_config={
                    "symbol": st.column_config.TextColumn(
                        "代码", help="Yahoo 规范代码"
                    ),
                    "quantity": st.column_config.NumberColumn(
                        "数量", help="可填负数表示空头 (IBKR 空头持仓)"
                    ),
                    "avg_cost": st.column_config.NumberColumn(
                        "成本 (当地货币)", min_value=0.0
                    ),
                },
            )
            c1, c2 = st.columns(2)
            if c1.button(
                "保存", icon=":material/save:", width="stretch", key="save_holdings"
            ):
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
                    # 锁内读-改-写: 读盘 (溯源键来源) 与写盘在同一临界区,
                    # 并发的导入/其它页面编辑不会互相覆盖 (旧写法 load→改→save
                    # 全量替换, 窗口内的并发写入会被静默丢掉)
                    try:
                        with storage.edit_portfolio(storage.PORTFOLIO_PATH) as pf:
                            # 保留编辑器不展示的溯源键 (如钱包导入的 import_source),
                            # 否则页面保存一次就丢掉, 后续多钱包导入会退化成同代码互相覆盖
                            extra = {
                                _norm_sym(h.get("symbol", "")): {
                                    k: v
                                    for k, v in h.items()
                                    if k
                                    not in ("symbol", "quantity", "avg_cost", "type")
                                }
                                for h in pf.get("holdings", [])
                            }
                            for r in clean:
                                r.update(extra.get(r["symbol"], {}))
                                # 手改过数量: 来源分量已与 quantity 不一致, 作废分量表
                                # (否则下次钱包导入会把旧分量加回来, 数量虚增);
                                # 未改数量时保留分量表, 多钱包来源记账不丢
                                q = r.get("quantity")
                                q = float(q) if isinstance(q, (int, float)) else 0.0
                                sq = r.get("source_quantities")
                                if (
                                    isinstance(sq, dict)
                                    and sq
                                    and q != sum(float(v or 0) for v in sq.values())
                                ):
                                    r.pop("source_quantities", None)
                                    r["import_source"] = r.get("import_source") or ""
                            pf["holdings"] = clean
                            # 基础货币由「设置」页单独维护: 这里不写回渲染开始时
                            # 读到的旧 base, 否则另一标签页刚改的设置会被持仓保存
                            # 用陈旧快照覆盖 (即使本次保存已持锁)。
                    except (DataFileLocked, CorruptDataError, OSError) as e:
                        st.error(f"持仓保存失败: {e}")
                        return
                    _clear_quote_caches()
                    # 必须 rerun: 本次渲染的 holdings 仍是 data_editor 原始行
                    # (含重复行), 而落盘的 clean 已去重剔除 —— 不 rerun 的话
                    # 同一次渲染里的 KPI/明细表按未去重行计算, 重复代码市值
                    # 当场双计, 与保存提示自相矛盾 (自选保存分支同款处理)
                    st.toast("持仓已保存" + (" (重复行已移除)" if dups else ""))
                    st.rerun()
            if c2.button(
                "重载",
                icon=":material/refresh:",
                width="stretch",
                key="reload_holdings",
            ):
                st.session_state.pop("holdings_editor", None)
                st.rerun()

        with st.expander(":material/notifications: 自选提醒", expanded=False):
            if not watch.get("watchlist"):
                watch["watchlist"] = []
            st.caption(
                "同一个代码可属多个列表（「所属列表」列用逗号分隔，如: 科技,美股）。"
                "阈值按当地货币; 两级: upper_1/upper_2、lower_1/lower_2; 可只设一侧。"
                "指标基准 metric: price=现价 (默认) / pb=市净率 (阈值按 PB 倍数) / "
                "mcap=总市值 (阈值按当地货币绝对金额)。"
                "旧格式自动迁移。"
            )
            st.caption("现有列表: " + "、".join(list_names(watch)))
            entries = watch.get("watchlist", [])
            df_w = pd.DataFrame(
                entries,
                columns=[
                    "symbol",
                    "lists",
                    "metric",
                    "upper_1",
                    "upper_2",
                    "lower_1",
                    "lower_2",
                    "pb",
                    "mcap",
                    "note",
                ],
            )
            if not df_w.empty and "lists" in df_w:
                df_w["lists"] = df_w["lists"].apply(
                    lambda v: (
                        ", ".join(v) if isinstance(v, list) else (str(v) if v else "")
                    )
                )
                # note 全缺失时 pandas 3 推成 float64, 与 TextColumn 编辑冲突
                df_w["note"] = df_w["note"].astype(str).replace("nan", "")
            edited_w = st.data_editor(
                df_w,
                num_rows="dynamic",
                key="watchlist_editor",
                column_config={
                    "symbol": st.column_config.TextColumn(
                        "代码", help="Yahoo 规范代码"
                    ),
                    "lists": st.column_config.TextColumn("所属列表 (逗号分隔)"),
                    "metric": st.column_config.SelectboxColumn(
                        "指标基准",
                        options=["", "price", "pb", "mcap"],
                        help="price=现价 (默认); pb=市净率; mcap=总市值",
                    ),
                    "upper_1": st.column_config.NumberColumn("上限 I", format="%.2f"),
                    "upper_2": st.column_config.NumberColumn("上限 II", format="%.2f"),
                    "lower_1": st.column_config.NumberColumn("下限 I", format="%.2f"),
                    "lower_2": st.column_config.NumberColumn("下限 II", format="%.2f"),
                    "mcap": st.column_config.NumberColumn(
                        "市值覆盖",
                        format="%.0f",
                        help="手写总市值 (当地货币), 覆盖行情/数据源取值; 仅 metric=mcap 时生效",
                    ),
                    "pb": st.column_config.NumberColumn(
                        "每股净资产覆盖",
                        format="%.4f",
                        help="手写每股净资产 (BVPS), 覆盖行情/数据源取值; 仅 metric=pb 时生效",
                    ),
                    "note": st.column_config.TextColumn("备注"),
                },
            )
            c3, c4 = st.columns(2)
            if c3.button(
                "保存",
                icon=":material/save:",
                width="stretch",
                key="save_watchlist",
            ):
                # 编辑器只展示固定列: 保存时保留未展示的字段 (如手写的 name,
                # 脚本/第三方追加的自定义键), 否则一次 UI 保存就会静默丢数据
                # (日历页的名称叠加正是读取 name 字段)。
                editor_keys = {
                    "symbol",
                    "lists",
                    "metric",
                    "upper_1",
                    "upper_2",
                    "lower_1",
                    "lower_2",
                    "pb",
                    "mcap",
                    "note",
                    "type",
                }
                extra = {
                    _norm_sym(e.get("symbol", "")): {
                        k: v for k, v in e.items() if k not in editor_keys
                    }
                    for e in entries
                }
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
                    # 指标基准单元格留空 = price (缺省): 显式钉死。否则
                    # normalize_watch_entry 会按 mcap/pb 覆盖键把基准推断为
                    # mcap/pb, 与「市值覆盖仅 metric=mcap 时生效」的列说明相反,
                    # 价格阈值会被当成市值/市净率比较 → 每次误报
                    m_raw = r.get("metric")
                    if m_raw is None or (
                        isinstance(m_raw, float) and pd.isna(m_raw)
                    ):
                        m_raw = ""
                    metric = str(m_raw).strip().lower()
                    r = {
                        **r,
                        "metric": metric if metric in ("price", "pb", "mcap") else "price",
                    }
                    e = normalize_watch_entry(r)
                    e["symbol"] = key
                    e["lists"] = parse_lists(e.get("lists"))
                    e = {**extra.get(key, {}), **e}
                    # data_editor 清空单元格会产生 NaN; note 若是 NaN 会让
                    # save_watchlist 的 allow_nan=False 直接抛 ValueError
                    for k in (
                        "metric",
                        "upper_1",
                        "upper_2",
                        "lower_1",
                        "lower_2",
                        "pb",
                        "mcap",
                        "note",
                    ):
                        v = e.get(k)
                        if (
                            v is None
                            or v == ""
                            or (isinstance(v, float) and pd.isna(v))
                        ):
                            e.pop(k, None)
                    rows.append(e)
                if bad or dups:
                    if bad:
                        st.error(f"无法识别: {', '.join(bad)}")
                    if dups:
                        st.error(f"重复代码 (已去重): {', '.join(sorted(set(dups)))}")
                if not bad:
                    try:
                        with storage.edit_watchlist(watchlist_path) as wl:
                            wl["watchlist"] = rows
                    except (DataFileLocked, CorruptDataError, OSError) as e:
                        st.error(f"自选保存失败: {e}")
                        return
                    _clear_quote_caches(fx=False)
                    if dups:
                        st.toast("自选已保存 (重复行已移除)")
                    else:
                        st.toast("自选已保存")
                    # 本次 run 的 watch 是脚本开头从磁盘读的旧快照: 保存后不 rerun
                    # 的话, 下方自选表/自选范围仍用旧数据, 且 cached_quotes.clear()
                    # 让本次按旧符号集重取 → 新代码 quotes.get 为 None, 冒出一条
                    # 幻觉的「行情缺失」告警。rerun 后 watch 重新读盘即一致。
                    st.rerun()
            if c4.button(
                "重载",
                icon=":material/refresh:",
                width="stretch",
                key="reload_watchlist",
            ):
                st.session_state.pop("watchlist_editor", None)
                st.rerun()


    return edited
