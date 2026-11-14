"""doctor 子命令: 数据源健康 / 缓存覆盖 / 本地完整性体检.

与 `cache info` 的分工:
- `cache info` 回答「缓存里有多少条」;
- `doctor` 回答「这些数据是哪来的、成功率如何、组合里谁缺数据、本地文件
  还健康吗」。默认只读本地 (零网络); `--probe` 才主动打一次真实接口。
"""

from __future__ import annotations

from datetime import datetime

from ._common import _finish_with_error, _print_json

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


def _time(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def build_report(
    window_hours: float = 24.0,
    *,
    with_probe: bool = False,
    probe_symbols=None,
) -> dict:
    """汇总健康/覆盖/完整性/缓存/保温状态; 实现见 ``health.report``."""
    from .. import health

    return health.report(
        window_hours, with_probe=with_probe, probe_symbols=probe_symbols
    )


def _print_human(report: dict) -> None:
    overall = report["overall"]
    print(f"\n=== 数据健康 ({report['window_hours']:g}h 窗口) ===")
    print(
        f"  {_STATUS_ICON.get(overall, '?')} 总体: "
        f"{_STATUS_TEXT.get(overall, overall)}"
    )
    print(f"  生成时间: {_time(report['generated_at'])}")

    health = report["health"]
    totals = health["totals"]
    if totals["attempts"]:
        print(
            f"  回源尝试: {totals['attempts']} 次, 成功 {totals['ok']} / "
            f"失败 {totals['fail']} ({totals['success_rate']:.0%})"
        )
    else:
        print("  回源尝试: 窗口内无记录 (只读缓存或尚未取数)")

    print("\n--- 数据源 ---")
    header = (
        f"  {'状态':<4} {'来源':<20} {'类型':<4} {'尝试':>5} "
        f"{'成功率':>6} {'p95':>7} {'最近成功':>12} {'最近失败':>12}"
    )
    print(header)
    for row in health["sources"]:
        if row["status"] == "idle" and row["source"] not in (
            "yfinance",
            "akshare",
            "cfets",
        ):
            # 可选源未启用是常态: 只在 --json 中保留, 人读输出不刷屏
            continue
        rate = (
            f"{row['success_rate']:.0%}" if row["success_rate"] is not None else "—"
        )
        p95 = f"{row['p95_ms']:.0f}ms" if row["p95_ms"] is not None else "—"
        print(
            f"  {_STATUS_ICON.get(row['status'], '?'):<4} "
            f"{row['label'][:20]:<20} "
            f"{_KIND_TEXT.get(row['kind'], row['kind']):<4} "
            f"{row['attempts']:>5} {rate:>6} {p95:>7} "
            f"{_time(row['last_ok_at']):>12} {_time(row['last_fail_at']):>12}"
        )
        if row["status"] in ("down", "degraded") and row["last_error"]:
            print(f"        └ {row['last_error'][:100]}")

    cov = report["coverage"]
    sym = cov["symbols"]
    print("\n--- 组合数据覆盖 (只读缓存) ---")
    print(
        f"  代码: 共 {sym['total']} (持仓 {sym['holdings']} / 自选 {sym['watchlist']})"
    )
    print(
        f"  行情: 新鲜 {sym['fresh']} / 陈旧 {sym['stale']} / "
        f"偏旧 {sym['old']} / 过期 {sym['expired']} / 缺失 {sym['missing']}"
    )
    fx = cov["fx"]
    fx_missing = fx["missing"]
    print(
        f"  汇率: 基础 {fx['base']}, 需要 {fx['needed']} 个, "
        f"缺失 {len(fx_missing)}"
        + (f" ({', '.join(fx_missing[:8])})" if fx_missing else "")
    )
    gaps = cov.get("gaps") or []
    if gaps:
        print(f"  缺口 {len(gaps)} 项 (前 12):")
        for item in gaps[:12]:
            print(f"    - {item['symbol']} [{item['kind']}]: {'; '.join(item['reasons'])}")
        if len(gaps) > 12:
            print(f"    … 另有 {len(gaps) - 12} 项")
    else:
        print("  ✅ 持仓/自选的行情与关键 K线均已有缓存")

    for err in cov.get("errors") or []:
        print(f"  ⚠️ {err}")

    print("\n--- 缓存 ---")
    info = report["cache"]
    print(
        f"  行情 {info['total']} 条 (新鲜 {info['fresh']} / 陈旧 {info['stale']}), "
        f"K线 {info['ohlc_total']} 组, 汇率 {info['fx_total']} 组, "
        f"日历 {info['calendar_total']} 组"
    )
    if report.get("cache_sources"):
        parts = [
            f"{src} {v['total']}"
            for src, v in report["cache_sources"].items()
        ]
        print(f"  当前缓存来源: {', '.join(parts)}")

    print("\n--- 本地完整性 ---")
    for check in report["integrity"]["checks"]:
        icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}.get(check["status"], "?")
        detail = f" — {check['detail']}" if check["detail"] else ""
        print(f"  {icon} {check['name']}{detail}")

    warm = report["warm"]
    print(
        f"\n  保温线程 (本进程): 行情 {'运行' if warm['quotes']['started'] else '未启动'}"
        f" / 日历 {'运行' if warm['calendar']['started'] else '未启动'}"
    )
    for label, state in (("行情", warm["quotes"]), ("日历", warm["calendar"])):
        if state["last_done_at"]:
            print(
                f"    {label}最近完成: {_time(state['last_done_at'])} "
                f"({_age(report['generated_at'] - state['last_done_at'])} 前)"
            )

    failures = health.get("recent_failures") or []
    if failures:
        print(f"\n--- 最近失败 (最多 10/{len(failures)}) ---")
        for f in failures[:10]:
            symbol = f["symbol"] or "-"
            print(
                f"  {_age(f['age_seconds'])}前 {f['source']}/"
                f"{_KIND_TEXT.get(f['kind'], f['kind'])} {symbol}: "
                f"{(f['error'] or '')[:120]}"
            )

    probe = report.get("probe")
    if probe:
        print("\n--- 主动探测 (--probe) ---")
        for row in probe["results"]:
            icon = "✅" if row["ok"] else "❌"
            print(
                f"  {icon} {row['symbol']:<12} {row['elapsed_ms']:>7.0f}ms "
                f"{row['detail'][:80]}"
            )

    print()


def cmd_doctor(args) -> None:
    """数据健康体检; --json 输出完整报告, --probe 主动联网探测."""
    from .. import cache as cache_mod

    if getattr(args, "clear_events", False):
        n = cache_mod.clear_health_events()
        if args.json:
            _print_json({"cleared": n})
        else:
            print(f"✅ 已清空健康事件 ({n} 条)")
        return

    window = float(getattr(args, "window", 24) or 24)
    with_probe = bool(getattr(args, "probe", False))
    probe_symbols = getattr(args, "probe_symbols", None)
    try:
        report = build_report(
            window, with_probe=with_probe, probe_symbols=probe_symbols
        )
    except Exception as e:  # noqa: BLE001 - 体检入口本身不能抛裸 traceback
        _finish_with_error(f"数据健康检查失败: {e}")

    if args.json:
        _print_json(report)
    else:
        _print_human(report)

    if report["overall"] == "down":
        raise SystemExit(1)
