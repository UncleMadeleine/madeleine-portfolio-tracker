"""全球日历源: yfinance 单票 calendar + 可选长桥兜底.

从 events.py 拆出: 单票/批量 global 事件抓取、Yahoo 限速与硬超时。
缓存与跨源编排仍在 events.py。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from . import cache as cache_mod
from .deps import yfinance as _yf
from .events_common import (
    CAL_NEG_TTL,
    CAL_STALE_TTL,
    CAL_TTL,
    _ProgressReporter,
    _is_valid_eps,
)
from .events_window import _age_exceeded, _age_of, _to_date, _window_rows
from .util import with_timeout

_YF_PAUSE = 0.05
_YF_MAX_WORKERS = 24
_YF_START_LOCK = threading.Lock()
_YF_NEXT_START = 0.0
_YF_TIMEOUT = 10.0
_UNSET = object()

def _yf_calendar_rows(symbol: str) -> list[dict]:
    """yf.Ticker(symbol).calendar → 原始事件行 (不含窗口过滤)."""
    yf = _yf()

    cal = with_timeout(lambda: yf.Ticker(symbol).calendar, _YF_TIMEOUT)
    if not isinstance(cal, dict):
        return []
    out: list[dict] = []
    eps_avg = None
    for k in ("Earnings Average", "EPS Estimate Average"):
        try:
            v = cal.get(k)
            f = float(v) if v is not None else None
        except (TypeError, ValueError):
            f = None
        if f is not None and _is_valid_eps(f):
            eps_avg = f
            break
    ed = cal.get("Earnings Date")
    dates = []
    if isinstance(ed, (list, tuple)):
        candidates = list(ed)
    else:
        candidates = [ed]
    for c in candidates:
        d = _to_date(c)
        if d is not None:
            dates.append(d)
    for d in sorted(set(dates)):
        row: dict = {
            "type": "earnings",
            "date": d,
            "date_status": "estimate",  # Yahoo 无 confirmed 标志, 一律估计口径
        }
        if eps_avg is not None:
            row["note"] = f"EPS 估计 ~{eps_avg:.2f}"
        out.append(row)
    for key, typ in (("Ex-Dividend Date", "ex_dividend"), ("Dividend Date", "dividend")):
        d = _to_date(cal.get(key))
        if d is not None:
            out.append({"type": typ, "date": d, "date_status": "planned"})
    return out

def _yf_rate_limit() -> None:
    """跨线程限制 yfinance 请求启动频率 (替代顺序循环里的 sleep).

    多线程下单纯 `time.sleep(_YF_PAUSE)` 只会让每个 worker 各睡一次,
    请求仍可能同时打满 Yahoo; 这里用模块级「下一个可用启动时刻」把启动
    间隔真正串起来。线程在锁外等待, 不阻塞其它 worker 发起请求。
    """
    global _YF_NEXT_START
    if _YF_PAUSE <= 0:
        return
    with _YF_START_LOCK:
        now = time.monotonic()
        wait = _YF_NEXT_START - now
        _YF_NEXT_START = max(now, _YF_NEXT_START) + _YF_PAUSE
    if wait > 0:
        time.sleep(wait)

def _fetch_global_one(symbol: str, refresh: bool, cached_row=_UNSET) -> list[dict]:
    """单票 global 事件 (磁盘缓存优先); 空结果同样落库 (24h 负缓存语义).

    cached_row 由批量缓存读取层传入 (None 表示确认无行), 避免未命中的代码
    再逐只连一次 SQLite; 不传时保持旧的自行读缓存行为。
    """
    if refresh:
        row = None
    elif cached_row is _UNSET:
        row = cache_mod.get_calendar_row(f"g:{symbol}")
    else:
        row = cached_row
    if row is not None:
        evs, ttl = row["events"], (CAL_TTL if row["events"] else CAL_NEG_TTL)
        if not _age_exceeded(row["fetched_at"], ttl):
            return evs
    _yf_rate_limit()
    evs = _yf_calendar_rows(symbol)
    cache_mod.set_calendar_row(f"g:{symbol}", evs)
    return evs

def _fetch_global_events(
    symbols: list[str],
    start: date,
    end: date,
    refresh: bool,
    reporter: _ProgressReporter | None = None,
    stale_ok: bool = False,
    cache_only: bool = False,
) -> tuple[dict[str, list[dict]], list[str], dict]:
    """yfinance 单票并发取数 → {symbol: 窗口过滤后事件}, issues, 执行统计.

    命中缓存的代码零请求; 未命中的由 _YF_MAX_WORKERS 个线程并发发出,
    启动间隔仍由 _yf_rate_limit 控制 (防 429)。单票失败注记进 issues,
    不拖丢整批; progress 回调只在主线程调用, 避免 Streamlit API 跨线程。

    stale_ok: 过期但仍在 CAL_STALE_TTL 内的缓存先返回 (页面秒开, 后台重取)。
    cache_only: 只读缓存, 缺失/过老的行不进 todo (由调用方决定是否回源)。
    stats 额外记录 stale (用过陈旧行) / missing (缓存缺失) / stale_age。
    """
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    issues: list[str] = []
    stats = {"fresh": 0, "cached": 0, "fail": 0, "stale": 0, "missing": 0}
    stale_age = 0.0
    todo: list[str] = []
    if reporter is not None:
        reporter.plan(len(symbols))
    # 批量读缓存: 逐票 get_calendar_row 在大自选下是 N 次 SQLite 往返。
    cached_rows = (
        {} if refresh else cache_mod.get_calendar_rows([f"g:{s}" for s in symbols])
    )
    for s in symbols:
        row = None if refresh else cached_rows.get(f"g:{s}")
        if row is not None:
            evs = row["events"]
            ttl = CAL_TTL if evs else CAL_NEG_TTL
            age = _age_of(row["fetched_at"])
            usable = age <= ttl or (stale_ok and age <= CAL_STALE_TTL)
            if usable:
                if age > ttl:
                    stats["stale"] += 1
                    stale_age = max(stale_age, age)
                # 窗口过滤也放在单票保护内: 脏缓存 (NaT/非 dict 行) 只拖丢
                # 这一只, 不能让整批 fetch_calendar 抛出。
                try:
                    out[s] = _window_rows(
                        evs, start, end, source="yfinance", symbol=s
                    )
                except Exception as e:  # noqa: BLE001 - 单票脏数据不拖整批
                    stats["fail"] += 1
                    issues.append(
                        f"{s}: yfinance 缓存行异常 ({type(e).__name__}: {e})"[:160]
                    )
                else:
                    stats["cached"] += 1
                if reporter is not None:
                    reporter.step(s)
                continue
        if cache_only:
            stats["missing"] += 1
            if reporter is not None:
                reporter.step(s)
            continue
        todo.append(s)

    def _one(s: str) -> tuple[str, list[dict], str | None]:
        """单票取数 + 窗口过滤; 任何异常都收敛成一条 issue, 不冒泡."""
        try:
            evs = _fetch_global_one(
                s,
                refresh,
                cached_rows.get(f"g:{s}") if not refresh else None,
            )
            return s, _window_rows(evs, start, end, source="yfinance", symbol=s), None
        except Exception as e:  # noqa: BLE001 - 单票失败不拖丢整批
            return s, [], f"{s}: yfinance 日历失败 ({type(e).__name__}: {e})"[:160]

    def _apply(s: str, rows: list[dict], issue: str | None) -> None:
        out[s] = rows
        if issue is None:
            stats["fresh"] += 1
        else:
            stats["fail"] += 1
            issues.append(issue)
        if reporter is not None:
            reporter.step(s)

    if todo:
        workers = min(_YF_MAX_WORKERS, len(todo))
        if workers <= 1:
            for s in todo:
                _apply(*_one(s))
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_one, s) for s in todo]
                for fut in as_completed(futures):
                    _apply(*fut.result())
    stats["stale_age"] = stale_age
    return out, issues, stats

