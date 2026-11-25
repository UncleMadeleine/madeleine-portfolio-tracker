"""「组合」页面 (默认页): 侧边栏持仓/自选编辑 + 主区总览/明细/配置/自选提醒.

从 app.py 拆出: app.py 只做 st.navigation 路由 (每页独立 URL, 刷新不丢页),
本模块承载组合页全部渲染逻辑与行情/汇率缓存.

首屏行情走 stale-while-revalidate (见 _load_quotes): 磁盘缓存 30 分钟内的
旧数据先渲染秒开, 后台线程静默重取, 看门狗片段 (_first_screen_watchdog)
在刷新完成后 rerun 换新; 只有冷启动 (无任何可用缓存) 才阻塞转圈。汇率
同理 (见 _load_fx): akshare/CFETS 冷启 ~10s 的即期查询同样落盘先用后刷。

数据持续更新由两路互补: 看门狗片段 (页面开着) 在磁盘数据临近过期时主动
重取, 用户看到的数据滚动更新; 进程级保温线程 (tracker.services.warm,
app.py 入口调用 ensure_warmers 启动) 在没有任何页面打开时照常刷磁盘缓存,
后来者打开即是新鲜数据。两路共用刷新协调器 (services.warm.start_refresh),
同数据面不并发、失败有节流; CLI `tracker warm` 也复用同一套服务。
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st

from tracker import nav, storage
from tracker.analytics import build_view, summarize
from tracker.storage import CorruptDataError, DataFileLocked
from tracker.source_order import display_source
from tracker.symbols import parse
from .portfolio_data import (  # noqa: F401 - 兼容再导出
    QUOTE_FRESH_TTL,
    _NAV_SNAPSHOT_KEY,
    QUOTE_STALE_TTL,
    _PLAN_KEY,
    _REFRESH_MIN_INTERVAL,
    _REFRESH_POLL,
    _clear_quote_caches,
    _fetch_quotes_with_progress,
    _first_screen_watchdog,
    _load_fx,
    _load_quotes,
    _load_quotes_cached,
    _plan,
    _plan_data_age,
    _plan_key,
    _plan_needs_periodic_refresh,
    _plan_reset,
    _plan_symbol_count,
    _refresh_period,
    _refresh_state,
    _start_refresh,
    cached_fx,
    cached_quotes,
)
from .portfolio_nav import NAV_RANGES, _render_nav_section  # noqa: F401
from .portfolio_sidebar import render_sidebar
from .portfolio_format import (
    _detail_with_unpriced,
    _holdings_unchanged,
    _merged_issue_lines,
    _norm_sym,
    _pnl_color,
    _selected_symbols,
    _status_md_tag,
    _symbol_pie,
    fmt,
)
from tracker.services.watchlist import (
    build_watchlist_view,
    entries_for,
    list_names,
    merge_entries,
    sort_watchlist,
    triggered_entries,
)
from tracker.ui import settings as S, theme
from tracker.ui.issues import render_issue_summary
from tracker.ui.kline_page import set_quick_symbols

WATCHLIST_PATH = storage.WATCHLIST_PATH

# 「按代码」饼图只展示市值前 N 大的代码, 其余合并为「其他」切片
# 饼图内标注占比的阈值: 低于该占比的切片太窄, 标签会互相压字 (改看图例/hover)


def _post_import() -> None:
    """导入写盘后清行情/汇率缓存, 让新持仓立即取数."""
    _clear_quote_caches()








def _stage_delete_holdings() -> None:
    """删除按钮 on_click: 把本行选中代码暂存, 供下一次脚本 run 弹确认框.

    读取上一轮渲染写入的 ``_selected_holdings`` —— 按钮回调先于脚本
    重跑执行, 此时 dataframe 选择状态仍是用户点击时的值。
    """
    symbols = st.session_state.get("_selected_holdings") or []
    if symbols:
        st.session_state["_delete_holdings_pending"] = list(symbols)


def _stage_delete_watchlist() -> None:
    symbols = st.session_state.get("_selected_watchlist") or []
    if symbols:
        st.session_state["_delete_watchlist_pending"] = list(symbols)


def _stage_refresh(symbols_key: str) -> None:
    """「刷新选中」按钮 on_click: 只记代码, 下一次脚本 run 只回源这几只."""
    symbols = st.session_state.get(symbols_key) or []
    if symbols:
        st.session_state["_refresh_quotes_pending"] = list(symbols)


def _stage_refresh_holdings() -> None:
    _stage_refresh("_selected_holdings")


def _stage_refresh_watchlist() -> None:
    _stage_refresh("_selected_watchlist")


def _clear_delete_holdings() -> None:
    st.session_state.pop("_delete_holdings_pending", None)


def _clear_delete_watchlist() -> None:
    st.session_state.pop("_delete_watchlist_pending", None)


def _consume_context_action() -> None:
    """消费右键菜单的删除操作: 复用现有确认弹窗, 前端不直接落盘.

    trigger 由 theme.inject() 暂存; 这里只把目标代码挂到 pending 槽位, 后续
    表格渲染处的 _confirm_delete_* 弹窗负责确认与锁内读-改-写。
    """
    payload = st.session_state.pop(theme.CONTEXT_ACTION_KEY, None)
    if not isinstance(payload, dict):
        return
    action = str(payload.get("action") or "")
    symbol = _norm_sym(str(payload.get("symbol") or "").strip())
    if not symbol:
        return
    if action == "delete_watchlist":
        st.session_state["_delete_watchlist_pending"] = [symbol]
    elif action == "delete_holdings":
        st.session_state["_delete_holdings_pending"] = [symbol]


_DELETE_CONFIG = {
    "holdings": {
        "warning": "将从组合中删除以下持仓：",
        "noun": "持仓",
        "edit": storage.edit_portfolio,
        "path": lambda: storage.PORTFOLIO_PATH,
        "list_key": "holdings",
        "editor": "holdings_editor",
        "detail": "holdings_detail_select",
        "selected": "_selected_holdings",
        "pending": "_delete_holdings_pending",
        "clear_fx": True,
        "error": "持仓删除失败",
    },
    "watchlist": {
        "warning": "将从自选中删除以下代码：",
        "noun": "自选",
        "edit": storage.edit_watchlist,
        "path": lambda: WATCHLIST_PATH,
        "list_key": "watchlist",
        "editor": "watchlist_editor",
        "detail": "watchlist_detail_select",
        "selected": "_selected_watchlist",
        "pending": "_delete_watchlist_pending",
        "clear_fx": False,
        "error": "自选删除失败",
    },
}


def _confirm_delete(kind: str, symbols: list[str]) -> None:
    """删除持仓/自选的公共确认框: 锁内读-改-写, 不改动另一类数据."""
    cfg = _DELETE_CONFIG[kind]
    st.warning(cfg["warning"], icon=":material/warning:")
    st.markdown("、".join(f"**{s}**" for s in symbols))
    c1, c2 = st.columns(2)
    if c1.button("取消", width="stretch", key=f"cancel_delete_{kind}"):
        st.session_state.pop(cfg["pending"], None)
        st.rerun()
    if c2.button(
        "删除",
        type="primary",
        icon=":material/delete:",
        width="stretch",
        key=f"confirm_delete_{kind}",
    ):
        targets = set(symbols)
        try:
            with cfg["edit"](cfg["path"]()) as data:
                data[cfg["list_key"]] = [
                    row
                    for row in data.get(cfg["list_key"], [])
                    if _norm_sym(str(row.get("symbol", ""))) not in targets
                ]
        except (DataFileLocked, CorruptDataError, OSError) as e:
            st.error(f"{cfg['error']}: {e}")
            return
        _clear_quote_caches(fx=cfg["clear_fx"])
        # data_editor 里可能还留着被删行; 不清会在下次「保存」时写回。
        st.session_state.pop(cfg["editor"], None)
        st.session_state.pop(cfg["detail"], None)
        st.session_state.pop(cfg["selected"], None)
        st.session_state.pop(cfg["pending"], None)
        st.toast(f"已删除 {len(targets)} 个{cfg['noun']}")
        st.rerun()


@st.dialog("删除持仓", on_dismiss=_clear_delete_holdings)
def _confirm_delete_holdings(symbols: list[str]) -> None:
    """从主表选中行删除持仓: 弹窗确认后锁内读-改-写."""
    _confirm_delete("holdings", symbols)


@st.dialog("删除自选", on_dismiss=_clear_delete_watchlist)
def _confirm_delete_watchlist(symbols: list[str]) -> None:
    """从自选主表选中行删除条目: 弹窗确认后锁内读-改-写."""
    _confirm_delete("watchlist", symbols)





# 净值走势可选区间: label → 回看自然日数 (None = 全部)
def render_portfolio_page() -> None:
    """组合页入口: 侧边栏编辑器 + 主区渲染 (行情一次批量拉取, 持仓/自选共用).

    首屏行情经 _load_quotes 走 stale-while-revalidate: 旧缓存秒渲染 + 后台
    刷新, 不再每次打开都干等网络; 冷启动 (无缓存) 才阻塞拉取。
    """
    # 右键菜单「删除」先转成 pending 目标, 由下方现有确认弹窗处理
    _consume_context_action()
    watch = storage.load_watchlist(WATCHLIST_PATH)
    if not watch.get("watchlist"):
        watch["watchlist"] = []

    settings = storage.load_settings()
    portfolio = storage.load_portfolio(storage.PORTFOLIO_PATH)
    base = portfolio.get("base_currency", "CNY")
    prefer_akshare = settings["prefer_akshare"]
    use_ibkr = settings["use_ibkr"]
    use_longport = bool(settings.get("use_longport"))
    crypto_source = settings["crypto_source"]

    edited = render_sidebar(portfolio, watch, WATCHLIST_PATH)

    # ---- 顶部: 页头 (数据未就绪也先渲染, 加载期间不空屏) ----
    theme.page_header(
        "组合总览",
        eyebrow="PORTFOLIO OVERVIEW",
        right_html=theme.chip(
            f"更新于 {datetime.now():%m-%d %H:%M}",
            title="免费数据源有延迟 (非美股 15-30 分钟), 仅供参考",
        ),
    )

    holdings = edited.dropna(subset=["symbol"]) if not edited.empty else edited

    all_watch_entries = merge_entries(watch)
    if holdings.empty and not all_watch_entries:
        # 先清空 K线页「常用」pills 再停: 否则删掉最后一个持仓/自选后, 已打开的
        # K线页仍残留被删代码 (quick_symbols 未更新), 用户点 pill 会查已删除的标的
        set_quick_symbols([])
        st.info(
            "在左侧添加持仓或自选股并保存。支持 A股/港股/美股/德股/荷兰股/英股/加股/澳股/新加坡股/加密货币。"
        )
        st.stop()

    holding_symbols = (
        [str(s) for s in holdings["symbol"].tolist()] if not holdings.empty else []
    )
    # K线页「常用」pills 仍列全部代码 (只传代码, 不触发取数)
    set_quick_symbols(
        list(
            dict.fromkeys(
                holding_symbols + [str(e["symbol"]) for e in all_watch_entries]
            )
        )
    )

    # 查看范围前置到取数之前: 行情只拉「持仓 + 当前范围自选」。自选 600+ 时
    # 全量冷启动既慢又会把 Yahoo 打到限流 (IP 级, 连累 K线页); 选具体列表
    # 只拉该列表。行情磁盘缓存按 symbols 元组命中, 5 分钟内切回原范围秒回。
    scope = st.selectbox(
        "查看范围",
        ["全部"] + list_names(watch),
        key="watch_scope",
        help="行情拉取范围 = 持仓 + 所选列表的自选股 (K线页「常用」仍含全部代码)",
    )
    # 「刷新选中」按钮在表格下方, 回调只把代码暂存; 这里取一次即清, 本次
    # 渲染只回源这几只 (不重拉整个范围), 下轮 rerun 不会重复刷新。
    refresh_symbols = tuple(
        st.session_state.pop("_refresh_quotes_pending", ()) or ()
    )
    watch_entries = entries_for(watch, None if scope == "全部" else scope)
    fetch_symbols = tuple(
        dict.fromkeys(holding_symbols + [str(e["symbol"]) for e in watch_entries])
    )
    if scope == "全部" and len(fetch_symbols) > 100:
        st.caption(
            f":material/info: 全部范围共 {len(fetch_symbols)} 个代码; "
            "无缓存冷启动拉取需要一些时间, 选具体列表可显著加快 "
            "(行情 5 分钟内有效, 30 分钟内的旧数据先展示后后台刷新)。"
        )

    # 首屏刷新计划每次渲染重建 (行情/汇率各自判定陈旧), 看门狗片段据此刻度调度
    _plan_reset()
    quotes, errors, notes = _load_quotes(
        fetch_symbols,
        prefer_akshare,
        use_ibkr,
        use_longport,
        crypto_source,
        refresh_symbols=refresh_symbols,
    )
    if not quotes:
        st.error(
            "未能获取任何行情。请检查网络: Yahoo 需可访问 finance.yahoo.com; "
            "国内网络可勾选 akshare 优先。"
        )
        with st.expander("错误详情"):
            for k, v in errors.items():
                st.write(f"{k}: {v}")
        # 冷启动失败时 _load_quotes 已把 plan 标脏并让看门狗按最短间隔补刷;
        # 但下面的 st.stop() 会终止本页渲染, 若看门狗在 stop 之后才注册,
        # 自动重试永远不会启动, 用户只能手动刷新。
        _first_screen_watchdog()
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
        fx, fx_missing = _load_fx(base, currencies, use_ibkr)
        view, view_issues = build_view(holdings.to_dict("records"), quotes, fx)
        issues += view_issues + [f"汇率缺失: {c}" for c in fx_missing]
        summary = summarize(view)
        m = summary
    else:
        view = pd.DataFrame()
        # 无持仓 → 无汇率需求: 清空槽位与年龄, 别让上一渲染的币种参数继续参与保温
        plan = _plan()
        plan["fx"] = None
        plan["f_age"] = 0.0

    # 看门狗片段: 后台刷新完成即 rerun 换新; 无刷新任务时零开销。放在行情/汇率
    # 都判定完之后 —— 一个后台任务同时刷两者, 避免分两轮刷新
    _first_screen_watchdog()

    # 每次成功渲染都 upsert 当天快照 (主键 日期+币种, 同日覆盖): 比只在
    # 「当天首次」写一次更稳 —— 冷启动行情不全时先写的偏低值, 会在后台
    # 刷新完成后的 rerun 被修正。写失败只记日志, 不能拖垮组合页。
    # 只有编辑器内容与已落盘持仓一致时才写历史净值。用户在侧栏改了数量但
    # 没点「保存」时, 页面仍用编辑值预览, 但持久化净值不能跟着未确认的输入
    # 走 (否则 portfolio.json 还是旧持仓, nav_history.db 却记了新市值)。
    if not view.empty and _holdings_unchanged(
        holdings.to_dict("records"), portfolio.get("holdings", [])
    ):
        row = nav.record_from_summary(
            datetime.now().date().isoformat(), base, summary, view
        )
        # 值与上次写入完全一致时跳过: 页面交互/看门狗 rerun 很频繁, 同一行
        # 反复 upsert 只是无谓的磁盘写。后台刷新修正了数值 (或换了日期/币种/
        # 持仓明细) 时指纹变化, 照常覆盖。
        if row != st.session_state.get(_NAV_SNAPSHOT_KEY):
            try:
                nav.save_rows([row])
            except Exception as e:  # noqa: BLE001 - 历史净值是附加能力
                logging.getLogger(__name__).warning("历史净值保存失败: %s", e)
            else:
                st.session_state[_NAV_SNAPSHOT_KEY] = row

    wview, wissues = build_watchlist_view(watch_entries, quotes)
    issues += wissues
    trig_all = triggered_entries(wview)

    # ---- 数据来源 / 取数提示 ----
    if notes:
        for n in notes:
            st.caption(f":material/info: {n}")
    src_counts: dict[str, int] = {}
    for q in quotes.values():
        src = display_source(getattr(q, "source", None))
        if src:
            src_counts[src] = src_counts.get(src, 0) + 1
    if src_counts:
        st.caption(
            "数据来源: "
            + " · ".join(f"{k} ×{v}" for k, v in sorted(src_counts.items()))
            + " (明细见持仓/自选表「数据来源」列)",
            help=(
                "美股/港股/全球 Yahoo Finance; A股/北交所 akshare 优先 (港股 akshare 兜底); "
                "加密货币 Binance; 汇率 CFETS + yfinance。可选前置源: 长桥 / IBKR (设置页开启)。"
                "每个代码实际命中的源见持仓/自选表「数据来源」列。"
                "非美股行情一般延迟 15-30 分钟, 仅供个人参考。"
            ),
        )

    # ---- KPI 卡片 (券商 App 风格: 关键数字前置) ----
    if not view.empty:
        cov = m.get("cost_coverage")
        pnl_delta = (
            f"{m['total_pnl_pct']:+.2%} (覆盖 {cov:.0%})"
            if m["total_pnl_pct"] is not None and cov is not None and cov < 1.0
            else f"{m['total_pnl_pct']:+.2%}"
            if m["total_pnl_pct"] is not None
            else None
        )
        with st.container(horizontal=True, key="fp_kpi"):
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
                # view/wview 只含有行情的行, 缺行情持仓会从计数里消失;
                # 计数应与明细表一致, 用原始条目数。
                f"{len(holdings)} / {len(watch_entries)}",
                border=True,
            )

    # ---- 净值走势 (历史快照; 无历史时给保存/回填提示) ----
    _render_nav_section(base)

    # ---- 阈值告警前置 (最关键信息, 类似券商推送条) ----
    if not trig_all.empty:
        with st.container(border=True):
            top, more = st.columns([5, 1], vertical_alignment="center")
            scope_tag = "" if scope == "全部" else f" ({scope})"
            up_tag, down_tag = S.up_down_tags()
            top.markdown(
                f":material/notifications_active: **{len(trig_all)} 只自选触及价格阈值**{scope_tag} · "
                + " · ".join(
                    f"**:{_status_md_tag(r['status'], up_tag, down_tag)}[{r['symbol']}]** {r['status'].split(' ', 1)[1]}"
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
        # errors (取数失败) 与 issues (视图计算问题) 可能报同一代码的同一原因
        # (如无法解析的代码在 _local_quote_errors 与 build_view 各产生一条):
        # 按完整文本去重, 否则展开明细里出现两遍、标题计数也翻倍。
        detail = _merged_issue_lines(errors, issues)
        with st.expander(
            f":material/warning: 数据问题 ({len(detail)})",
            icon=":material/warning:",
        ):
            st.caption(
                "失败代码会进入 5 分钟负缓存 (避免反复撞击限流的数据源); "
                "想立刻重试, 在下方明细/自选表选中代码后点「刷新选中」, 只重取这几只。"
            )
            for line in detail:
                st.write(f"- {line}")

    tab1, tab4, tab2 = st.tabs(
        [
            ":material/table_chart: 持仓明细",
            f":material/visibility: 自选观察{' :red[🔔' + str(len(trig_all)) + ']' if len(trig_all) else ''}",
            ":material/donut_large: 资产配置",
        ]
    )
    with tab1:
        detail = _detail_with_unpriced(
            view, holdings.to_dict("records") if not holdings.empty else []
        )
        if detail.empty:
            st.info("暂无持仓数据。")
        else:
            if "source" in detail.columns:
                detail["source"] = detail["source"].map(display_source)
            styled = detail.style.map(
                _pnl_color, subset=["change_pct", "pnl", "pnl_pct", "today_pnl"]
            )
            detail_event = st.dataframe(
                styled,
                width="stretch",
                height=min(120 + 35 * len(detail), 560),
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
                key="holdings_detail_select",
                column_config={
                    "symbol": st.column_config.TextColumn(
                        "代码", pinned=True, width="small"
                    ),
                    "name": st.column_config.TextColumn("名称", width="medium"),
                    "market": st.column_config.TextColumn("市场", width="small"),
                    "currency": st.column_config.TextColumn("币种", width="small"),
                    "source": st.column_config.TextColumn(
                        "数据来源",
                        width="small",
                        help="本次行情实际命中的数据源 (长桥/IBKR 优先时标注; 缓存沿用上次来源)",
                    ),
                    "price": st.column_config.NumberColumn(
                        "现价", format="%.3f", width="small"
                    ),
                    "change_pct": st.column_config.NumberColumn(
                        "涨跌%", format="%+.2f%%", width="small"
                    ),
                    "quantity": st.column_config.NumberColumn(
                        "数量", format="%,.6f", width="small"
                    ),
                    "avg_cost": st.column_config.NumberColumn(
                        "成本(当地)", format="%.4f", width="small"
                    ),
                    "market_value": st.column_config.NumberColumn(
                        f"市值({base})", format="%,.0f", width="small"
                    ),
                    "cost": st.column_config.NumberColumn(
                        f"成本({base})", format="%,.0f", width="small"
                    ),
                    "pnl": st.column_config.NumberColumn(
                        f"盈亏({base})", format="%+,.0f", width="small"
                    ),
                    "pnl_pct": st.column_config.NumberColumn(
                        # pnl_pct 是小数比值 (0.5 = +50%), percent 预定义格式
                        # 走 Intl percent 自动乘 100; 直套 "%+.2f%%" 会小 100 倍
                        "盈亏%", format="percent", width="small"
                    ),
                    "weight": None,
                    "weight_pct": st.column_config.ProgressColumn(
                        "权重",
                        min_value=0,
                        max_value=100,
                        format="%.1f%%",
                        width="small",
                    ),
                    "today_pnl": st.column_config.NumberColumn(
                        f"今日({base})", format="%+,.0f", width="small"
                    ),
                },
            )
            selected = _selected_symbols(detail_event, detail)
            st.session_state["_selected_holdings"] = selected
            dl_l, dl_mid, dl_rf, dl_r = st.columns([1, 1, 1, 2])
            dl_l.download_button(
                "导出 CSV",
                # 与页面明细表同口径: 包含缺行情/无法估值但仍存在的持仓,
                # 否则导出会静默少行, 用户以为持仓丢了。
                detail.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"portfolio_{datetime.now():%Y%m%d}.csv",
                mime="text/csv",
                icon=":material/download:",
            )
            dl_mid.button(
                f"刷新选中{f' ({len(selected)})' if selected else ''}",
                icon=":material/refresh:",
                width="stretch",
                key="refresh_holdings_selected",
                disabled=not selected,
                on_click=_stage_refresh_holdings,
                help="只强制重取选中代码 (跳过缓存与 5 分钟负缓存), 其它代码不重新请求",
            )
            dl_rf.button(
                f"删除选中{f' ({len(selected)})' if selected else ''}",
                icon=":material/delete:",
                width="stretch",
                key="delete_holdings_selected",
                disabled=not selected,
                on_click=_stage_delete_holdings,
            )
            if selected:
                dl_r.caption("已选: " + "、".join(selected))
            else:
                dl_r.caption("点击表格行可多选，再点「刷新选中」或「删除选中」。")
            pending_holdings = st.session_state.get("_delete_holdings_pending")
            if pending_holdings:
                _confirm_delete_holdings(list(pending_holdings))

    with tab2:
        if view.empty:
            st.info("暂无持仓数据。")
        else:
            c1, c2, c3 = st.columns(3)
            with c1:
                st.plotly_chart(
                    theme.style_chart(
                        px.pie(
                            view,
                            names="market",
                            values="market_value",
                            title="按市场",
                            color_discrete_sequence=theme.CHART_COLORS,
                        )
                    ),
                    width="stretch",
                )
            with c2:
                st.plotly_chart(
                    theme.style_chart(
                        px.pie(
                            view,
                            names="currency",
                            values="market_value",
                            title="按币种",
                            color_discrete_sequence=theme.CHART_COLORS,
                        )
                    ),
                    width="stretch",
                )
            with c3:
                st.plotly_chart(_symbol_pie(view), width="stretch")
            st.plotly_chart(
                theme.style_chart(
                    px.bar(
                        view.head(15),
                        x="symbol",
                        y="market_value",
                        color="market",
                        title=f"持仓市值 Top 15 ({base})",
                        color_discrete_sequence=theme.CHART_COLORS,
                    )
                ),
                width="stretch",
            )

    with tab4:
        if not watch.get("watchlist"):
            st.info("自选为空。在左侧「自选提醒」中添加代码与价格阈值。")
        else:
            # 「查看范围」已前置到取数处 (key=watch_scope, 行情按范围拉取),
            # 视图 wview 与触发项 trig_all 均按同一范围构建, 此处只留排序
            _, sort_col = st.columns([3, 2])
            sort_mode = sort_col.selectbox(
                "排序",
                ["默认 (触发优先)", "阈值等级", "当日涨跌幅 ↓", "当日涨跌幅 ↑"],
                key="watch_sort",
                label_visibility="collapsed",
            )
            trig = trig_all
            # 批量行情失败时 wissues 可能上百条: 逐条 caption 会把自选表挤到
            # 首屏之外。这里只留一行原因计数, 完整明细在上方「数据问题」里。
            render_issue_summary(
                wissues,
                noun="自选数据问题",
                hint="详情见上方「数据问题」",
                details=False,
            )
            if not trig.empty:
                for _, r in trig.iterrows():
                    metric = r.get("metric") or "price"
                    bv = r.get("base_value")
                    if (
                        metric == "pb"
                        and bv is not None
                        and pd.notna(bv)
                    ):
                        base_txt = f"PB {bv:.2f}"
                    elif metric == "mcap" and bv is not None and pd.notna(bv):
                        base_txt = (
                            f"市值 {bv:,.0f} {r['currency']}"
                            if abs(bv) < 1e12
                            else f"市值 {bv / 1e12:,.2f}万亿 {r['currency']}"
                        )
                    else:
                        base_txt = f"现价 {r['price']:,.2f} {r['currency']}"
                    st.warning(
                        f"**{r['symbol']}** {r['status']} · {base_txt}"
                        + (f" · {r['note']}" if r["note"] else ""),
                        icon=":material/notifications_active:",
                    )
            else:
                st.caption(":material/check_circle: 自选中暂无阈值触发")
            st.caption(
                f"当前查看: {scope} · 阈值按当地货币 (基准=pb 时按市净率倍数, "
                "基准=mcap 时按总市值); I 为预警线 / II 为强提醒线; "
                "距离 = 触发还需变动百分之几 (上限为负 / 下限为正 = 已越过)。"
            )
            if not wview.empty:
                mode_map = {
                    "默认 (触发优先)": "default",
                    "阈值等级": "severity",
                    "当日涨跌幅 ↓": "change_desc",
                    "当日涨跌幅 ↑": "change_asc",
                }
                wview = sort_watchlist(wview, mode_map.get(sort_mode, "default"))
            if wview.empty:
                st.info("自选行情全部缺失 (行情源失败或代码无效), 详情见上方数据问题。")
            else:
                if "source" in wview.columns:
                    wview["source"] = wview["source"].map(display_source)
                styled_w = wview.style.map(_pnl_color, subset=["change_pct"]).map(
                    lambda v: (
                        f"color: {S.up_down_colors()[0]}; font-weight: 600"
                        if str(v).startswith(("🟠", "🔴"))
                        else (
                            "color: #f0a30a; font-weight: 600"
                            if str(v).startswith("🟡")
                            else (
                                f"color: {S.up_down_colors()[1]}; font-weight: 600"
                                if str(v).startswith("🟢")
                                else ""
                            )
                        )
                    ),
                    subset=["status"],
                )
                w_event = st.dataframe(
                    styled_w,
                    on_select="rerun",
                    selection_mode="multi-row",
                    key="watchlist_detail_select",
                    column_config={
                        "metric": st.column_config.TextColumn("基准", width="small"),
                        "source": st.column_config.TextColumn(
                            "数据来源",
                            width="small",
                            help="本次行情实际命中的数据源 (长桥/IBKR 优先时标注; 缓存沿用上次来源)",
                        ),
                        "currency": st.column_config.TextColumn("币种", width="small"),
                        "book_value": st.column_config.NumberColumn(
                            "每股净资产", format="%.2f", width="small"
                        ),
                        "market_cap": st.column_config.NumberColumn(
                            "总市值", format="%.0f", width="small"
                        ),
                        "base_value": st.column_config.NumberColumn(
                            "基准值", format="%.2f", width="small"
                        ),
                        "upper_1": st.column_config.NumberColumn(
                            "上限 I", format="%.2f", width="small"
                        ),
                        "upper_2": st.column_config.NumberColumn(
                            "上限 II", format="%.2f", width="small"
                        ),
                        "lower_1": st.column_config.NumberColumn(
                            "下限 I", format="%.2f", width="small"
                        ),
                        "lower_2": st.column_config.NumberColumn(
                            "下限 II", format="%.2f", width="small"
                        ),
                        "status": st.column_config.TextColumn("状态", width="small"),
                        "change_pct": st.column_config.NumberColumn(
                            "涨跌%", format="%+.2f%%", width="small"
                        ),
                        "dist_upper_1_pct": st.column_config.NumberColumn(
                            "距上限 I %", format="%.1f", width="small"
                        ),
                        "dist_upper_2_pct": st.column_config.NumberColumn(
                            "距上限 II %", format="%.1f", width="small"
                        ),
                        "dist_lower_1_pct": st.column_config.NumberColumn(
                            "距下限 I %", format="%.1f", width="small"
                        ),
                        "dist_lower_2_pct": st.column_config.NumberColumn(
                            "距下限 II %", format="%.1f", width="small"
                        ),
                        "note": st.column_config.TextColumn("备注", width="medium"),
                        "triggered": None,
                    },
                )
                w_selected = _selected_symbols(w_event, wview)
                st.session_state["_selected_watchlist"] = w_selected
                wc1, wc_rf, wc2 = st.columns([1, 1, 3])
                wc1.button(
                    f"删除选中{f' ({len(w_selected)})' if w_selected else ''}",
                    icon=":material/delete:",
                    width="stretch",
                    key="delete_watchlist_selected",
                    disabled=not w_selected,
                    on_click=_stage_delete_watchlist,
                )
                wc_rf.button(
                    f"刷新选中{f' ({len(w_selected)})' if w_selected else ''}",
                    icon=":material/refresh:",
                    width="stretch",
                    key="refresh_watchlist_selected",
                    disabled=not w_selected,
                    on_click=_stage_refresh_watchlist,
                    help="只强制重取选中代码 (跳过缓存与 5 分钟负缓存), 其它代码不重新请求",
                )
                if w_selected:
                    wc2.caption("已选: " + "、".join(w_selected))
                else:
                    wc2.caption("点击表格行可多选，再点「刷新选中」或「删除选中」。")
                pending_watchlist = st.session_state.get("_delete_watchlist_pending")
                if pending_watchlist:
                    _confirm_delete_watchlist(list(pending_watchlist))
