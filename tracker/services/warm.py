"""无 UI 依赖的缓存保温/预热服务 (Streamlit 与 CLI 共用).

背景: 行情/汇率/日历/持仓K线与估值快照的周期刷新原先挂在 Streamlit 页面
模块里, 只有前端进程常驻时才会运行; CLI 每次调用都只能按需回源。本模块把
「从磁盘持仓/自选推导刷新范围 → 走正常缓存链 → 回写 SQLite」抽出来, 只依赖
取数与存储层, 不 import streamlit, 因而可以在 headless CLI / 常驻服务里安全运行。
行情全量一轮约 30s (Yahoo v7 批量), 常驻周期默认按 300s 新鲜期缩放。

使用方式:
  - UI: app 入口调用 ensure_warmers(); 页面看门狗复用 start_refresh /
    refresh_state 协调器, 同数据面不会重复发网络;
  - CLI: `tracker warm` 调用 warm_once() 做一次预热, --interval 则常驻。

跨进程单例: 常驻 CLI (`warm --interval`) 与 Streamlit 的 ensure_warmers()
都会抢 var/warm.lock 的独占 flock, 先启动者成为唯一 leader 并负责后台保温,
后启动者只保留页面级按需刷新 —— 否则两个进程各刷同一批 704 只, 会放大
数据源限流与失败率。leader 退出/崩溃后锁自动释放, 另一个进程下次检查时
再接管。

进程内协调器 (inflight/完成时间) 按进程隔离: UI 与 CLI 同时运行可能各发一次
同一批请求, 数据正确性不受影响 (SQLite WAL 支持并发读写)。
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:  # Unix/macOS: 跨进程文件锁
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:  # Windows: 退化为字节范围锁
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - Unix/macOS
    _msvcrt = None

from .. import events as events_mod
from .. import prices
from .. import storage
from ..cache import CACHE_TTL, STALE_TTL
from ..fx import get_fx_rates
from ..symbols import Market, canonical, parse
from .refresh import RefreshCoordinator
from .watchlist import entries_for

logger = logging.getLogger(__name__)

# ---------- 行情 + 汇率保温 ----------

#: 同一数据面两次后台刷新的最短间隔: 刷新失败 (网络中断/代码失效) 时避免
#: 高频重试 (失败项在 providers 层另有 5min 负缓存兜底)
REFRESH_MIN_INTERVAL = 60.0
#: 基准周期 = 行情新鲜期 (300s): 早于它触发时 get_quotes 仍会命中磁盘缓存,
#: 只是空转一轮并把「完成」记账, 真正回源反而被推迟到下一个周期。
REFRESH_PERIOD = float(CACHE_TTL)
#: 符号集越大周期越长 (大组合一轮刷新就要数分钟, 周期不拉长会把刷新器拖到
#: 常驻满负荷)。行情已改 Yahoo v7 批量 (100 个/请求), 706 只一轮约 30s,
#: 按 0.4s/只缩放; 700 只 ≈ 300s, 与 CACHE_TTL 对齐, 上限仍是陈旧期。
REFRESH_PERIOD_PER_SYMBOL = 0.4
REFRESH_PERIOD_MAX = float(STALE_TTL)
#: 保温线程的检查粒度: 磁盘年龄才是刷新依据, 每 60s 查一次才能让实际回源
#: 贴近缓存过期时刻 (而不是睡满一个周期后才刚好看一眼)。
REFRESH_TICK = 60.0
#: _refresh_done_at 保留的最近完成记录数 (超出按时间戳淘汰, 防长驻服务内存增长)
REFRESH_HISTORY = 64

_quote_coordinator = RefreshCoordinator(
    name="quote-refresh",
    min_interval=REFRESH_MIN_INTERVAL,
    history=REFRESH_HISTORY,
)
# 兼容旧调用方/测试直接观察协调器内部状态 (与类内是同一组对象)
_refresh_lock = _quote_coordinator.lock
_refresh_inflight = _quote_coordinator.inflight
_refresh_done_at = _quote_coordinator.done_at

_refresher_lock = threading.Lock()
_refresher_started = False

#: 孤儿缓存自动清理: 每 24h 最多一次 (常驻进程内计时, 重启后首轮会检查).
CACHE_PRUNE_INTERVAL = 24 * 3600.0
_last_cache_prune = 0.0
_cache_prune_lock = threading.Lock()

#: 无效/过期缓存 GC: 每周最多一次 (旧源签名 K线、过期序列/日历等).
CACHE_GC_INTERVAL = 7 * 24 * 3600.0
_last_cache_gc = 0.0
_cache_gc_lock = threading.Lock()


def _maybe_prune_orphans() -> dict | None:
    """按当前持仓/自选清理孤儿缓存; 每天最多一次, 失败不影响保温."""
    global _last_cache_prune
    now = time.time()
    with _cache_prune_lock:
        if now - _last_cache_prune < CACHE_PRUNE_INTERVAL:
            return None
        _last_cache_prune = now
    try:
        from .. import cache as cache_mod

        result = cache_mod.prune_orphans()
        if result.get("deleted"):
            logger.info("清理孤儿缓存: %s 条", result["deleted"])
        return result
    except Exception:  # noqa: BLE001 - 维护任务失败不能拖垮保温
        logger.warning("清理孤儿缓存失败", exc_info=True)
        return None


def _maybe_gc_invalid() -> dict | None:
    """清理无效/过期缓存; 每周最多一次, 失败不影响保温."""
    global _last_cache_gc
    now = time.time()
    with _cache_gc_lock:
        if now - _last_cache_gc < CACHE_GC_INTERVAL:
            return None
        _last_cache_gc = now
    try:
        from .. import cache as cache_mod

        result = cache_mod.gc_invalid()
        if result.get("deleted"):
            logger.info("清理无效缓存: %s 条", result["deleted"])
        return result
    except Exception:  # noqa: BLE001 - 维护任务失败不能拖垮保温
        logger.warning("清理无效缓存失败", exc_info=True)
        return None


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
    return _quote_coordinator.state(key)


def start_refresh(key: tuple, plan: dict) -> bool:
    """启动一次后台刷新 (行情 + 汇率); 返回是否真正发起.

    同 key 不并发 + 最短重试间隔由 RefreshCoordinator 统一处理。线程内只跑
    纯取数链路 (providers/akshare + SQLite, 不碰任何 st.* API):
    st.cache_data 依赖 ScriptRunner 上下文, 后台线程调用会炸; 磁盘缓存的
    读取在 get_quotes/get_fx_rates 内部, 新鲜项仍秒回不发网络。
    """
    q_args = plan.get("quotes")
    f_args = plan.get("fx")

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
        finally:
            # 保温/页面刷新大多是长驻进程, 健康事件不能等攒满 20 条才落盘;
            # 每轮结束立即 flush, 健康页才能看到刚发生的成功/失败。
            from .. import health

            health.flush()

    return _quote_coordinator.start(key, _run)


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
        yahoo = canonical((row or {}).get("symbol"))
        if yahoo and yahoo not in out:
            out.append(yahoo)
    return out


def _load_scope(portfolio_path=None, watchlist_path=None) -> tuple[dict, dict]:
    """读磁盘持仓/自选; 单侧读盘失败时按空数据继续 (不阻断另一侧保温)."""
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
    return portfolio, watch


def _scope_symbols(portfolio: dict, watch: dict, extra_symbols=()) -> list[str]:
    """持仓 + 全部自选 (+ 额外代码) → 规范 Yahoo 代码."""
    holdings = [h for h in portfolio.get("holdings", []) if h.get("symbol")]
    watch_rows = [w for w in entries_for(watch, None) if w.get("symbol")]
    extra = [{"symbol": s} for s in extra_symbols]
    return _symbols_from_rows([*holdings, *watch_rows, *extra])


def _read_scope(portfolio_path=None, watchlist_path=None) -> tuple[dict, list[str]]:
    """读磁盘持仓/自选; 读盘失败时返回空 scope (调用方按无数据处理)."""
    portfolio, watch = _load_scope(portfolio_path, watchlist_path)
    return portfolio, _scope_symbols(portfolio, watch)


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

_cal_coordinator = RefreshCoordinator(
    name="calendar-refresh",
    min_interval=CAL_REFRESH_MIN_INTERVAL,
    history=CAL_REFRESH_HISTORY,
    log_label="日历后台刷新",
)
# 兼容旧调用方/测试直接观察协调器内部状态 (与类内是同一组对象)
_cal_refresh_lock = _cal_coordinator.lock
_cal_refresh_inflight = _cal_coordinator.inflight
_cal_refresh_done_at = _cal_coordinator.done_at

_cal_refresher_lock = threading.Lock()
_cal_refresher_started = False


def calendar_key(symbols, months: int, use_longport: bool) -> tuple:
    return (tuple(symbols), int(months), bool(use_longport))


def calendar_refresh_state(key: tuple) -> tuple[bool, float | None]:
    return _cal_coordinator.state(key)


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
    return _cal_coordinator.start(
        key, lambda: _fetch_calendar(symbols, months, use_longport)
    )


def calendar_scope_symbols(
    *,
    portfolio_path=None,
    watchlist_path=None,
    extra_symbols=(),
) -> list[str]:
    """磁盘上的默认预热范围: 持仓 + 全部自选 (与页面「持仓+自选」同口径)."""
    portfolio, watch = _load_scope(portfolio_path, watchlist_path)
    return _scope_symbols(portfolio, watch, extra_symbols)


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


# ---------- 跨进程共享保温 leader (唯一常驻保温) ----------

#: 常驻保温锁文件 (var/warm.lock); flock 随进程退出自动释放, 空文件无害
_WARM_LOCK_NAME = "warm.lock"
_leader_lock = threading.Lock()
_leader_file = None
_leader_role: str | None = None
_leader_denied_logged = False


def warm_lock_path() -> Path:
    """共享保温锁文件路径 (与行情缓存同目录)."""
    from ..paths import var_dir

    return var_dir() / _WARM_LOCK_NAME


def _lock_file_nonblocking(f) -> None:
    """对已打开文件加跨进程独占锁; 被占用时抛 OSError."""
    if _fcntl is not None:
        _fcntl.flock(f.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return
    if _msvcrt is not None:  # pragma: no cover - Windows
        f.seek(0)
        _msvcrt.locking(f.fileno(), _msvcrt.LK_NBLCK, 1)


def _unlock_file(f) -> None:
    try:
        if _fcntl is not None:
            _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        elif _msvcrt is not None:  # pragma: no cover - Windows
            f.seek(0)
            _msvcrt.locking(f.fileno(), _msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _ensure_lockable_byte(f) -> None:
    """Windows 字节锁要求文件至少 1 字节; Unix 不需要."""
    if _msvcrt is None or _fcntl is not None:  # pragma: no cover - Windows
        return
    f.seek(0, os.SEEK_END)
    if f.tell() == 0:
        f.write(" ")
        f.flush()


def acquire_warm_leader(role: str = "warm") -> bool:
    """尝试成为全机唯一常驻保温 leader; 成功返回 True.

    锁用非阻塞 flock, 生命周期 = 进程生命周期 (文件句柄保存在模块全局)。
    已被其它进程持有时返回 False, 调用方只保留页面级按需刷新, 不应再起
    第二套后台线程。同一进程重复调用幂等返回 True。
    """
    global _leader_file, _leader_role, _leader_denied_logged

    with _leader_lock:
        if _leader_file is not None:
            return True
        path = warm_lock_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            f = open(path, "a+", encoding="utf-8")
        except OSError:
            # 锁文件都建不了 (只读数据目录) 时别连带停掉保温: 保持旧行为
            logger.warning("无法创建共享保温锁 %s, 本进程按独立保温启动", path)
            return True
        try:
            _ensure_lockable_byte(f)
            _lock_file_nonblocking(f)
        except OSError:
            f.close()
            return False
        try:
            if _fcntl is not None:
                f.seek(0)
                f.truncate()
            f.seek(0)
            f.write(f"{os.getpid()} {role}\n")
            f.flush()
        except OSError:
            pass  # 诊断文本写失败不影响锁本身
        _leader_file = f
        _leader_role = role
        _leader_denied_logged = False
        return True


def release_warm_leader() -> None:
    """主动释放共享保温锁 (CLI 常驻循环退出时调用; 进程退出会自动释放)."""
    global _leader_file, _leader_role

    with _leader_lock:
        f = _leader_file
        _leader_file = None
        _leader_role = None
    if f is not None:
        _unlock_file(f)
        f.close()


def warm_lock_held_by_other() -> bool:
    """探测其它进程是否持有共享保温锁 (不改变本进程 leader 状态)."""
    with _leader_lock:
        if _leader_file is not None:
            return False
    path = warm_lock_path()
    if not path.exists():
        return False
    try:
        f = open(path, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        _ensure_lockable_byte(f)
        _lock_file_nonblocking(f)
    except OSError:
        f.close()
        return True
    _unlock_file(f)
    f.close()
    return False


def leader_status() -> dict:
    """本进程/其它进程的共享保温 leader 状态 (doctor / 健康页展示)."""
    with _leader_lock:
        held = _leader_file is not None
        role = _leader_role
    return {
        "held": held,
        "role": role,
        "external": (not held) and warm_lock_held_by_other(),
    }


def ensure_warmers() -> None:
    """一次拉起行情/汇率 + 日历两条保温线程 (全机唯一 leader 才启动).

    常驻 CLI (`warm --interval`) 已持锁时, Streamlit 不再起第二套后台线程;
    只靠页面级按需刷新即可。若 leader 退出, 下一次 rerun 会自动接管。
    """
    global _leader_denied_logged

    if not acquire_warm_leader("streamlit"):
        if not _leader_denied_logged:
            logger.info("共享保温已由其它进程负责, 本进程跳过后台保温线程")
            _leader_denied_logged = True
        return
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


#: 持仓 K线保温窗口: 与 nav 默认 365 天回填一致 (ceil(365/30)+1 = 14 个月).
HISTORY_WARM_MONTHS = max(1, math.ceil(365 / 30) + 1)
#: 日线一天只变一次; 6h 内命中就不再回源, 避免 5min 行情周期重复拉历史.
HISTORY_WARM_TTL = 6 * 3600
HISTORY_WARM_WORKERS = 4


def warm_holdings_history_once(*, portfolio_path=None, extra_symbols=()) -> dict:
    """只按持仓 (可加 --symbols) 预热 K线, 不碰 702 只自选.

    目的: 修复「持仓无 K线缓存 → 净值回填跳过」; 自选只在 K线页按需取。
    6h 内已缓存直接命中, 所以高频行情保温不会顺带反复拉历史。
    """
    try:
        portfolio = storage.load_portfolio(
            portfolio_path or storage.PORTFOLIO_PATH
        )
    except Exception:  # noqa: BLE001 - 持仓文件坏了不影响其它保温
        portfolio = {}
    rows = [h for h in portfolio.get("holdings", []) if h.get("symbol")]
    rows += [{"symbol": s} for s in extra_symbols]
    symbols = _symbols_from_rows(rows)
    if not symbols:
        return {"symbols": [], "ok": 0, "errors": {}, "skipped": True}
    try:
        settings = storage.load_settings()
    except Exception:  # noqa: BLE001 - 读盘失败按默认源链
        settings = {}
    prefer_akshare = bool(settings.get("prefer_akshare", False))
    use_ibkr = bool(settings.get("use_ibkr", False))
    use_longport = bool(settings.get("use_longport", False))
    crypto_source = str(settings.get("crypto_source") or "auto")

    def _one(sym: str) -> tuple[str, str | None]:
        try:
            df = prices.get_ohlc(
                sym,
                months=HISTORY_WARM_MONTHS,
                prefer_akshare=prefer_akshare,
                use_ibkr=use_ibkr,
                use_longport=use_longport,
                crypto_source=crypto_source,
                cache_ttl=HISTORY_WARM_TTL,
            )
        except Exception as e:  # noqa: BLE001 - 单代码失败不拖垮其它持仓
            return sym, str(e) or type(e).__name__
        if df is None or df.empty:
            return sym, "无历史数据"
        return sym, None

    errors: dict[str, str] = {}
    with ThreadPoolExecutor(
        max_workers=min(HISTORY_WARM_WORKERS, len(symbols))
    ) as pool:
        for sym, err in pool.map(_one, symbols):
            if err:
                errors[sym] = err
    return {
        "symbols": symbols,
        "ok": len(symbols) - len(errors),
        "errors": errors,
        "skipped": False,
    }


VALUATION_WARM_WORKERS = 3


def warm_valuation_once(*, portfolio_path=None, extra_symbols=()) -> dict:
    """预热持仓 + PB/市值自选的估值快照 (基本面页/阈值提醒用).

    只覆盖真正用到估值指标的标的: 6 只持仓 + 9 只 PB/市值自选, 不把 700 只
    纯价格自选全预取。快照 6h 磁盘缓存, 5 分钟一轮行情保温只读盘; 首次/
    过期才回源。
    """
    portfolio, watch = _load_scope(portfolio_path, None)
    holdings = [h for h in portfolio.get("holdings", []) if h.get("symbol")]
    watch_rows = [
        e
        for e in (watch.get("watchlist") or [])
        if (
            e.get("metric") in ("pb", "mcap")
            or e.get("pb") is not None
            or e.get("mcap") is not None
        )
    ]
    rows = [*holdings, *watch_rows, *({"symbol": s} for s in extra_symbols)]
    symbols: list[str] = []
    for yahoo in _symbols_from_rows(rows):
        try:
            p = parse(yahoo)
        except ValueError:
            continue
        if p.market in (Market.CRYPTO, Market.INDEX):
            continue
        if p.yahoo not in symbols:
            symbols.append(p.yahoo)
    if not symbols:
        return {"symbols": [], "ok": 0, "errors": {}, "skipped": True}

    from .. import valuation

    def _one(sym: str) -> tuple[str, str | None]:
        try:
            valuation.fetch_snapshot(sym)
        except Exception as e:  # noqa: BLE001 - 单只失败不拖垮其它持仓
            return sym, str(e) or type(e).__name__
        return sym, None

    errors: dict[str, str] = {}
    with ThreadPoolExecutor(
        max_workers=min(VALUATION_WARM_WORKERS, len(symbols))
    ) as pool:
        for sym, err in pool.map(_one, symbols):
            if err:
                errors[sym] = err
    return {
        "symbols": symbols,
        "ok": len(symbols) - len(errors),
        "errors": errors,
        "skipped": False,
    }


def warm_once(
    *,
    include_quotes: bool = True,
    include_history: bool = True,
    include_valuation: bool = True,
    include_calendar: bool = True,
    extra_symbols=(),
    refresh: bool = False,
) -> dict:
    """同步执行一次预热 (CLI `tracker warm` 用), 返回可序列化摘要."""
    started = time.time()
    result: dict = {}
    try:
        pruned = _maybe_prune_orphans()
        if pruned is not None:
            result["cache_prune"] = pruned
        invalid = _maybe_gc_invalid()
        if invalid is not None:
            result["cache_gc"] = invalid
        if include_quotes:
            quotes, fx = warm_quotes_once(
                extra_symbols=extra_symbols, refresh=refresh
            )
            result["quotes"] = quotes
            result["fx"] = fx
        if include_history:
            result["history"] = warm_holdings_history_once(
                extra_symbols=extra_symbols
            )
        if include_valuation:
            result["valuation"] = warm_valuation_once(
                extra_symbols=extra_symbols
            )
        if include_calendar:
            result["calendar"] = warm_calendar_once(extra_symbols=extra_symbols)
        result["elapsed_seconds"] = round(time.time() - started, 2)
        return result
    finally:
        # 长驻保温进程不能等健康缓冲攒满 20 条: 重启会丢, 健康页也看不到
        # 本轮的真实批量结果 (尤其 yfinance 的 1 条批量事件)。
        from .. import health

        health.flush()


def status() -> dict:
    """进程内保温线程状态 (doctor / 数据健康页展示用).

    注意「是否启动」是进程级的: `tracker doctor` 是独立进程, 这里读到 False
    只说明本进程没启动保温线程, 不代表常驻服务没在跑。
    """
    with _refresher_lock:
        quote_started = _refresher_started
    with _cal_refresher_lock:
        cal_started = _cal_refresher_started
    with _refresh_lock:
        quote_inflight = len(_refresh_inflight)
        quote_last = max(_refresh_done_at.values()) if _refresh_done_at else None
    with _cal_refresh_lock:
        cal_inflight = len(_cal_refresh_inflight)
        cal_last = max(_cal_refresh_done_at.values()) if _cal_refresh_done_at else None
    return {
        "quotes": {
            "started": quote_started,
            "inflight": quote_inflight,
            "last_done_at": quote_last,
        },
        "calendar": {
            "started": cal_started,
            "inflight": cal_inflight,
            "last_done_at": cal_last,
        },
        "leader": leader_status(),
    }
