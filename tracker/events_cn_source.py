"""A股日历源: akshare 整批预约披露 + 分红送转.

从 events.py 拆出: 东财整批请求、板块/报表期任务并发、单块缓存与进度。
窗口/报表期工具见 events_window.py, 公共常量见 events_common.py。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date

from . import cache as cache_mod
from .deps import akshare as _akshare
from .events_cn import _cn_normalize_disclosure, _cn_normalize_fhps, _pd_date
from .events_common import (
    CAL_STALE_TTL,
    CAL_TTL,
    _ProgressReporter,
    _merge_cache_meta,
)
from .events_window import (
    _age_of,
    _in_window,
    cn_dividend_periods_for_window,
    cn_periods_for_window,
)
from .util import with_timeout

_CN_MAX_WORKERS = 6
_AK_TIMEOUT = 15.0
_CN_BOARDS = ("沪深A股", "京市A股")

# sys.stderr 是进程级对象: 重定向窗口必须串行化, 否则 Streamlit 多会话/
# 多线程并发取日历时会互相覆盖, 甚至把 devnull 永久留在全局。
_STDERR_LOCK = threading.Lock()


@contextmanager
def _quiet_stderr():
    """静音 akshare 内层 tqdm 进度条 (tqdm 构造时捕获 sys.stderr).

    CLI/UI 日历请求一次拉全市场 5 千行, 内层进度条只会刷屏; 我们自己的
    单票循环进度 (fetch_calendar progress 回调) 打到 stderr 的提示不受
    影响。并发批次只在最外层持锁一次 (worker 传 quiet=False), 既避免全局
    stderr 被并发会话互相改写, 又允许同一批次内多个请求并行。
    """
    import os
    import sys

    with _STDERR_LOCK:
        devnull = open(os.devnull, "w", encoding="utf-8")
        old = sys.stderr
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stderr = old
            devnull.close()


_CN_BOARDS = ("沪深A股", "京市A股")


def _fetch_cn_cached(
    key: str,
    *,
    period: str,
    step_label: str,
    refresh: bool,
    stale_ok: bool,
    cache_only: bool,
    reporter: _ProgressReporter | None,
    quiet: bool,
    fetch,
    normalize,
    error_text,
) -> tuple[list[dict], str | None, dict]:
    """CN 日历单块缓存的公共壳 (预约披露/分红共用)."""
    meta = {"stale": False, "missing": False, "stale_age": 0.0, "used": False}
    row = None if refresh else cache_mod.get_calendar_row(key)
    if row is not None:
        age = _age_of(row["fetched_at"])
        if age <= CAL_TTL or (stale_ok and age <= CAL_STALE_TTL):
            meta.update(stale=age > CAL_TTL, stale_age=age, used=True)
            if reporter is not None:
                reporter.step(step_label)
            return row["events"], None, meta
    if cache_only:
        meta["missing"] = True
        if reporter is not None:
            reporter.step(step_label)
        return [], None, meta
    try:
        if quiet:
            with _quiet_stderr():
                raw = fetch()
        else:
            raw = fetch()
    except Exception as e:  # noqa: BLE001 - 单块失败注记并继续其余块
        if reporter is not None:
            reporter.step(step_label)
        return [], error_text(e)[:200], meta
    evs = normalize(raw)
    cache_mod.set_calendar_row(key, evs)
    if reporter is not None:
        reporter.step(step_label)
    return evs, None, meta


def _fetch_cn_yysj_board(
    board: str,
    period: str,
    refresh: bool,
    stale_ok: bool = False,
    cache_only: bool = False,
    reporter: _ProgressReporter | None = None,
    quiet: bool = True,
) -> tuple[list[dict], str | None, dict]:
    """单个板块的预约披露 (缓存优先, TTL 6h) → events, error, meta."""
    return _fetch_cn_cached(
        f"cn:yysj:{period}:{board}",
        period=period,
        step_label=f"{period} {board}",
        refresh=refresh,
        stale_ok=stale_ok,
        cache_only=cache_only,
        reporter=reporter,
        quiet=quiet,
        fetch=lambda: with_timeout(
            _akshare().stock_yysj_em, _AK_TIMEOUT, symbol=board, date=period
        ),
        normalize=lambda df: _cn_normalize_disclosure(df, period),
        error_text=lambda e: f"akshare 预约披露 {period} {board} 失败 ({e})",
    )


def _fetch_cn_fhps_period(
    period: str,
    refresh: bool,
    stale_ok: bool = False,
    cache_only: bool = False,
    reporter: _ProgressReporter | None = None,
    quiet: bool = True,
) -> tuple[list[dict], str | None, dict]:
    """单个报表期的分红送转 (缓存优先, TTL 6h) → events, error, meta."""
    return _fetch_cn_cached(
        f"cn:fhps:{period}",
        period=period,
        step_label=f"{period} 分红",
        refresh=refresh,
        stale_ok=stale_ok,
        cache_only=cache_only,
        reporter=reporter,
        quiet=quiet,
        fetch=lambda: with_timeout(_akshare().stock_fhps_em, _AK_TIMEOUT, date=period),
        normalize=_cn_normalize_fhps,
        error_text=lambda e: f"akshare 分红 {period} 失败 ({e})",
    )


def _fetch_cn_period(
    period: str,
    refresh: bool,
    stale_ok: bool = False,
    cache_only: bool = False,
    reporter: _ProgressReporter | None = None,
    boards: tuple[str, ...] | list[str] | None = None,
    quiet: bool = True,
) -> tuple[dict, list[str]]:
    """一个报表期的整市场日历 (预约披露 + 分红, 各自独立缓存) → events, issues.

    meta 挂在 data 上 (stale/missing/stale_age/used), 保持原有二元组返回结构。
    boards 可只取本范围真正需要的板块 (如自选里没有北交所代码就跳过京市)。
    """
    evs: list[dict] = []
    issues: list[str] = []
    meta = {"stale": False, "missing": False, "stale_age": 0.0, "used": False}
    for board in tuple(boards) if boards is not None else _CN_BOARDS:
        board_evs, issue, bmeta = _fetch_cn_yysj_board(
            board,
            period,
            refresh,
            stale_ok=stale_ok,
            cache_only=cache_only,
            reporter=reporter,
            quiet=quiet,
        )
        evs.extend(board_evs)
        if issue:
            issues.append(issue)
        _merge_cache_meta(meta, bmeta)
    fhps_evs, issue, fmeta = _fetch_cn_fhps_period(
        period,
        refresh,
        stale_ok=stale_ok,
        cache_only=cache_only,
        reporter=reporter,
        quiet=quiet,
    )
    evs.extend(fhps_evs)
    if issue:
        issues.append(issue)
    _merge_cache_meta(meta, fmeta)
    return {"events": evs, **meta}, issues


def _cn_boards_for(symbols: set[str]) -> tuple[str, ...]:
    """本范围代码真正需要的东财板块 (沪/深 vs 北交所).

    原实现无条件拉「沪深 + 京市」两块全市场数据; 大部分组合没有北交所
    代码, 京市那块纯属白请求。反过来只有北交所代码时也无需拉沪深。
    """
    suffixes = {s.rpartition(".")[2].upper() for s in symbols if "." in s}
    boards: list[str] = []
    if not suffixes or suffixes & {"SS", "SZ"}:
        boards.append("沪深A股")
    if "BJ" in suffixes:
        boards.append("京市A股")
    return tuple(boards) or _CN_BOARDS


def _fetch_cn_events(
    symbols: set[str],
    start: date,
    end: date,
    refresh: bool,
    stale_ok: bool = False,
    cache_only: bool = False,
    reporter: _ProgressReporter | None = None,
) -> tuple[list[dict], list[str], dict[str, str], dict]:
    """cn 域事件: 每报表期整批缓存 + 本窗口过滤. → events, issues, names, meta.

    预约披露只取窗口真正可能命中的报表期; 分红送转单独用 15 个月回看
    (旧报告期的方案可能在窗口内才实施), 只对预约披露集合之外的老报告期
    补拉 fhps。各板块/报表期任务并发执行, 冷启动耗时从累加降到最慢一路。
    """
    events: list[dict] = []
    issues: list[str] = []
    names: dict[str, str] = {}
    meta = {"stale": False, "missing": False, "stale_age": 0.0, "used": False}

    def take(rows: list[dict]) -> None:
        for ev in rows:
            # 缓存载荷只校验顶层 events 是 list; 旧版本/手工改库/半截写入
            # 可能混进缺 symbol 的行。单条脏数据不能打断整批日历渲染。
            if not isinstance(ev, dict):
                continue
            sym = ev.get("symbol")
            if not isinstance(sym, str) or not sym:
                continue
            typ = ev.get("type")
            if not isinstance(typ, str) or not typ.strip():
                continue
            d = _pd_date(ev.get("date"))
            if sym in symbols and _in_window(d, start, end):
                events.append({**ev, "date": d})
                if sym not in names and ev.get("name"):
                    names[sym] = ev["name"]

    boards = _cn_boards_for(symbols)
    # 未来的报表期不可能已经有预约披露/分红实施数据, 东财接口对这类 date
    # 还会抛 TypeError 并被当成「数据源失败」反复重试。窗口可以包含尚未结束
    # 的季度 (如 11 月查 12 月), 这里按今天截断。
    today_s = date.today().strftime("%Y%m%d")
    earn_periods = [
        p for p in cn_periods_for_window(start, end) if p <= today_s
    ]
    earn_set = set(earn_periods)
    div_extra = [
        p
        for p in cn_dividend_periods_for_window(start, end)
        if p not in earn_set and p <= today_s
    ]

    # (kind, period, board) 任务清单: 预约披露按板块拆, 分红按报表期拆。
    tasks: list[tuple[str, str, str | None]] = []
    for period in earn_periods:
        for board in boards:
            tasks.append(("yysj", period, board))
        tasks.append(("fhps", period, None))
    for period in div_extra:
        tasks.append(("fhps", period, None))
    if reporter is not None:
        reporter.plan(len(tasks))

    def _label(task: tuple[str, str, str | None]) -> str:
        kind, period, board = task
        return f"{period} {board}" if kind == "yysj" else f"{period} 分红"

    def _run(task: tuple[str, str, str | None]):
        kind, period, board = task
        if kind == "yysj":
            return _fetch_cn_yysj_board(
                str(board),
                period,
                refresh,
                stale_ok=stale_ok,
                cache_only=cache_only,
                reporter=None,
                quiet=False,
            )
        return _fetch_cn_fhps_period(
            period,
            refresh,
            stale_ok=stale_ok,
            cache_only=cache_only,
            reporter=None,
            quiet=False,
        )

    def _run_safe(task: tuple[str, str, str | None]):
        try:
            return _run(task)
        except Exception as e:  # noqa: BLE001 - 单任务失败不拖整批
            kind, period, board = task
            what = f"预约披露 {period} {board}" if kind == "yysj" else f"分红 {period}"
            return (
                [],
                f"akshare {what} 失败 ({type(e).__name__}: {e})"[:200],
                {"stale": False, "missing": False, "stale_age": 0.0, "used": False},
            )

    results: dict[int, tuple[list[dict], str | None, dict]] = {}
    if cache_only or len(tasks) <= 1:
        # 只读缓存没有网络等待, 顺序执行即可 (也避免无谓的线程/stderr 切换)。
        for i, task in enumerate(tasks):
            results[i] = _run_safe(task)
            if reporter is not None:
                reporter.step(_label(task))
    else:
        # stderr 是进程级状态: 整批只持锁一次, worker 用 quiet=False。
        with _quiet_stderr():
            with ThreadPoolExecutor(max_workers=min(_CN_MAX_WORKERS, len(tasks))) as ex:
                futures = {
                    ex.submit(_run_safe, task): (i, task)
                    for i, task in enumerate(tasks)
                }
                for fut in as_completed(futures):
                    i, task = futures[fut]
                    results[i] = fut.result()
                    if reporter is not None:
                        reporter.step(_label(task))

    # 按任务原顺序合并, 保证事件/名称/进度与串行实现一致。
    for i, task in enumerate(tasks):
        evs, issue, tmeta = results[i]
        take(evs)
        if issue:
            issues.append(issue)
        _merge_cache_meta(meta, tmeta)
    return events, issues, names, meta


