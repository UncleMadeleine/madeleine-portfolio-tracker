"""「日历」页面: 持仓/自选的财报与重大事件 (本月+下月起) 月历视图 + 明细表.

数据链 = tracker.events (global→yfinance 单票 / cn→akshare 整批 /
longport 兜底), 缓存三层: st.cache_data (页面内存) + 磁盘 calendar_cache
(6h 正 / 24h 负)。刷新按钮同时清页面缓存并绕过磁盘缓存。

首屏 stale-while-revalidate: 先只读磁盘 (7 天内的旧日历也秒开), 过期/缺失
键交给后台线程按正常缓存链补齐, 看门狗片段在完成后 rerun 换新; 完全没有
缓存时才阻塞取数, 并显示 A股整批/海外单票的实时进度条。

进程级缓存预热: app 入口调用 tracker.services.warm.ensure_warmers(), 服务
启动后按默认范围 (持仓+自选, 本月+下月) 每 30 分钟走一次正常缓存链, 让日历
缓存常新; 与页面刷新共用协调器, 同 key 不会重复发网络。CLI `tracker warm`
也可独立触发同一套预热逻辑, 不必启动 Streamlit。
"""

from __future__ import annotations

import calendar as _cal
import html
import time

from datetime import date, timedelta

import pandas as pd

import streamlit as st

from tracker import events as events_mod
from tracker import storage
from tracker.ics import build_ics, ics_filename
from tracker.services import warm as warm_mod
from tracker.services.watchlist import entries_for
from tracker.symbols import parse
from tracker.ui import theme
from tracker.watchlist import load_watchlist

# 月历单元格单日最多展示的事件数 (超出折叠: 单元格内省略, 日程/明细表看全集)
MAX_PER_CELL = 3

# 首屏 stale-while-revalidate: 磁盘有旧日历 (7 天内) 时先渲染, 后台按正常
# 缓存链重取过期/缺失的键, 完成后看门狗 rerun 换新。周期/节流与组合页同口径。
CAL_REFRESH_POLL = 2.0
CAL_REFRESH_MIN_INTERVAL = warm_mod.CAL_REFRESH_MIN_INTERVAL
_CAL_SEEN_KEY = "_cal_refresh_seen"

# 进程级缓存预热实现见 tracker/services/warm.py (无 Streamlit 依赖, CLI
# `tracker warm` 共用同一套协调器); 页面只保留默认窗口常量给文案/测试引用。
CAL_WARM_PERIOD = warm_mod.CAL_WARM_PERIOD
CAL_WARM_FIRST_DELAY = warm_mod.CAL_WARM_FIRST_DELAY
CAL_WARM_MONTHS = warm_mod.CAL_WARM_MONTHS
CAL_WARM_USE_LONGPORT = warm_mod.CAL_WARM_USE_LONGPORT

# 事件类型 → 月历单元格单字标记 (与 events.EVENT_TYPE_LABELS 中文一一对应)
TYPE_MARK = {
    "earnings": "报",
    "ex_dividend": "息",
    "record_date": "登",
    "dividend": "派",
}

# 事件类型展示顺序/配色类名 (CSS 中 fp-ev-* 定义 --ev 主色)
TYPE_ORDER = ("earnings", "ex_dividend", "record_date", "dividend")
TYPE_RANK = {t: i for i, t in enumerate(TYPE_ORDER)}
WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def _fetch_calendar(
    symbols: tuple[str, ...], months: int, use_longport: bool, refresh: bool,
    progress=None,
):
    """真正取数 (不带名称): 名称由 _apply_names 在缓存外叠加."""
    return events_mod.fetch_calendar(
        list(symbols),
        months=months,
        refresh=refresh,
        use_longport=use_longport,
        progress=progress,
    )


def _peek_calendar(
    symbols: tuple[str, ...], months: int, use_longport: bool
):
    """只读磁盘缓存 (绝不发网络); 无任何可用缓存行时返回 None.

    页面首屏先走这里: 7 天内的旧日历也先展示 (stale-while-revalidate),
    缺失/过期键由 _calendar_watchdog 在后台按正常缓存链补齐。
    """
    return events_mod.fetch_calendar(
        list(symbols),
        months=months,
        use_longport=use_longport,
        stale_ok=True,
        cache_only=True,
    )


@st.cache_data(ttl=300, show_spinner=False)
def cached_calendar(
    symbols: tuple[str, ...], months: int, use_longport: bool
):
    """日历缓存探针 (页面内存 5 分钟 + 磁盘 calendar_cache 双层).

    只读缓存、不阻塞网络: 冷启动返回 None, 由页面走带进度条的阻塞取数;
    有旧数据则秒回并触发后台刷新。名称映射不参与缓存 (由 _apply_names 叠加),
    刷新完成/手动刷新时 clear() 让下一次 rerun 重新读盘。
    """
    return _peek_calendar(symbols, months, use_longport)


def _cache_meta(data: dict | None) -> dict:
    """安全读取结果里的 cache 元数据 (测试/旧缓存结果可能没有该键)."""
    return (data or {}).get("cache") or {}


def _clear_calendar_cache() -> None:
    """清页面探针缓存; 兼容测试里 monkeypatch 成普通函数 (无 .clear) 的情况."""
    clear = getattr(cached_calendar, "clear", None)
    if callable(clear):
        clear()


def _cal_key(symbols, months: int, use_longport: bool) -> tuple:
    return warm_mod.calendar_key(symbols, months, use_longport)


def _start_calendar_refresh(
    key: tuple, symbols: tuple[str, ...], months: int, use_longport: bool
) -> bool:
    """启动一次后台日历重取 (实现与进程级预热共用 services.warm)."""
    return warm_mod.start_calendar_refresh(key, symbols, months, use_longport)


@st.fragment(run_every=CAL_REFRESH_POLL)
def _calendar_watchdog(
    key: tuple, symbols: tuple[str, ...], months: int, use_longport: bool
) -> None:
    """首屏 SWR 看门狗: 刷新完成即清页面缓存并 rerun 换新.

    - 后台进行中: 只显示一条状态 caption;
    - 完成事件按 key 只消费一次 (防热循环), 之后若数据仍陈旧才按最短
      间隔补刷 (全部源失败/无数据时不会每 2s 重试)。
    """
    inflight, done = warm_mod.calendar_refresh_state(key)
    seen = st.session_state.setdefault(_CAL_SEEN_KEY, {})
    if inflight:
        st.caption(":material/sync: 日历后台刷新中, 先展示缓存数据…")
        return
    if done is not None and done > seen.get(key, 0.0):
        seen[key] = done
        cached_calendar.clear()
        st.rerun()
        return
    if done is None or time.time() - done >= CAL_REFRESH_MIN_INTERVAL:
        if _start_calendar_refresh(key, symbols, months, use_longport):
            st.caption(":material/sync: 日历后台刷新中, 先展示缓存数据…")


def _fetch_calendar_with_progress(
    symbols: tuple[str, ...], months: int, use_longport: bool, refresh: bool
):
    """阻塞取数 + 进度条 (冷启动/手动刷新路径)."""
    bar = st.progress(
        0.0, text=f"准备拉取 {len(symbols)} 只代码的财经日历..."
    )
    last = {"frac": -1.0, "at": 0.0}

    def _on_progress(done, total, label) -> None:
        total = max(int(total or 0), int(done or 0), 1)
        frac = min(max(float(done) / total, 0.0), 1.0)
        now = time.monotonic()
        # 逐代码回调可能上百次: 增幅 <1% 且距上次 <0.2s 时跳过, 防刷爆前端
        if (
            frac < 1.0
            and frac - last["frac"] < 0.01
            and now - last["at"] < 0.2
        ):
            return
        last["frac"], last["at"] = frac, now
        text = f"日历取数 {done}/{total}"
        if label:
            text += f" · {label}"
        bar.progress(frac, text=text)

    try:
        return _fetch_calendar(
            symbols, months, use_longport, refresh, progress=_on_progress
        )
    finally:
        bar.empty()


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


def _rows_to_symbols(rows: list[dict]) -> list[str]:
    """持仓/自选行 → 规范 Yahoo 代码 (去重保序; 无法识别的静默跳过)."""
    syms: list[str] = []
    for e in rows:
        raw = str(e.get("symbol") or "").strip()
        if not raw:
            continue
        try:
            syms.append(parse(raw).yahoo)
        except ValueError:
            continue
    return list(dict.fromkeys(syms))


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
    return _rows_to_symbols(scope_rows)


def render_calendar_page() -> None:
    """日历页: 工具栏 → 概览 → 月历卡 → 按日日程 → 完整表格."""
    theme.page_header("财经日历", eyebrow="ECONOMIC CALENDAR")

    lists = _watch_lists()
    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns(
            [2, 2, 1.5, 1.5, 1], vertical_alignment="bottom"
        )
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
                st.caption("旧数据先展示, 后台自动刷新 · 磁盘缓存 6 小时")
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
        refresh = c5.button(
            ":material/refresh: 刷新", use_container_width=True, type="primary"
        )

    syms = _resolve_scope()
    if not syms:
        st.info("范围内没有代码 (先在「导入」或 watchlist add 里添加)。")
        return

    key = _cal_key(syms, int(months), bool(use_longport))
    try:
        if refresh:
            # 刷新: 先清页面缓存, 再直接穿透磁盘缓存取数; 本次结果不写入
            # st.cache_data (不会把刚算出的条目又立刻清掉)。
            _clear_calendar_cache()
            data = _fetch_calendar_with_progress(
                tuple(syms), int(months), bool(use_longport), refresh=True
            )
            _clear_calendar_cache()
        else:
            # 首屏先探缓存 (只读, 秒回): 有旧数据立即渲染 + 后台刷新;
            # 完全无缓存才阻塞取数 (带进度条), 取完清探针缓存供下次读新盘。
            data = cached_calendar(tuple(syms), int(months), bool(use_longport))
            if data is None:
                # 预热/其它标签页可能已在拉同一范围: 不重复阻塞取数,
                # 交给看门狗在后台完成后 rerun。
                warming, _done = warm_mod.calendar_refresh_state(key)
                if warming:
                    st.info(
                        "日历缓存正在后台预热/刷新, 完成后自动显示; "
                        "首次全量加载需要一些时间。"
                    )
                    _calendar_watchdog(
                        key, tuple(syms), int(months), bool(use_longport)
                    )
                    return
                data = _fetch_calendar_with_progress(
                    tuple(syms), int(months), bool(use_longport), refresh=False
                )
                _clear_calendar_cache()
            else:
                cmeta = _cache_meta(data)
                if cmeta.get("stale") or cmeta.get("partial"):
                    _calendar_watchdog(
                        key, tuple(syms), int(months), bool(use_longport)
                    )
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
    _render_overview(evs)
    cmeta = _cache_meta(data)
    if cmeta.get("stale"):
        age_h = float(cmeta.get("stale_age") or 0.0) / 3600.0
        st.caption(
            f":material/history: 先展示约 {age_h:.1f} 小时前的缓存, "
            "后台正在刷新最新日历…"
        )
    elif cmeta.get("partial"):
        st.caption(":material/sync: 部分代码暂无缓存, 后台正在补齐日历…")
    if not evs:
        st.info("窗口内无财报/分红事件。")
        return

    st.download_button(
        "导出 .ics 日历",
        build_ics(evs).encode("utf-8"),
        file_name=ics_filename(data.get("window")),
        mime="text/calendar",
        icon=":material/calendar_month:",
        help="全天事件, 可导入 Apple 日历 / Google Calendar / Outlook",
    )
    _render_month_grids(evs, w0, w1)
    _render_agenda(evs)
    _render_detail_table(evs)


def _event_date(ev: dict) -> date:
    return date.fromisoformat(str(ev["date"])[:10])


def _type_class(ev: dict) -> str:
    t = str(ev.get("type") or "")
    return t if t in TYPE_RANK else "other"


def _sorted_events(evs: list[dict]) -> list[dict]:
    return sorted(
        evs,
        key=lambda e: (
            str(e.get("date") or ""),
            TYPE_RANK.get(str(e.get("type")), 9),
            str(e.get("symbol") or ""),
        ),
    )


def _section_head(title: str, meta: str = "") -> None:
    """页面内小节标题 (左侧标题 + 右侧辅助说明)."""
    meta_html = (
        f'<span class="fp-section-sub">{html.escape(meta)}</span>' if meta else ""
    )
    st.markdown(
        '<div class="fp-section-head">'
        f'<span class="fp-section-title">{html.escape(title)}</span>'
        f"{meta_html}</div>",
        unsafe_allow_html=True,
    )


def _render_overview(evs: list[dict]) -> None:
    """概览条: 事件/标的/下一事件统计 + 事件类型图例 (带数量)."""
    today = date.today()
    symbols = {str(e.get("symbol") or "") for e in evs}
    symbols.discard("")
    future = sorted(d for d in (_event_date(e) for e in evs) if d >= today)
    if future:
        nd = future[0]
        delta = (nd - today).days
        rel = "今天" if delta == 0 else "明天" if delta == 1 else f"{delta} 天后"
        next_v, next_l = f"{nd:%m-%d}", rel
    else:
        next_v, next_l = "—", "暂无未来事件"

    stats = [
        (str(len(evs)), "个事件"),
        (str(len(symbols)), "只标的"),
        (next_v, next_l),
    ]
    parts = ['<div class="fp-cal-overview">']
    for value, label in stats:
        parts.append(
            '<div class="fp-cal-stat">'
            f'<span class="fp-cal-stat-v">{html.escape(value)}</span>'
            f'<span class="fp-cal-stat-l">{html.escape(label)}</span></div>'
        )
    parts.append('<div class="fp-cal-legend">')
    for t in TYPE_ORDER:
        count = sum(1 for e in evs if str(e.get("type")) == t)
        label = events_mod.EVENT_TYPE_LABELS.get(t, t)
        parts.append(
            f'<span class="fp-cal-legend-item fp-ev-{t}"><i class="fp-dot"></i>'
            f"{html.escape(label)}<b>{count}</b></span>"
        )
    parts.append("</div></div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def _render_month_grids(evs: list[dict], w0: date, w1: date) -> None:
    """每个自然月一张 7 列月历卡 (周行 + 相邻月淡显; 当日高亮, 单日折叠).

    用原生 CSS Grid 输出整月 (而不是 7×N 个 st.columns): Streamlit 的列在
    窄屏会自动折成单列, 会把月历拆成一条竖线; CSS Grid 固定 7 列, 移动端
    只缩小字号/隐藏名称, 保持日历语义。相邻月的日期只做视觉占位、不重复
    展示事件 (事件属于窗口内另一个月的卡片)。
    """
    by_day: dict[date, list[dict]] = {}
    for e in evs:
        by_day.setdefault(_event_date(e), []).append(e)
    for day_evs in by_day.values():
        day_evs.sort(
            key=lambda e: (
                TYPE_RANK.get(str(e.get("type")), 9),
                str(e.get("symbol") or ""),
            )
        )
    today = date.today()

    for start, _end in events_mod._month_starts(w0, w1):
        y, m = start.year, start.month
        first = date(y, m, 1)
        last = date(y, m, _cal.monthrange(y, m)[1])
        month_evs = [
            e for d, day_evs in by_day.items() if (d.year, d.month) == (y, m)
            for e in day_evs
        ]
        meta = f"{len(month_evs)} 个事件" if month_evs else "本月无事件"

        parts = [
            '<div class="fp-cal-month">',
            '<div class="fp-cal-head">',
            f'<span class="fp-cal-title">{y} 年 {m} 月</span>',
            f'<span class="fp-cal-month-meta">{meta}</span>',
            "</div>",
            '<div class="fp-cal-grid">',
        ]
        for idx, wd in enumerate(WEEKDAYS):
            cls = "fp-cal-wd is-weekend" if idx >= 5 else "fp-cal-wd"
            parts.append(f'<div class="{cls}">{wd}</div>')

        for week_idx, week in enumerate(_cal.monthcalendar(y, m)):
            for col, day_num in enumerate(week):
                if day_num:
                    dt = date(y, m, day_num)
                elif week_idx == 0:
                    dt = first - timedelta(days=first.weekday() - col)
                else:
                    dt = last + timedelta(days=col - last.weekday())
                out = dt.month != m
                classes = ["fp-cal-cell"]
                if col >= 5:
                    classes.append("is-weekend")
                if out:
                    classes.append("is-out")
                elif dt == today:
                    classes.append("is-today")
                day_evs = [] if out else (by_day.get(dt) or [])
                if day_evs:
                    classes.append("has-ev")
                body = [f'<div class="fp-cal-num">{dt.day}</div>']
                if day_evs:
                    body.append('<div class="fp-cal-evs">')
                    for ev in day_evs[:MAX_PER_CELL]:
                        mark = TYPE_MARK.get(ev["type"], "?")
                        sym = html.escape(str(ev["symbol"]))
                        name = _short_name(ev.get("name"))
                        label = events_mod.EVENT_TYPE_LABELS.get(
                            ev["type"], ev["type"]
                        )
                        tooltip = html.escape(
                            f"{ev['symbol']} {name or ''} · {label}".strip()
                        )
                        name_html = (
                            f'<span class="fp-cal-name">{html.escape(name)}</span>'
                            if name
                            else ""
                        )
                        body.append(
                            f'<div class="fp-cal-ev fp-ev-{_type_class(ev)}" '
                            f'title="{tooltip}"><i class="fp-dot"></i>'
                            f'<span class="fp-cal-ev-text">{mark} {sym}</span>'
                            f"{name_html}</div>"
                        )
                    body.append("</div>")
                    extra = len(day_evs) - MAX_PER_CELL
                    if extra > 0:
                        body.append(
                            f'<div class="fp-cal-more">+{extra} 更多</div>'
                        )
                    # 窄屏只保留类型色点 (完整代码/名称见下方日程与表格)
                    tags = "".join(
                        f'<i class="fp-cal-tag fp-ev-{_type_class(ev)}"></i>'
                        for ev in day_evs[:4]
                    )
                    if len(day_evs) > 4:
                        tags += (
                            f'<span class="fp-cal-tag-more">'
                            f"+{len(day_evs) - 4}</span>"
                        )
                    body.append(f'<div class="fp-cal-marks">{tags}</div>')
                parts.append(f'<div class="{" ".join(classes)}">{"".join(body)}</div>')

        parts.append("</div></div>")
        st.markdown("".join(parts), unsafe_allow_html=True)


def _render_agenda(evs: list[dict]) -> None:
    """按日期分组的日程列表 (比裸表格更接近成熟日历的 agenda 视图)."""
    grouped: dict[date, list[dict]] = {}
    for e in _sorted_events(evs):
        grouped.setdefault(_event_date(e), []).append(e)
    today = date.today()
    _section_head("日程", f"{len(grouped)} 个交易日")

    parts = ['<div class="fp-agenda">']
    for dt in sorted(grouped):
        delta = (dt - today).days
        if delta == 0:
            rel = "今天"
        elif delta == 1:
            rel = "明天"
        elif delta > 1:
            rel = f"{delta} 天后"
        elif delta == -1:
            rel = "昨天"
        else:
            rel = f"{-delta} 天前"
        classes = ["fp-agenda-day"]
        if delta == 0:
            classes.append("is-today")
        elif delta < 0:
            classes.append("is-past")

        items = []
        for ev in grouped[dt]:
            label = events_mod.EVENT_TYPE_LABELS.get(
                ev.get("type"), str(ev.get("type") or "")
            )
            status = events_mod.DATE_STATUS_LABELS.get(
                ev.get("date_status") or "", ""
            )
            note = str(ev.get("note") or "").strip()
            source = str(ev.get("source") or "").strip()
            name = str(ev.get("name") or "").strip()
            items.append(
                f'<div class="fp-agenda-item fp-ev-{_type_class(ev)}">'
                f'<span class="fp-agenda-badge">{html.escape(label)}</span>'
                f'<span class="fp-agenda-symbol">'
                f'{html.escape(str(ev.get("symbol") or ""))}</span>'
                + (
                    f'<span class="fp-agenda-name">{html.escape(name)}</span>'
                    if name
                    else ""
                )
                + (
                    f'<span class="fp-agenda-status">{html.escape(status)}</span>'
                    if status
                    else ""
                )
                + (
                    f'<span class="fp-agenda-note">{html.escape(note)}</span>'
                    if note
                    else ""
                )
                + (
                    f'<span class="fp-agenda-src">{html.escape(source)}</span>'
                    if source
                    else ""
                )
                + "</div>"
            )
        parts.append(
            f'<div class="{" ".join(classes)}">'
            f'<div class="fp-agenda-date">'
            f'<span class="fp-agenda-dow">周{WEEKDAYS[dt.weekday()]}</span>'
            f'<span class="fp-agenda-num">{dt.month}/{dt.day}</span>'
            f'<span class="fp-agenda-rel">{rel}</span></div>'
            f'<div class="fp-agenda-items">{"".join(items)}</div></div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def _render_detail_table(evs: list[dict]) -> None:
    """完整事件表: 收进折叠面板, 不打断日历/日程的浏览节奏."""
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
            for e in _sorted_events(evs)
        ]
    )
    with st.expander(f"表格视图 · 全部 {len(df)} 个事件", expanded=False):
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
