"""无 UI 依赖的缓存保温/预热服务 (Streamlit 与 CLI 共用).

背景: 行情/汇率/日历的周期刷新原先挂在 Streamlit 页面模块里, 只有前端进程
常驻时才会运行; CLI 每次调用都只能按需回源。本模块把「从磁盘持仓/自选推导
刷新范围 → 走正常缓存链 → 回写 SQLite」抽出来, 只依赖取数与存储层, 不 import
streamlit, 因而可以在 headless CLI / 常驻服务里安全运行。

使用方式:
  - UI: app 入口调用 ensure_warmers(); 页面看门狗复用 start_refresh /
    refresh_state 协调器, 同数据面不会重复发网络;
  - CLI: `tracker warm` 调用 warm_once() 做一次预热, --interval 则常驻。

进程内协调器 (inflight/完成时间) 按进程隔离: UI 与 CLI 同时运行可能各发一次
同一批请求, 数据正确性不受影响 (SQLite WAL 支持并发读写)。
"""

from __future__ import annotations

import logging
import threading
import time

from .. import events as events_mod
from .. import prices
from .. import storage
from ..cache import CACHE_TTL, STALE_TTL
from ..fx import get_fx_rates
from ..symbols import parse
from .watchlist import entries_for, merge_entries

logger = logging.getLogger(__name__)

# ---------- 行情 + 汇率保温 ----------

#: 同一数据面两次后台刷新的最短间隔: 刷新失败 (网络中断/代码失效) 时避免
#: 高频重试 (失败项在 providers 层另有 5min 负缓存兜底)
REFRESH_MIN_INTERVAL = 60.0
#: 基准周期 = 行情新鲜期 (300s): 早于它触发时 get_quotes 仍会命中磁盘缓存,
#: 只是空转一轮并把「完成」记账, 真正回源反而被推迟到下一个周期。
REFRESH_PERIOD = float(CACHE_TTL)
#: 符号集越大周期越长 (大组合一轮刷新就要数分钟, 周期不拉长会把刷新器拖到
#: 常驻满负荷, Yahoo 限流是 IP 级的, 连累 K线页), 上限 = 陈旧期。
REFRESH_PERIOD_PER_SYMBOL = 2.0
REFRESH_PERIOD_MAX = float(STALE_TTL)
#: 保温线程的检查粒度: 磁盘年龄才是刷新依据, 每 60s 查一次才能让实际回源
#: 贴近缓存过期时刻 (而不是睡满一个周期后才刚好看一眼)。
REFRESH_TICK = 60.0
#: _refresh_done_at 保留的最近完成记录数 (超出按时间戳淘汰, 防长驻服务内存增长)
REFRESH_HISTORY = 64

_refresh_lock = threading.Lock()
#: key → 是否正在刷新 (进程内跨会话共享: 同数据面多个标签页只发一次网络)
_refresh_inflight: set[tuple] = set()
#: key → 最近一次刷新完成时间戳 (看门狗据此 rerun / 节流重试 / 周期保温)
_refresh_done_at: dict[tuple, float] = {}

_refresher_lock = threading.Lock()
_refresher_started = False


def refresh_period(n_symbols: int) -> float:
    """保温周期 (秒): 基准 300s 起, 按符号集规模拉长, 上限 30min."""
    return min(
        max(REFRESH_PERIOD, n_symbols * REFRESH_PERIOD_PER_SYMBOL),
        REFRESH_PERIOD_MAX,
    )


def refresh_key(plan: dict) -> tuple:
    """刷新任务键: 行情参数 + 汇率参数 (任一变化都是新任务)."""
    return (plan.get("quotes"), plan.get("fx"))


def refresh_state(key: tuple) -> tuple[bool, float | None]:
    """返回 (是否正在刷新, 最近完成时间戳)."""
    with _refresh_lock:
        return key in _refresh_inflight, _refresh_done_at.get(key)


def start_refresh(key: tuple, plan: dict) -> bool:
    """启动一次后台刷新 (行情 + 汇率); 返回是否真正发起.

    同 key 不并发 (多标签页/连续 rerun 只发一次网络) + 最短重试间隔 (刷新
    失败后避免看门狗高频重试)。线程内只跑纯取数链路 (providers/akshare +
    SQLite, 不碰任何 st.* API): st.cache_data 依赖 ScriptRunner 上下文, 后台
    线程调用会炸; 磁盘缓存的读取在 get_quotes/get_fx_rates 内部, 新鲜项
    仍秒回不发网络。
    """
    q_args = plan.get("quotes")
    f_args = plan.get("fx")
    now = time.time()
    with _refresh_lock:
        if key in _refresh_inflight:
            return False
        if now - _refresh_done_at.get(key, 0.0) < REFRESH_MIN_INTERVAL:
            return False
        _refresh_inflight.add(key)
        # 长驻服务里键会随切范围/改持仓无限累积: 只留最近 N 个
        if len(_refresh_done_at) > REFRESH_HISTORY:
            for old in sorted(_refresh_done_at, key=_refresh_done_at.get)[
                : len(_refresh_done_at) - REFRESH_HISTORY
            ]:
                _refresh_done_at.pop(old, None)

    def _run() -> None:
        try:
            if q_args:
                symbols, prefer_akshare, use_ibkr, use_longport, crypto_source = q_args
                prices.get_quotes(
                    list(symbols),
                    prefer_akshare=prefer_akshare,
                    use_ibkr=use_ibkr,
                    use_longport=use_longport,
                    crypto_source=crypto_source,
                )
            if f_args:
                base, currencies, use_ibkr = f_args
                get_fx_rates(base, list(currencies), use_ibkr=use_ibkr)
        except Exception:  # noqa: BLE001 - 刷新失败: 陈旧数据继续展示, 下轮重试
            # 静默 pass 会让持续失败退化成无诊断的热循环 (每轮重试都视为
            # "完成"), 至少留下日志痕迹定位是哪个数据面刷不动
            logger.warning(
                "后台刷新失败 (key=%s), 下轮重试", key, exc_info=True
            )
        finally:
            with _refresh_lock:
                _refresh_inflight.discard(key)
                _refresh_done_at[key] = time.time()

    threading.Thread(target=_run, name="quote-refresh", daemon=True).start()
    return True


def cache_age(symbols=(), fx=None) -> float:
    """磁盘缓存覆盖数据的最旧年龄 (秒); 有代码缺缓存 → inf (视为必须刷).

    后台保温线程用 (没有 session 计划可读), 与页面 _plan_data_age 同口径。
    """
    from .. import cache as cache_mod

    symbols = tuple(dict.fromkeys(symbols))
    worst = 0.0
    if symbols:
        hits, ages = cache_mod.get_cached_with_age(list(symbols), ttl=10**9)
        if len(hits) < len(symbols):
            return float("inf")
        if ages:
            worst = max(worst, max(ages.values()))
    if fx:
        base, currencies, _use_ibkr = fx
        need = [c for c in currencies if c != base]
        if need:
            rates, f_ages = cache_mod.get_fx_cached(base, need, ttl=10**9)
            if len(rates) < len(need):
                return float("inf")
            if f_ages:
                worst = max(worst, max(f_ages.values()))
    return worst


def _symbols_from_rows(rows) -> list[str]:
    """持仓/自选行 → 规范 Yahoo 代码 (去重保序; 无法识别的静默跳过)."""
    out: list[str] = []
    for row in rows:
        raw = str((row or {}).get("symbol") or "").strip()
        if not raw:
            continue
        try:
            yahoo = parse(raw).yahoo
        except ValueError:
            continue
        if yahoo not in out:
            out.append(yahoo)
    return out


def _read_scope(portfolio_path=None, watchlist_path=None) -> tuple[dict, list[str]]:
    """读磁盘持仓/自选; 读盘失败时返回空 scope (调用方按无数据处理)."""
    try:
        portfolio = storage.load_portfolio(
            portfolio_path or storage.PORTFOLIO_PATH
        )
    except Exception:  # noqa: BLE001 - 组合缺失时自选域照旧
        portfolio = {}
    try:
        watch = storage.load_watchlist(
            watchlist_path or storage.WATCHLIST_PATH
        )
    except Exception:  # noqa: BLE001
        watch = {}
    holdings = [h for h in portfolio.get("holdings", []) if h.get("symbol")]
    watch_rows = [w for w in merge_entries(watch) if w.get("symbol")]
    return portfolio, _symbols_from_rows([*holdings, *watch_rows])


def disk_refresh_plan(
    *,
    portfolio_path=None,
    watchlist_path=None,
    extra_symbols=(),
) -> dict | None:
    """从磁盘数据 (持仓/自选/设置) 推导刷新参数; 无代码时返回 None.

    不读 session_state: 后台线程跨会话共享, 且页面可能一个都没开。币种从
    磁盘行情缓存推导 (与页面 _load_fx 同源, 保证 key 一致可被协调器去重)。
    extra_symbols 供 CLI `tracker warm --symbols` 扩展预热范围。
    """
    try:
        settings = storage.load_settings()
    except Exception:  # noqa: BLE001 - 读盘失败: 下个周期再试
        return None
    portfolio, symbols = _read_scope(portfolio_path, watchlist_path)
    extra = _symbols_from_rows([{"symbol": s} for s in extra_symbols])
    symbols = list(dict.fromkeys([*symbols, *extra]))
    if not symbols:
        return None

    q_args = (
        tuple(symbols),
        settings["prefer_akshare"],
        settings["use_ibkr"],
        bool(settings.get("use_longport")),
        settings["crypto_source"],
    )
    base = portfolio.get("base_currency", "CNY")
    from .. import cache as cache_mod

    hits, _ages = cache_mod.get_cached_with_age(symbols, ttl=10**9)
    # 币种排除 base (与页面 _load_fx 的 need 同口径, 保证刷新 key 一致可去重)
    currencies = tuple(
        sorted({q.currency for q in hits.values() if q.currency != base})
    )
    f_args = (base, currencies, settings["use_ibkr"]) if currencies else None
    return {
        "quotes": q_args,
        "fx": f_args,
        # 仅 warm_once 用来补齐「首次冷启动还没有行情缓存」时的汇率范围
        "base": base,
        "use_ibkr": settings["use_ibkr"],
    }


def background_refresher_loop(plan_fn=None) -> None:
    """保温线程主循环: 每 60s 检查磁盘数据年龄, 到周期才刷新.

    检查粒度固定 60s, 刷新周期按符号集规模缩放 (见 refresh_period):
    这样实际回源贴近缓存过期时刻, 而不用睡满整个周期才看一次年龄。
    年龄判定与页面看门狗同口径 (cache_age): 页面刚刷新的数据这里会跳过,
    不重复发网络。任何异常都吞掉续命 —— 保温失败不该影响服务本身。
    """
    while True:
        time.sleep(REFRESH_TICK)
        try:
            plan = (plan_fn or disk_refresh_plan)()
            if not plan or not (plan.get("quotes") or plan.get("fx")):
                continue
            q_args = plan.get("quotes") or ()
            symbols = q_args[0] if q_args else ()
            period = refresh_period(len(symbols))
            # 年龄判定与页面看门狗同口径: 页面刚刷新的数据这里会跳过
            if cache_age(symbols, plan.get("fx")) < period:
                continue
            start_refresh(refresh_key(plan), plan)
        except Exception:  # noqa: BLE001 - 保温失败不影响服务
            # 吞掉续命但留日志: 持续失败 (如缓存库损坏) 不能完全无痕迹
            logger.warning("保温线程刷新失败", exc_info=True)


def ensure_background_refresher(plan_fn=None) -> None:
    """启动进程级后台保温线程 (每个进程一条, 惰性启动, 重复调用无副作用).

    看门狗片段只在组合页开着时轮询; 这条线程让服务在没有任何页面打开时也
    持续刷新磁盘缓存 —— 之后任何人打开都是新鲜数据秒开, 保存了新持仓的
    下一个周期也会自动补取 (无需等页面渲染)。与页面级刷新共用协调器,
    不会重复发网络。
    """
    global _refresher_started
    with _refresher_lock:
        if _refresher_started:
            return
        _refresher_started = True
    threading.Thread(
        target=background_refresher_loop,
        args=(plan_fn,),
        name="quote-refresher",
        daemon=True,
    ).start()


# ---------- 财经日历预热 ----------

CAL_REFRESH_MIN_INTERVAL = 60.0
CAL_REFRESH_HISTORY = 64

# 进程级缓存预热: 服务启动后按默认范围 (持仓+自选, 本月+下月) 周期性走
# 缓存链刷新磁盘日历。日历 TTL 6h, 30 分钟检查一次即可; 首次延迟避开启动
# 风暴 (与组合页保温线程同思路, 但日历低频, 周期不需要更短)。
CAL_WARM_PERIOD = 1800.0
CAL_WARM_FIRST_DELAY = 60.0
# 预热只覆盖页面默认窗口/默认兜底开关; 切到其它月份/长桥时按需取数,
# 已缓存的单票/报表期数据仍然共享。
CAL_WARM_MONTHS = 2
CAL_WARM_USE_LONGPORT = False

_cal_refresh_lock = threading.Lock()
_cal_refresh_inflight: set[tuple] = set()
_cal_refresh_done_at: dict[tuple, float] = {}

_cal_refresher_lock = threading.Lock()
_cal_refresher_started = False


def calendar_key(symbols, months: int, use_longport: bool) -> tuple:
    return (tuple(symbols), int(months), bool(use_longport))


def calendar_refresh_state(key: tuple) -> tuple[bool, float | None]:
    with _cal_refresh_lock:
        return key in _cal_refresh_inflight, _cal_refresh_done_at.get(key)


def _fetch_calendar(symbols, months: int, use_longport: bool):
    """真正取数: 新鲜键直接命中磁盘, 只对过期/缺失键回源."""
    return events_mod.fetch_calendar(
        list(symbols),
        months=int(months),
        refresh=False,
        use_longport=bool(use_longport),
    )


def start_calendar_refresh(
    key: tuple, symbols, months: int, use_longport: bool
) -> bool:
    """启动一次后台日历重取 (过期/缺失键才发网络); 返回是否真正发起.

    与组合页同款协调器: 同 key 不并发 (多标签页只发一次), 失败后按最短
    间隔节流。线程内只走 events → SQLite, 不碰任何 st.* API。
    """
    now = time.time()
    with _cal_refresh_lock:
        if key in _cal_refresh_inflight:
            return False
        if now - _cal_refresh_done_at.get(key, 0.0) < CAL_REFRESH_MIN_INTERVAL:
            return False
        _cal_refresh_inflight.add(key)
        if len(_cal_refresh_done_at) > CAL_REFRESH_HISTORY:
            for old in sorted(_cal_refresh_done_at, key=_cal_refresh_done_at.get)[
                : len(_cal_refresh_done_at) - CAL_REFRESH_HISTORY
            ]:
                _cal_refresh_done_at.pop(old, None)

    def _run() -> None:
        try:
            _fetch_calendar(symbols, months, use_longport)
        except Exception:  # noqa: BLE001 - 旧数据继续展示, 下轮再试
            logger.warning(
                "日历后台刷新失败 (key=%s), 下轮重试", key, exc_info=True
            )
        finally:
            with _cal_refresh_lock:
                _cal_refresh_inflight.discard(key)
                _cal_refresh_done_at[key] = time.time()

    threading.Thread(target=_run, name="calendar-refresh", daemon=True).start()
    return True


def calendar_scope_symbols(
    *,
    portfolio_path=None,
    watchlist_path=None,
    extra_symbols=(),
) -> list[str]:
    """磁盘上的默认预热范围: 持仓 + 全部自选 (与页面「持仓+自选」同口径)."""
    try:
        portfolio = storage.load_portfolio(
            portfolio_path or storage.PORTFOLIO_PATH
        )
    except Exception:  # noqa: BLE001
        portfolio = {}
    try:
        watch = storage.load_watchlist(
            watchlist_path or storage.WATCHLIST_PATH
        )
    except Exception:  # noqa: BLE001
        watch = {}
    holdings = [h for h in portfolio.get("holdings", []) if h.get("symbol")]
    watch_rows = [w for w in entries_for(watch, None) if w.get("symbol")]
    extra = [{"symbol": s} for s in extra_symbols]
    return _symbols_from_rows([*holdings, *watch_rows, *extra])


def calendar_warm_plan(
    *,
    portfolio_path=None,
    watchlist_path=None,
    extra_symbols=(),
) -> tuple | None:
    """预热任务参数 (与 start_calendar_refresh 的签名对齐); 无代码返回 None."""
    symbols = tuple(
        calendar_scope_symbols(
            portfolio_path=portfolio_path,
            watchlist_path=watchlist_path,
            extra_symbols=extra_symbols,
        )
    )
    if not symbols:
        return None
    key = calendar_key(symbols, CAL_WARM_MONTHS, CAL_WARM_USE_LONGPORT)
    return key, symbols, CAL_WARM_MONTHS, CAL_WARM_USE_LONGPORT


def calendar_warm_tick(**kwargs) -> None:
    """执行一次预热检查: 缓存新鲜时只读盘, 过期/缺失才回源 (正常缓存链)."""
    plan = calendar_warm_plan(**kwargs)
    if plan is not None:
        start_calendar_refresh(*plan)


def background_calendar_refresher_loop() -> None:
    """进程级日历保温线程: 周期性按默认范围刷新磁盘缓存, 页面打开即命中.

    线程内只走 events/akshare/yfinance + SQLite, 不碰任何 st.* API; 与页面
    级刷新共用 start_calendar_refresh 协调器, 同 key 不会重复发网络。
    """
    time.sleep(CAL_WARM_FIRST_DELAY)
    while True:
        try:
            calendar_warm_tick()
        except Exception:  # noqa: BLE001 - 预热失败不影响服务, 下轮再试
            logger.warning("日历缓存预热失败, 下轮重试", exc_info=True)
        time.sleep(CAL_WARM_PERIOD)


def ensure_calendar_refresher() -> None:
    """启动进程级日历缓存预热线程 (每进程一条, 幂等)."""
    global _cal_refresher_started
    with _cal_refresher_lock:
        if _cal_refresher_started:
            return
        _cal_refresher_started = True
    threading.Thread(
        target=background_calendar_refresher_loop,
        name="calendar-warm",
        daemon=True,
    ).start()


def ensure_warmers() -> None:
    """一次拉起行情/汇率 + 日历两条保温线程 (幂等)."""
    ensure_background_refresher()
    ensure_calendar_refresher()


# ---------- 一次性预热 (CLI) ----------


def warm_quotes_once(
    *, extra_symbols=(), refresh: bool = False
) -> tuple[dict, dict | None]:
    """同步预热一次行情 + 汇率; 返回 (行情摘要, 汇率摘要或 None).

    与后台线程不同, 这里在行情取完后按实际返回的币种补汇率 —— 冷启动
    还没有行情缓存时, disk_refresh_plan 的 fx 参数是空的, 不能直接照抄。
    """
    plan = disk_refresh_plan(extra_symbols=extra_symbols)
    if not plan or not plan.get("quotes"):
        return (
            {"symbols": [], "ok": 0, "errors": {}, "notes": [], "skipped": True},
            None,
        )
    symbols, prefer_akshare, use_ibkr, use_longport, crypto_source = plan["quotes"]
    quotes, errors, notes = prices.get_quotes(
        list(symbols),
        prefer_akshare=prefer_akshare,
        use_ibkr=use_ibkr,
        use_longport=use_longport,
        crypto_source=crypto_source,
        refresh=refresh,
    )
    base = str(plan.get("base") or "CNY").strip().upper() or "CNY"
    currencies = sorted(
        {
            str(q.currency).upper()
            for q in quotes.values()
            if getattr(q, "currency", None) and str(q.currency).upper() != base
        }
    )
    if not currencies and plan.get("fx"):
        currencies = list(plan["fx"][1])
    quote_summary = {
        "symbols": list(symbols),
        "ok": len(quotes),
        "errors": errors,
        "notes": notes,
        "skipped": False,
    }
    fx_summary = None
    if currencies:
        rates, missing = get_fx_rates(
            base, currencies, use_ibkr=bool(plan.get("use_ibkr"))
        )
        fx_summary = {
            "base": base,
            "currencies": currencies,
            "ok": len(rates),
            "missing": missing,
        }
    return quote_summary, fx_summary


def warm_calendar_once(*, extra_symbols=()) -> dict | None:
    """同步预热一次财经日历 (默认窗口/默认兜底开关); 无代码返回 None."""
    plan = calendar_warm_plan(extra_symbols=extra_symbols)
    if plan is None:
        return None
    _key, symbols, months, use_longport = plan
    data = events_mod.fetch_calendar(
        list(symbols),
        months=months,
        refresh=False,
        use_longport=use_longport,
    )
    events = (data or {}).get("events") or []
    return {
        "symbols": list(symbols),
        "months": months,
        "use_longport": use_longport,
        "events": len(events),
        "cache": (data or {}).get("cache") or {},
        "issues": (data or {}).get("issues") or [],
    }


def warm_once(
    *,
    include_quotes: bool = True,
    include_calendar: bool = True,
    extra_symbols=(),
    refresh: bool = False,
) -> dict:
    """同步执行一次预热 (CLI `tracker warm` 用), 返回可序列化摘要."""
    started = time.time()
    result: dict = {}
    if include_quotes:
        quotes, fx = warm_quotes_once(
            extra_symbols=extra_symbols, refresh=refresh
        )
        result["quotes"] = quotes
        result["fx"] = fx
    if include_calendar:
        result["calendar"] = warm_calendar_once(extra_symbols=extra_symbols)
    result["elapsed_seconds"] = round(time.time() - started, 2)
    return result
