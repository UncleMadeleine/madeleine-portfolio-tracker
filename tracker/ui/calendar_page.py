"""「日历」页面: 持仓/自选的财报与重大事件 (本月+下月起) 月历视图 + 明细表.

数据链 = tracker.events (global→yfinance 单票 / cn→akshare 整批 /
longport 兜底), 缓存三层: st.cache_data (页面内存) + 磁盘 calendar_cache
(6h 正 / 24h 负)。刷新按钮同时清页面缓存并绕过磁盘缓存。
"""

from __future__ import annotations

import calendar as _cal

from datetime import date

import pandas as pd

import streamlit as st

from tracker import events as events_mod
from tracker import storage
from tracker.services.watchlist import entries_for
from tracker.symbols import parse
from tracker.watchlist import load_watchlist

# 月历单元格单日最多展示的事件数 (超出折叠: 单元格内省略, 明细表看全集)
MAX_PER_CELL = 4

# 事件类型 → 月历单元格单字标记 (与 events.EVENT_TYPE_LABELS 中文一一对应)
TYPE_MARK = {
    "earnings": "报",
    "ex_dividend": "息",
    "record_date": "登",
    "dividend": "派",
}


def _fetch_calendar(
    symbols: tuple[str, ...], months: int, use_longport: bool, refresh: bool
):
    """真正取数 (不带名称): 名称由 _apply_names 在缓存外叠加."""
    return events_mod.fetch_calendar(
        list(symbols),
        months=months,
        refresh=refresh,
        use_longport=use_longport,
    )


@st.cache_data(ttl=1800, show_spinner=False)
def cached_calendar(symbols: tuple[str, ...], months: int, use_longport: bool):
    """日历取数 (页面内存 st.cache_data + 磁盘 calendar_cache 双层).

    名称映射不参与缓存: 改名/新增自选后立即生效, 也避免大映射进哈希键。
    刷新按钮走 _fetch_calendar(refresh=True) 直接穿透两层缓存。
    """
    return _fetch_calendar(symbols, months, use_longport, refresh=False)


def _apply_names(data: dict) -> dict:
    """把 watchlist 名称叠加到缓存结果 (返回浅拷贝, 不改动缓存对象).

    事件自带名称 (akshare/长桥) 时保留源名称; 缺失时才用 watchlist 名称补,
    这样改自选名不用等 30 分钟取数缓存过期。
    """
    evs = [dict(e) for e in (data.get("events") or [])]
    names = dict(data.get("names") or {})
    for sym, nm in _watch_names().items():
        names.setdefault(sym, nm)
    for ev in evs:
        if not ev.get("name"):
            nm = names.get(ev.get("symbol"))
            if nm:
                ev["name"] = nm
    return {**data, "events": evs, "names": names}


def _watch_names() -> dict[str, str]:
    """watchlist 名称 → {symbol: name} (纯本地读, 名称是显示增益)."""
    names: dict[str, str] = {}
    try:
        wdata = load_watchlist()
    except Exception:  # noqa: BLE001
        return names
    for e in wdata.get("watchlist", []):
        sym = str(e.get("symbol") or "").strip().upper()
        nm = str(e.get("name") or "").strip()
        if sym and nm and sym not in names:
            try:
                names[parse(sym).yahoo] = nm
            except ValueError:
                pass
    return names


def _watch_lists() -> list[str]:
    try:
        wdata = load_watchlist()
    except Exception:  # noqa: BLE001
        return []
    seen: list[str] = []
    for e in wdata.get("watchlist", []):
        for name in e.get("lists") or []:
            if name and name not in seen:
                seen.append(name)
    return seen


def _resolve_scope() -> list[str]:
    """页面范围选择 → 代码列表 (切换范围/列表/月份, 缓存键随参数联动)."""
    scope = st.session_state.get("cal_scope", "持仓+自选")
    watch_name = None
    if scope == "仅自选列表":
        chosen = st.session_state.get("cal_list")
        watch_name = chosen if chosen and chosen != "全部" else None
    try:
        holdings = [
            h
            for h in storage.load_portfolio().get("holdings", [])
            if h.get("symbol")
        ]
    except Exception:  # noqa: BLE001 - 组合缺失时自选域照旧
        holdings = []
    try:
        wentries = [
            w for w in entries_for(load_watchlist(), watch_name) if w.get("symbol")
        ]
    except Exception:  # noqa: BLE001
        wentries = []
    if scope == "仅持仓":
        scope_rows = holdings
    elif scope in ("仅自选", "仅自选列表"):
        scope_rows = wentries
    else:
        scope_rows = [*holdings, *wentries]
    syms: list[str] = []
    for e in scope_rows:
        raw = str(e["symbol"]).strip()
        try:
            syms.append(parse(raw).yahoo)
        except ValueError:
            continue
    return list(dict.fromkeys(syms))


def render_calendar_page() -> None:
    """日历页: 范围选择 → 每个自然月一张月历网格 + 明细表."""
    st.markdown("### :material/event: 财经日历")

    lists = _watch_lists()
    c1, c2, c3, c4, c5 = st.columns([2, 2, 1.4, 1.6, 1])
    with c1:
        scope_opts = (
            ["持仓+自选", "仅持仓", "仅自选"]
            if not lists
            else ["持仓+自选", "仅持仓", "仅自选", "仅自选列表"]
        )
        st.selectbox("范围", scope_opts, key="cal_scope")
    with c2:
        if st.session_state.get("cal_scope") == "仅自选列表":
            st.selectbox("自选列表", ["全部"] + lists, key="cal_list")
        else:
            st.caption("切换选择即拉取; 取数结果列内缓存 30 分钟")
    with c3:
        months = st.selectbox(
            "窗口",
            [2, 3, 4],
            index=0,
            format_func=lambda m: f"{m} 个月 (本月起)",
            key="cal_months",
        )
    use_longport = c4.checkbox(
        "长桥兜底",
        help="yfinance/akshare 拿不到的标的用长桥日历补 (需已 longport-login)",
    )
    refresh = c5.button(":material/refresh: 刷新", use_container_width=True)

    syms = _resolve_scope()
    if not syms:
        st.info("范围内没有代码 (先在「导入」或 watchlist add 里添加)。")
        return

    try:
        with st.spinner(f"拉取 {len(syms)} 只代码的财报/事件日历..."):
            if refresh:
                # 刷新: 先清页面缓存, 再直接穿透磁盘缓存取数; 本次结果不写入
                # st.cache_data (不会把刚算出的条目又立刻清掉)。
                cached_calendar.clear()
                data = _fetch_calendar(
                    tuple(syms), int(months), bool(use_longport), refresh=True
                )
            else:
                data = cached_calendar(tuple(syms), int(months), bool(use_longport))
        data = _apply_names(data)
    except Exception as e:
        st.warning(f"日历获取失败: {e}")
        return

    evs = data.get("events") or []
    issues = data.get("issues") or []
    w0 = date.fromisoformat(data["window"]["start"])
    w1 = date.fromisoformat(data["window"]["end"])
    if issues:
        st.caption("⚠ " + "; ".join(issues[:3]) + ("…" if len(issues) > 3 else ""))
    src_bits = " · ".join(
        f"{k}×{v}" for k, v in (data.get("sources") or {}).items() if v
    )
    st.caption(
        f"窗口 {w0} → {w1} · {len(evs)} 个事件 · {src_bits or '无来源'} · "
        "标记: 报=财报 息=除权除息 登=股权登记 派=派息日"
    )
    if not evs:
        st.info("窗口内无财报/分红事件。")
        return

    _render_month_grids(evs, w0, w1)
    st.divider()
    _render_detail_table(evs)


def _render_month_grids(evs: list[dict], w0: date, w1: date) -> None:
    """每个自然月一张 7 列网格 (首行表头 + 周行; 当日高亮, 单日折叠)."""
    by_day: dict[date, list[dict]] = {}
    for e in evs:
        d = date.fromisoformat(str(e["date"])[:10])
        by_day.setdefault(d, []).append(e)
    today = date.today()
    for start, _end in events_mod._month_starts(w0, w1):
        st.markdown(f"#### {start.year} 年 {start.month} 月")
        header = st.columns(7)
        for i, wd in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            header[i].caption(wd)
        for week in _cal.monthcalendar(start.year, start.month):
            row = st.columns(7)
            for i, d in enumerate(week):
                if not d:
                    continue
                dt = date(start.year, start.month, d)
                day_evs = by_day.get(dt) or []
                if dt == today:
                    label = f":red[**{d}**]"
                elif day_evs:
                    label = f"**{d}**"
                else:
                    label = f":gray[{d}]"
                lines = [label]
                for ev in day_evs[:MAX_PER_CELL]:
                    mark = TYPE_MARK.get(ev["type"], "?")
                    lines.append(
                        f"· {mark} {ev['symbol']} {_short_name(ev.get('name'))}"
                    )
                if len(day_evs) > MAX_PER_CELL:
                    lines.append(f"(+{len(day_evs) - MAX_PER_CELL})")
                # Markdown 单换行是软换行 (渲染成空格), 用行尾两空格强制换行
                row[i].markdown("  \n".join(lines))
    return


def _render_detail_table(evs: list[dict]) -> None:
    st.markdown("**明细**")
    df = pd.DataFrame(
        [
            {
                "日期": e["date"],
                "代码": e["symbol"],
                "名称": e.get("name") or None,
                "类型": events_mod.EVENT_TYPE_LABELS.get(e["type"], e["type"]),
                "状态": events_mod.DATE_STATUS_LABELS.get(
                    e.get("date_status") or "", ""
                ),
                "说明": e.get("note") or None,
                "来源": e.get("source") or None,
            }
            for e in evs
        ]
    )
    st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "日期": st.column_config.TextColumn("日期", width="small"),
            "代码": st.column_config.TextColumn("代码", width="small"),
            "名称": st.column_config.TextColumn("名称", width="medium"),
            "类型": st.column_config.TextColumn("类型", width="small"),
            "状态": st.column_config.TextColumn("状态", width="small"),
            "说明": st.column_config.TextColumn("说明", width="large"),
            "来源": st.column_config.TextColumn("来源", width="small"),
        },
    )


def _short_name(nm: str | None, limit: int = 8) -> str:
    s = (nm or "").strip()
    if not s:
        return ""
    return s[:limit] + ("…" if len(s) > limit else "")
