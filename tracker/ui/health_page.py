"""「数据健康」页面: 数据源成功率 / 组合缓存覆盖 / 本地完整性 / 最近失败.

页面本身不联网 (除非用户点「主动探测」), 读的是 provider 热路径记录到
``health_events`` 的观测数据和磁盘缓存状态。目标是回答:

- 页面上的价格/曲线是哪来的? 上游最近在失败吗?
- 组合里谁没有行情 / 汇率 / K线缓存, 导致净值或盈亏可能不准?
- 本地数据文件、缓存库、目录权限还健康吗?
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from . import theme

from tracker import health

_STATUS_ICON = {
    "ok": "✅",
    "degraded": "⚠️",
    "down": "❌",
    "idle": "·",
    "unknown": "?",
}
_STATUS_TEXT = {
    "ok": "正常",
    "degraded": "降级",
    "down": "异常",
    "idle": "未使用",
    "unknown": "无数据",
}
_KIND_TEXT = {
    "quote": "行情",
    "history": "K线",
    "fx": "汇率",
    "calendar": "日历",
    "other": "其它",
}
_QUOTE_STATUS_TEXT = {
    "fresh": "新鲜",
    "stale": "陈旧",
    "old": "偏旧",
    "expired": "过期",
    "missing": "缺失",
}


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _time(ts: float | None, now: float) -> str:
    if not ts:
        return "—"
    return f"{_age(now - ts)}前"


@st.cache_data(ttl=30, show_spinner=False)
def cached_report(window_hours: int) -> dict:
    """30s 内存缓存: 页面 rerun 不反复扫健康表/缓存库."""
    return health.report(window_hours)


def _status_icon(status: str) -> str:
    return _STATUS_ICON.get(status, "?")


def _render_banner(report: dict) -> None:
    overall = report["overall"]
    totals = report["health"]["totals"]
    gaps = len(report["coverage"].get("gaps") or [])
    if overall == "ok":
        st.success(
            f"数据面正常: 窗口内 {totals['attempts']} 次回源, "
            f"成功率 {totals['success_rate']:.0%}, 组合无缓存缺口。"
        )
    elif overall == "degraded":
        reasons = []
        if gaps:
            reasons.append(f"{gaps} 个代码缺行情/汇率/K线")
        bad = [
            r["label"] + " " + _KIND_TEXT.get(r["kind"], r["kind"])
            for r in report["health"]["sources"]
            if r["status"] == "degraded"
        ]
        if bad:
            reasons.append("降级来源: " + "、".join(bad[:4]))
        st.warning("数据面降级, 页面仍可打开但部分数字可能不准。" + " ".join(reasons))
    elif overall == "down":
        st.error(
            "数据面异常: 有来源持续失败或本地数据文件/缓存库不可用。"
            "请查看下方「数据源」与「本地完整性」。"
        )
    else:
        st.info("暂无回源记录 (可能只用到了磁盘缓存)。")


def _render_sources(report: dict, now: float) -> None:
    rows = report["health"]["sources"]
    show_idle = st.toggle(
        "显示未使用的数据源", value=False, key="health_show_idle",
        help="IBKR / 长桥 / EODHD 等可选源未启用时没有调用记录, 默认折叠。",
    )
    if not show_idle:
        rows = [r for r in rows if r["status"] != "idle"]
    if not rows:
        st.caption("窗口内没有回源记录。")
        return
    table = []
    for r in rows:
        table.append(
            {
                "状态": (
                    f"{_status_icon(r['status'])} "
                    f"{_STATUS_TEXT.get(r['status'], r['status'])}"
                ),
                "来源": r["label"],
                "类型": _KIND_TEXT.get(r["kind"], r["kind"]),
                "尝试": r["attempts"],
                "成功率": (
                    f"{r['success_rate']:.0%}"
                    if r["success_rate"] is not None
                    else "—"
                ),
                "p50": f"{r['p50_ms']:.0f}ms" if r["p50_ms"] is not None else "—",
                "p95": f"{r['p95_ms']:.0f}ms" if r["p95_ms"] is not None else "—",
                "最近成功": _time(r["last_ok_at"], now),
                "最近失败": _time(r["last_fail_at"], now),
                "最近错误": (r["last_error"] or "")[:90],
            }
        )
    st.dataframe(
        pd.DataFrame(table),
        hide_index=True,
        use_container_width=True,
        column_config={
            "状态": st.column_config.TextColumn(width="medium"),
            "尝试": st.column_config.NumberColumn(width="small"),
            "最近错误": st.column_config.TextColumn(width="large"),
        },
    )


def _render_coverage(report: dict) -> None:
    cov = report["coverage"]
    sym = cov["symbols"]
    fx = cov["fx"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("代码总数", sym["total"], help="持仓 + 自选去重后的代码数")
    c2.metric("行情新鲜", sym["fresh"], help="5 分钟内更新")
    c3.metric(
        "行情缺失/过期",
        sym["missing"] + sym["expired"],
        delta=None if sym["missing"] + sym["expired"] == 0 else "需刷新",
        delta_color="inverse",
    )
    c4.metric(
        "汇率缺失",
        len(fx["missing"]),
        delta=None if not fx["missing"] else ", ".join(fx["missing"][:3]),
        delta_color="inverse",
    )

    gaps = cov.get("gaps") or []
    if gaps:
        st.markdown(f"**缓存缺口 {len(gaps)} 项** (行情缺失/超过一天、持仓无 K线)")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "代码": g["symbol"],
                        "类型": g["kind"],
                        "原因": "; ".join(g["reasons"]),
                    }
                    for g in gaps
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.caption("✅ 持仓与自选的行情、持仓 K线缓存均无缺口。")

    with st.expander(f"逐代码明细 ({len(cov.get('items') or [])})"):
        items = cov.get("items") or []
        if items:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "代码": i["symbol"],
                            "名称": i["name"] or "",
                            "归属": i["kind"],
                            "币种": i["currency"] or "",
                            "行情": _QUOTE_STATUS_TEXT.get(
                                i["quote_status"], i["quote_status"]
                            ),
                            "行情年龄": _age(i["quote_age_seconds"]),
                            "K线年龄": _age(i["ohlc_age_seconds"]),
                        }
                        for i in items
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
    for err in cov.get("errors") or []:
        st.warning(err)


def _render_failures(report: dict) -> None:
    failures = report["health"].get("recent_failures") or []
    if not failures:
        st.caption("✅ 统计窗口内没有失败记录。")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "多久前": _age(f["age_seconds"]),
                    "来源": health.label(f["source"]),
                    "类型": _KIND_TEXT.get(f["kind"], f["kind"]),
                    "代码": f["symbol"] or "—",
                    "错误": (f["error"] or "")[:160],
                }
                for f in failures
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )


def _render_integrity(report: dict) -> None:
    for check in report["integrity"]["checks"]:
        icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}.get(check["status"], "?")
        detail = f" — {check['detail']}" if check["detail"] else ""
        st.markdown(f"{icon} **{check['name']}**{detail}")


def _render_cache(report: dict) -> None:
    info = report["cache"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("行情缓存", info["total"], help=f"新鲜 {info['fresh']} 条")
    c2.metric("K线缓存", info["ohlc_total"], help=f"有效 {info['ohlc_fresh']} 组")
    c3.metric("汇率缓存", info["fx_total"], help=f"新鲜 {info['fx_fresh']} 组")
    c4.metric("日历缓存", info["calendar_total"], help="整批 + 单票事件")
    if info.get("error"):
        st.warning(f"缓存统计读取失败: {info['error']}")
    st.caption(
        f"数据库 {info['db']} · {info['size_bytes'] / 1024 / 1024:.1f} MB · "
        f"行情 TTL {info['ttl_seconds']}s / 陈旧 {info['stale_ttl_seconds']}s"
    )

    sources = report.get("cache_sources") or {}
    if sources:
        st.markdown("**当前行情缓存来源** (刷新后会被新来源覆盖)")
        st.dataframe(
            pd.DataFrame(
                [
                    {"来源": health.label(src), "条数": v["total"], "新鲜": v["fresh"]}
                    for src, v in sources.items()
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )

    warm = report["warm"]
    st.caption(
        f"保温线程 (本进程): 行情 "
        f"{'运行中' if warm['quotes']['started'] else '未启动'} / 日历 "
        f"{'运行中' if warm['calendar']['started'] else '未启动'}"
    )


def render_health_page() -> None:
    """数据健康页入口."""
    theme.page_header("数据健康", eyebrow="DATA HEALTH")

    c1, c2, c3 = st.columns([2, 1, 1])
    window = c1.selectbox(
        "统计窗口",
        [6, 24, 72, 168],
        index=1,
        format_func=lambda h: f"近 {h} 小时" if h < 168 else "近 7 天",
        key="health_window",
    )
    probe = c3.button("主动探测", help="联网打一次行情/汇率, 约数秒到十几秒")
    if c2.button("刷新", help="清掉 30s 页面缓存并重新读取"):
        cached_report.clear()
        st.rerun()

    if probe:
        with st.spinner("正在探测 AAPL / 600519.SS / 0700.HK / BTC-USD / USD-CNY …"):
            report = health.report(window, with_probe=True)
        cached_report.clear()
    else:
        report = cached_report(window)

    now = report["generated_at"]
    _render_banner(report)

    c1, c2, c3, c4 = st.columns(4)
    totals = report["health"]["totals"]
    c1.metric(
        "回源成功率",
        f"{totals['success_rate']:.0%}" if totals["success_rate"] is not None else "—",
        help=f"{totals['ok']} / {totals['attempts']} 次",
    )
    c2.metric("回源尝试", totals["attempts"], help="缓存命中不计入")
    c3.metric("缓存缺口", len(report["coverage"].get("gaps") or []))
    c4.metric(
        "最近失败",
        len(report["health"].get("recent_failures") or []),
        help="窗口内失败次数 (最多展示 20 条)",
    )

    tabs = st.tabs(["数据源", "组合覆盖", "最近失败", "本地完整性", "缓存与保温"])
    with tabs[0]:
        st.caption(
            "数据来自 provider 热路径的真实回源记录; 缓存命中不记录。"
            "「未使用」表示窗口内没有调用 (可能是没启用该可选源)。"
        )
        _render_sources(report, now)
        if report.get("probe"):
            st.markdown("**主动探测结果**")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "状态": "✅" if r["ok"] else "❌",
                            "目标": r["symbol"],
                            "类型": _KIND_TEXT.get(r["kind"], r["kind"]),
                            "耗时": f"{r['elapsed_ms']:.0f}ms",
                            "详情": r["detail"][:120],
                        }
                        for r in report["probe"]["results"]
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )
    with tabs[1]:
        _render_coverage(report)
    with tabs[2]:
        _render_failures(report)
    with tabs[3]:
        _render_integrity(report)
    with tabs[4]:
        _render_cache(report)
        st.divider()
        st.markdown("**维护**")
        confirm = st.checkbox(
            "我确认要清空健康事件记录", key="health_clear_confirm",
            help="只清排障记录, 不影响行情/K线/汇率缓存与持仓数据。",
        )
        if st.button("清空健康事件", disabled=not confirm):
            from tracker import cache as cache_mod

            n = cache_mod.clear_health_events()
            cached_report.clear()
            st.success(f"已清空 {n} 条健康事件")
            st.rerun()
        st.caption(
            "体检时间: "
            + datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        )
