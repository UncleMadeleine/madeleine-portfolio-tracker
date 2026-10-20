"""「组合」页面 (默认页): 侧边栏持仓/自选编辑 + 主区总览/明细/配置/自选提醒.

从 app.py 拆出: app.py 只做 st.navigation 路由 (每页独立 URL, 刷新不丢页),
本模块承载组合页全部渲染逻辑与行情/汇率缓存.

首屏行情走 stale-while-revalidate (见 _load_quotes): 磁盘缓存 30 分钟内的
旧数据先渲染秒开, 后台线程静默重取, 看门狗片段 (_first_screen_watchdog)
在刷新完成后 rerun 换新; 只有冷启动 (无任何可用缓存) 才阻塞转圈。汇率
同理 (见 _load_fx): akshare/CFETS 冷启 ~10s 的即期查询同样落盘先用后刷。

数据持续更新由两路互补: 看门狗片段 (页面开着) 在磁盘数据临近过期时主动
重取, 用户看到的数据滚动更新; 进程级保温线程 (ensure_background_refresher,
app.py 入口启动) 在没有任何页面打开时照常刷磁盘缓存, 后来者打开即是新鲜
数据。两路共用刷新协调器 (_start_refresh), 同数据面不并发、失败有节流。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st

from tracker import cache as cache_mod, prices, storage
from tracker.analytics import build_view, by_symbol, summarize
from tracker.fx import get_fx_rates
from tracker.symbols import parse
from tracker.services.watchlist import (
    build_watchlist_view,
    entries_for,
    list_names,
    merge_entries,
    sort_watchlist,
    triggered_entries,
)
from tracker.watchlist import (
    normalize_watch_entry,
    parse_lists,
)
from tracker.ui import settings as S
from tracker.ui.kline_page import set_quick_symbols

WATCHLIST_PATH = storage.WATCHLIST_PATH

# 「按代码」饼图只展示市值前 N 大的代码, 其余合并为「其他」切片
SYMBOL_TOP = 20
# 饼图内标注占比的阈值: 低于该占比的切片太窄, 标签会互相压字 (改看图例/hover)
SYMBOL_LABEL_MIN_SHARE = 0.035


@st.cache_data(ttl=300, show_spinner=False)
def cached_quotes(
    symbols: tuple[str, ...],
    prefer_akshare: bool,
    use_ibkr: bool,
    use_longport: bool = False,
    crypto_source: str = "auto",
):
    return prices.get_quotes(
        list(symbols),
        prefer_akshare=prefer_akshare,
        use_ibkr=use_ibkr,
        use_longport=use_longport,
        crypto_source=crypto_source,
    )


@st.cache_data(ttl=600, show_spinner=False)
def cached_fx(base: str, currencies: tuple[str, ...], use_ibkr: bool = False):
    return get_fx_rates(base, list(currencies), use_ibkr=use_ibkr)


# ---------- 首屏数据: stale-while-revalidate (先渲染旧数据, 后台静默刷新) ----------
# 磁盘缓存 (行情 + 汇率) 两档有效期 (见 cache.py):
# - 新鲜期 (5min): 渲染即最终结果, 不发网络;
# - 陈旧期 (30min): 先渲染旧数据秒开首屏, 后台线程按新鲜期重取, 取完看门狗
#   片段 rerun 换新 —— 非美股行情本身延迟 15-30 分钟, 先看到数比干等 30s+
#   更可用 (冷进程下 akshare/CFETS 一次即期汇率就要 ~10s, 更不能挡首屏);
# 两档都 miss (冷启动/设置页清了磁盘缓存) 才阻塞转圈: 此时没有任何可展示的
# 数据, 等是唯一选择。
QUOTE_FRESH_TTL = cache_mod.CACHE_TTL
QUOTE_STALE_TTL = cache_mod.STALE_TTL
# 同一数据面两次后台刷新的最短间隔: 刷新失败 (网络中断/代码失效) 时避免看门狗
# 高频重试 (失败项在 providers 层另有 5min 负缓存兜底)
_REFRESH_MIN_INTERVAL = 60.0
# 看门狗片段轮询间隔 (秒): 后台刷新完成后尽快 rerun 换新数据
_REFRESH_POLL = 2.0
# _refresh_done_at 保留的最近完成记录数 (超出按时间戳淘汰, 防长驻服务内存增长)
_REFRESH_HISTORY = 64
# session_state 里的首屏刷新计划 (每次渲染重建, 看门狗据此调度)
_PLAN_KEY = "_first_screen_plan"

# ---------- 周期保温: 服务长期运行时数据持续更新 ----------
# 页面开着 → 看门狗片段按周期主动重取 (用户看到的数据滚动更新);
# 页面全关 → 进程级保温线程照常刷磁盘缓存 (后来者打开即是新鲜数据)。
# 基准周期小于行情新鲜期 (300s), 保证磁盘缓存常新; 符号集越大周期越长
# (大组合一轮刷新就要数分钟, 周期不拉长会把刷新器拖到常驻满负荷, Yahoo
# 限流是 IP 级的, 连累 K线页), 上限 = 陈旧期。
_REFRESH_PERIOD = 240.0
_REFRESH_PERIOD_PER_SYMBOL = 2.0
_REFRESH_PERIOD_MAX = 1800.0

_refresh_lock = threading.Lock()
# key → 是否正在刷新 (进程内跨会话共享: 同数据面多个标签页只发一次网络)
_refresh_inflight: set[tuple] = set()
# key → 最近一次刷新完成时间戳 (看门狗据此 rerun / 节流重试 / 周期保温)
_refresh_done_at: dict[tuple, float] = {}
# 进程级保温线程只启一次 (模块级标志 + 锁; importlib.reload 会重置, 无害)
_refresher_lock = threading.Lock()
_refresher_started = False


def _refresh_period(n_symbols: int) -> float:
    """保温周期 (秒): 基准 240s 起, 按符号集规模拉长, 上限 30min."""
    return min(
        max(_REFRESH_PERIOD, n_symbols * _REFRESH_PERIOD_PER_SYMBOL),
        _REFRESH_PERIOD_MAX,
    )


def _plan() -> dict:
    """当前会话的首屏刷新计划 (惰性创建; 原地更新, 字段跨渲染保留).

    - quotes/fx: 取数参数 (周期保温刷新要用)
    - dirty: 是否有待后台刷新的数据面
    - seen: 已消费的刷新完成时间戳 (只消费一次, 防热循环)
    - q_age/f_age: 行情/汇率磁盘缓存的最旧年龄 (秒), 周期保温据此判断
    """
    return st.session_state.setdefault(
        _PLAN_KEY,
        {
            "quotes": None,
            "fx": None,
            "dirty": False,
            "seen": 0.0,
            "q_age": 0.0,
            "f_age": 0.0,
        },
    )


def _plan_reset() -> None:
    """每次渲染重建刷新计划 (行情/汇率两个槽位由各自 loader 填写).

    只清脏标记, 其余字段原地保留: seen 丢失会让一次完成事件被反复当成
    "新完成"触发 rerun (数据仍脏时形成热循环); quotes/fx 参数与年龄丢失
    则周期保温无依据。
    """
    _plan()["dirty"] = False


def _plan_set(kind: str, args, dirty: bool = False) -> None:
    """记录取数参数 (周期保温刷新要用); dirty=True 表示需后台刷新.

    args=None 表示该数据面本渲染没有取数需求 (如无持仓 → 无汇率)。
    """
    plan = _plan()
    plan[kind] = args
    if dirty:
        plan["dirty"] = True


def _plan_key(plan: dict) -> tuple:
    """刷新任务键: 行情参数 + 汇率参数 (任一变化都是新任务)."""
    return (plan.get("quotes"), plan.get("fx"))


def _refresh_state(key: tuple) -> tuple[bool, float | None]:
    with _refresh_lock:
        return key in _refresh_inflight, _refresh_done_at.get(key)


def _start_refresh(key: tuple, plan: dict) -> bool:
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
        if now - _refresh_done_at.get(key, 0.0) < _REFRESH_MIN_INTERVAL:
            return False
        _refresh_inflight.add(key)
        # 长驻服务里键会随切范围/改持仓无限累积: 只留最近 _REFRESH_HISTORY 个
        if len(_refresh_done_at) > _REFRESH_HISTORY:
            for old in sorted(_refresh_done_at, key=_refresh_done_at.get)[
                : len(_refresh_done_at) - _REFRESH_HISTORY
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
            logging.getLogger(__name__).warning(
                "首屏后台刷新失败 (key=%s), 下轮重试", key, exc_info=True
            )
        finally:
            with _refresh_lock:
                _refresh_inflight.discard(key)
                _refresh_done_at[key] = time.time()

    threading.Thread(target=_run, name="first-screen-refresh", daemon=True).start()
    return True


def _local_quote_errors(fetch_symbols) -> dict[str, str]:
    """纯本地计算的低级错误 (代码无法识别), 不依赖刷新结果.

    首屏秒渲染路径上没有 get_quotes 的 errors 产出, 但 parse 失败是本地判断,
    补一遍让「无法识别」提示不丢失 (缺行情的代码仍由 build_view 报缺失)。
    """
    errors: dict[str, str] = {}
    for s in fetch_symbols:
        try:
            parse(s)
        except ValueError as e:
            errors[str(s)] = str(e)
    return errors


def _load_quotes(
    fetch_symbols: tuple[str, ...],
    prefer_akshare: bool,
    use_ibkr: bool,
    use_longport: bool,
    crypto_source: str,
) -> tuple[dict, dict, list]:
    """首屏行情加载 (stale-while-revalidate), 返回 (quotes, errors, notes).

    - 磁盘全部新鲜: 直接渲染 (读盘即返回, 不发网络);
    - 有陈旧缓存: 立即返回旧数据, 刷新计划交给看门狗片段调度 (不在此起线程
      —— 汇率计划随后才知情, 一个后台任务同时刷两者);
    - 无任何可用缓存: 阻塞拉取 (没有可展示的数据, 只能等).

    无法识别的代码永远取不到数, 不进刷新计划 (否则计划永久 dirty, 看门狗
    每间隔空转); 它们的报错由 errors 给出。
    """
    errors = _local_quote_errors(fetch_symbols)
    # 缓存读写两侧键都是 parse().yahoo 规范形态: 未保存的编辑器输入
    # (如 "aapl"/"600519.SH") 原样查缓存必 miss, 计划被永久标 dirty
    # 反复后台刷新。取数/查缓存一律先归一再去重, 报错仍保留用户原输入。
    fetchable = tuple(
        dict.fromkeys(
            _norm_sym(s) for s in fetch_symbols if str(s) not in errors
        )
    )
    hits, ages = cache_mod.get_cached_with_age(
        list(fetchable), ttl=QUOTE_STALE_TTL
    )
    args = (fetchable, prefer_akshare, use_ibkr, use_longport, crypto_source)
    # 参数始终记录 (周期保温要用), 只有陈旧/缺失才置脏
    plan = _plan()
    plan["quotes"] = args
    # 最旧条目年龄; 有代码缺缓存 → inf (视为必须刷)
    plan["q_age"] = (
        float("inf")
        if len(hits) < len(fetchable)
        else (max(ages.values()) if ages else 0.0)
    )
    stale = [s for s in fetchable if ages.get(s, float("inf")) > QUOTE_FRESH_TTL]
    if not stale:
        return hits, errors, []
    if not hits:
        with st.spinner("拉取行情 (首次加载需初始化数据引擎)..."):
            out = cached_quotes(
                fetchable, prefer_akshare, use_ibkr, use_longport, crypto_source
            )
        plan["q_age"] = 0.0  # 刚同步取完数, 不必立刻保温
        return out
    # 陈旧数据秒渲染 + 挂后台刷新
    plan["dirty"] = True
    return hits, errors, []


def _load_fx(
    base: str, currencies: tuple[str, ...], use_ibkr: bool
) -> tuple[dict, list]:
    """首屏汇率加载 (stale-while-revalidate), 返回 (rates, missing).

    与 _load_quotes 同构: 磁盘陈旧期内秒取 (含部分缺失 —— 缺失币种报
    「缺少汇率」, 后台补齐), 全空才阻塞拉取。base 自身恒为 1.0, 不落盘,
    因此不参与陈旧/缺失判定 (否则持有本币资产时计划永久 dirty)。
    """
    need = tuple(c for c in currencies if c != base)
    f_args = (base, need, use_ibkr) if need else None
    # 参数始终记录 (周期保温要用), 只有陈旧/缺失才置脏
    plan = _plan()
    plan["fx"] = f_args
    if not need:
        plan["f_age"] = 0.0
        return ({base: 1.0} if currencies else {}), []
    hits, ages = cache_mod.get_fx_cached(base, list(need), ttl=QUOTE_STALE_TTL)
    missing = [c for c in need if c not in hits]
    plan["f_age"] = (
        float("inf") if missing else (max(ages.values()) if ages else 0.0)
    )
    stale = [c for c in need if ages.get(c, float("inf")) > QUOTE_FRESH_TTL]
    if not stale:
        return ({**hits, base: 1.0}), missing
    if not hits:
        with st.spinner("拉取汇率..."):
            out = cached_fx(base, currencies, use_ibkr)
        plan["f_age"] = 0.0  # 刚同步取完数, 不必立刻保温
        return out
    # 陈旧汇率秒取 + 挂后台刷新
    plan["dirty"] = True
    return ({**hits, base: 1.0}), missing


@st.fragment(run_every=_REFRESH_POLL)
def _first_screen_watchdog() -> None:
    """首屏数据 (行情+汇率) 后台刷新看门狗 (独立片段, 轮询不阻塞其它交互).

    - 刷新完成 (且本次完成事件还没消费过) → st.rerun() 全量重渲染换新;
      完成事件按 plan["seen"] 只消费一次, 避免数据仍脏时反复 rerun 成热循环;
    - 已消费过完成事件但数据仍陈旧 (全部源失败/负缓存/代码失效) → 按最短
      间隔补刷, 不热重试;
    - 数据虽新鲜但磁盘年龄已到保温周期 → 主动重取, 让长期开着的页面数据
      持续滚动更新 (服务长期运行的主要场景);
    - 无待处理刷新 → 直接返回 (仅一次 session_state 读取).
    """
    plan = st.session_state.get(_PLAN_KEY)
    if not plan:
        return
    key = _plan_key(plan)
    inflight, done = _refresh_state(key)
    if not plan.get("dirty") and not inflight:
        # 周期保温: 没有可取数的参数 (无持仓也无自选) 时直接退出
        if not (plan.get("quotes") or plan.get("fx")):
            return
        if _plan_data_age(plan) < _refresh_period(_plan_symbol_count(plan)):
            return
        plan["dirty"] = True  # 复用脏路径: 发起 → 完成 → rerun 换新
    if inflight:
        st.caption(":material/sync: 数据后台刷新中, 先展示缓存数据…")
        return
    if done is not None and done > (plan.get("seen") or 0.0):
        plan["seen"] = done
        st.rerun()
        return
    # 完成事件已消费但数据仍脏 (网络中断/代码失效): 到最短间隔才补刷
    if done is None or time.time() - done >= _REFRESH_MIN_INTERVAL:
        if _start_refresh(key, plan):
            plan["base"] = time.time()
            st.caption(":material/sync: 数据后台刷新中, 先展示缓存数据…")


def _plan_symbol_count(plan: dict) -> int:
    q_args = plan.get("quotes") or ()
    return len(q_args[0]) if q_args else 0


def _plan_data_age(plan: dict) -> float:
    """计划覆盖数据的磁盘年龄 (秒): 取行情/汇率两者最旧; 缺数据视为无限旧."""
    return max(plan.get("q_age") or 0.0, plan.get("f_age") or 0.0)


def _cache_age(symbols=(), fx=None) -> float:
    """磁盘缓存覆盖数据的最旧年龄 (秒); 有代码缺缓存 → inf (视为必须刷).

    后台保温线程用 (没有 session 计划可读), 与页面 _plan_data_age 同口径。
    """
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


def ensure_background_refresher() -> None:
    """启动进程级后台保温线程 (每个服务进程一条, 惰性启动, 重复调用无副作用).

    看门狗片段只在组合页开着时轮询; 这条线程让服务在没有任何页面打开时也
    持续刷新磁盘缓存 —— 之后任何人打开都是新鲜数据秒开, 保存了新持仓的
    下一个周期也会自动补取 (无需等页面渲染)。与页面级刷新共用协调器
    (_start_refresh 的 inflight/间隔去重), 不会重复发网络。
    """
    global _refresher_started
    with _refresher_lock:
        if _refresher_started:
            return
        _refresher_started = True
    threading.Thread(
        target=_background_refresher_loop, name="quote-refresher", daemon=True
    ).start()


def _disk_refresh_plan() -> dict | None:
    """从磁盘数据 (持仓/自选/设置) 推导刷新参数; 无代码时返回 None.

    不读 session_state: 后台线程跨会话共享, 且页面可能一个都没开。币种从
    磁盘行情缓存推导 (与页面 _load_fx 同源, 保证 key 一致可被协调器去重)。
    """
    try:
        settings = storage.load_settings()
        portfolio = storage.load_portfolio(storage.PORTFOLIO_PATH)
        watch = storage.load_watchlist(WATCHLIST_PATH)
    except Exception:  # noqa: BLE001 - 读盘失败: 下个周期再试
        return None
    symbols = [
        str(h.get("symbol", "")).strip() for h in portfolio.get("holdings", [])
    ]
    symbols += [str(e["symbol"]) for e in merge_entries(watch)]
    symbols = tuple(dict.fromkeys(s for s in symbols if s))
    if not symbols:
        return None
    q_args = (
        symbols,
        settings["prefer_akshare"],
        settings["use_ibkr"],
        bool(settings.get("use_longport")),
        settings["crypto_source"],
    )
    base = portfolio.get("base_currency", "CNY")
    hits, _ages = cache_mod.get_cached_with_age(list(symbols), ttl=10**9)
    # 币种排除 base (与页面 _load_fx 的 need 同口径, 保证刷新 key 一致可去重)
    currencies = tuple(
        sorted({q.currency for q in hits.values() if q.currency != base})
    )
    f_args = (base, currencies, settings["use_ibkr"]) if currencies else None
    return {"quotes": q_args, "fx": f_args}


def _background_refresher_loop() -> None:
    """保温线程主循环: 每周期检查磁盘数据年龄, 旧了才刷新.

    首周期等满一个基准周期再动, 避开服务启动风暴 (页面打开时首屏 SWR 已
    负责立即刷新); 之后周期按符号集规模缩放。年龄判定与页面看门狗同口径
    (_cache_age): 页面刚刷新的数据这里会跳过, 不重复发网络。任何异常都
    吞掉续命 —— 保温失败不该影响服务本身。
    """
    period = _REFRESH_PERIOD
    while True:
        time.sleep(period)
        try:
            plan = _disk_refresh_plan()
            if not plan or not (plan.get("quotes") or plan.get("fx")):
                continue
            q_args = plan.get("quotes") or ()
            symbols = q_args[0] if q_args else ()
            period = _refresh_period(len(symbols))
            # 年龄判定与页面看门狗同口径: 页面刚刷新的数据这里会跳过
            if _cache_age(symbols, plan.get("fx")) < period:
                continue
            _start_refresh(_plan_key(plan), plan)
        except Exception:  # noqa: BLE001 - 保温失败不影响服务
            # 吞掉续命但留日志: 持续失败 (如缓存库损坏) 不能完全无痕迹
            logging.getLogger(__name__).warning(
                "保温线程刷新失败 (period=%ss)", period, exc_info=True
            )


def _post_import() -> None:
    """导入写盘后清行情/汇率缓存, 让新持仓立即取数."""
    cached_quotes.clear()
    cached_fx.clear()


def fmt(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{digits}f}"


def _norm_sym(sym: str) -> str:
    """代码 → Yahoo 规范形式; 无法识别时退回原始大写 (调用方随后报错)."""
    try:
        return parse(sym).yahoo
    except ValueError:
        return sym.strip().upper()


def _pnl_color(v) -> str:
    """涨跌单元格字体色: 涨/跌按全局配色 (0/缺失不着色)。"""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == 0:
        return ""
    up_c, down_c = S.up_down_colors()
    return f"color: {up_c}" if v > 0 else f"color: {down_c}"


def _symbol_pie(view: pd.DataFrame):
    """「按代码」资产配置饼图: 市值前 SYMBOL_TOP 名逐个展示, 其余合并为「其他」.

    代码维度动辄几十上百个, 全画进饼图不可读; 尾部合并后图例仍有 20+ 条,
    故占比过小的切片不标文字 (改看图例/hover), 图例字号也随条目数调小,
    避免 1/3 栏宽下把饼图本身挤没。
    """
    sym_view = by_symbol(view, top=SYMBOL_TOP)
    fig = px.pie(
        sym_view,
        names="symbol",
        values="market_value",
        title=(
            f"按代码 (Top {SYMBOL_TOP} + 其他)"
            if view["symbol"].nunique() > SYMBOL_TOP
            else "按代码"
        ),
    )
    total_mv = float(sym_view["market_value"].sum())
    if total_mv:
        fig.update_traces(
            text=[
                f"{v / total_mv:.1%}" if v / total_mv >= SYMBOL_LABEL_MIN_SHARE else ""
                for v in sym_view["market_value"]
            ],
            textinfo="text",
        )
    if len(sym_view) > 10:
        fig.update_layout(legend=dict(font=dict(size=10)))
    return fig


def render_portfolio_page() -> None:
    """组合页入口: 侧边栏编辑器 + 主区渲染 (行情一次批量拉取, 持仓/自选共用).

    首屏行情经 _load_quotes 走 stale-while-revalidate: 旧缓存秒渲染 + 后台
    刷新, 不再每次打开都干等网络; 冷启动 (无缓存) 才阻塞拉取。
    """
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

    with st.sidebar:
        st.markdown(
            ":material/candlestick_chart: **组合追踪**",
            help="多市场持仓 + 自选提醒 · 数据源按域路由: 全球 yfinance / A股 akshare / 加密 Binance; 可选长桥/IBKR 前置 (设置页)",
        )
        with st.expander(":material/edit_note: 持仓管理", expanded=True):
            st.caption(
                "代码规范: AAPL · SAP.DE · BP.L · RY.TO · BHP.AX · 0700.HK · 600519.SS · BTC-USD"
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
                    # 保留编辑器不展示的溯源键 (如钱包导入的 import_source), 否则页面
                    # 保存一次就丢掉, 后续多钱包导入会退化成同代码互相覆盖
                    extra = {
                        _norm_sym(h.get("symbol", "")): {
                            k: v
                            for k, v in h.items()
                            if k not in ("symbol", "quantity", "avg_cost", "type")
                        }
                        for h in portfolio.get("holdings", [])
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
                    storage.save_portfolio({"base_currency": base, "holdings": clean})
                    cached_quotes.clear()
                    cached_fx.clear()
                    if dups:
                        st.toast("持仓已保存 (重复行已移除)")
                    else:
                        st.toast("持仓已保存")
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
                    storage.save_watchlist({"watchlist": rows}, WATCHLIST_PATH)
                    cached_quotes.clear()
                    if dups:
                        st.toast("自选已保存 (重复行已移除)")
                    else:
                        st.toast("自选已保存")
                    # 本次 run 的 watch 是脚本开头从磁盘读的旧快照: 保存后不 rerun
                    # 的话, 下方自选表/查看范围仍用旧数据, 且 cached_quotes.clear()
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

    st.divider()
    st.caption(
        "数据源: 美股/港股/全球 Yahoo Finance; A股/北交所 akshare 优先 (港股 akshare 兜底); "
        "加密货币 Binance; 汇率 CFETS + yfinance。可选前置源: 长桥 / IBKR (设置页开启)。"
        "每个代码实际命中的源见下方「数据来源」列与页头汇总。"
        "非美股行情一般延迟 15-30 分钟, 仅供个人参考。"
    )

    holdings = edited.dropna(subset=["symbol"]) if not edited.empty else edited

    all_watch_entries = merge_entries(watch)
    if holdings.empty and not all_watch_entries:
        # 先清空 K线页「常用」pills 再停: 否则删掉最后一个持仓/自选后, 已打开的
        # K线页仍残留被删代码 (quick_symbols 未更新), 用户点 pill 会查已删除的标的
        set_quick_symbols([])
        st.info(
            "在左侧添加持仓或自选股并保存。支持 A股/港股/美股/德股/英股/加股/澳股/新加坡股/加密货币。"
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
    # 只拉该列表。cached_quotes 按 symbols 元组缓存, 5 分钟内切回原范围命中。
    scope = st.selectbox(
        "查看范围",
        ["全部"] + list_names(watch),
        key="watch_scope",
        help="行情拉取范围 = 持仓 + 所选列表的自选股 (K线页「常用」仍含全部代码)",
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
        fetch_symbols, prefer_akshare, use_ibkr, use_longport, crypto_source
    )
    if not quotes:
        st.error(
            "未能获取任何行情。请检查网络: Yahoo 需可访问 finance.yahoo.com; "
            "国内网络可勾选 akshare 优先。"
        )
        with st.expander("错误详情"):
            for k, v in errors.items():
                st.write(f"{k}: {v}")
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

    wview, wissues = build_watchlist_view(watch_entries, quotes)
    issues += wissues
    trig_all = triggered_entries(wview)

    # ---- 顶部: 页头 + KPI 卡片 (券商 App 风格: 关键数字前置) ----
    head_l, head_r = st.columns([3, 1])
    head_l.markdown("### 组合总览")
    head_r.caption(
        f":material/schedule: {datetime.now():%m-%d %H:%M}",
        help="免费数据源有延迟 (非美股 15-30 分钟), 仅供参考",
    )
    if notes:
        for n in notes:
            st.caption(f":material/info: {n}")
    src_counts: dict[str, int] = {}
    for q in quotes.values():
        src = getattr(q, "source", None)
        if src:
            src_counts[src] = src_counts.get(src, 0) + 1
    if src_counts:
        st.caption(
            "数据来源: "
            + " · ".join(f"{k} ×{v}" for k, v in sorted(src_counts.items()))
            + " (明细见持仓/自选表「数据来源」列)"
        )

    if not view.empty:
        cov = m.get("cost_coverage")
        pnl_delta = (
            f"{m['total_pnl_pct']:+.2%} (覆盖 {cov:.0%})"
            if m["total_pnl_pct"] is not None and cov is not None and cov < 1.0
            else f"{m['total_pnl_pct']:+.2%}"
            if m["total_pnl_pct"] is not None
            else None
        )
        with st.container(horizontal=True):
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
                f"{len(view)} / {len(wview)}",
                border=True,
            )

    # ---- 阈值告警前置 (最关键信息, 类似券商推送条) ----
    if not trig_all.empty:
        with st.container(border=True):
            top, more = st.columns([5, 1], vertical_alignment="center")
            scope_tag = "" if scope == "全部" else f" ({scope})"
            top.markdown(
                f":material/notifications_active: **{len(trig_all)} 只自选触及价格阈值**{scope_tag} · "
                + " · ".join(
                    f"**:{S.up_down_tags()[0]}[{r['symbol']}]** {r['status'].split(' ', 1)[1]}"
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
        with st.expander(
            f":material/warning: 数据问题 ({len(errors) + len(issues)})",
            icon=":material/warning:",
        ):
            for k, v in errors.items():
                st.write(f"- {k}: {v}")
            for i in issues:
                st.write(f"- {i}")

    tab1, tab4, tab2 = st.tabs(
        [
            ":material/table_chart: 持仓明细",
            f":material/visibility: 自选观察{' :red[🔔' + str(len(trig_all)) + ']' if len(trig_all) else ''}",
            ":material/donut_large: 资产配置",
        ]
    )
    with tab1:
        if view.empty:
            st.info("暂无持仓数据。")
        else:
            styled = view.style.map(
                _pnl_color, subset=["change_pct", "pnl", "pnl_pct", "today_pnl"]
            )
            st.dataframe(
                styled,
                width="stretch",
                height=min(120 + 35 * len(view), 560),
                hide_index=True,
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
            dl_l, dl_r = st.columns([1, 3])
            dl_l.download_button(
                "导出 CSV",
                view.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"portfolio_{datetime.now():%Y%m%d}.csv",
                mime="text/csv",
                icon=":material/download:",
            )

    with tab2:
        if view.empty:
            st.info("暂无持仓数据。")
        else:
            c1, c2, c3 = st.columns(3)
            with c1:
                st.plotly_chart(
                    px.pie(view, names="market", values="market_value", title="按市场"),
                    width="stretch",
                )
            with c2:
                st.plotly_chart(
                    px.pie(
                        view,
                        names="currency",
                        values="market_value",
                        title="按币种",
                    ),
                    width="stretch",
                )
            with c3:
                st.plotly_chart(_symbol_pie(view), width="stretch")
            st.plotly_chart(
                px.bar(
                    view.head(15),
                    x="symbol",
                    y="market_value",
                    color="market",
                    title=f"持仓市值 Top 15 ({base})",
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
            for i in wissues:
                st.caption(f":material/warning: {i}")
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
                st.dataframe(
                    styled_w,
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
