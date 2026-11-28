"""组合页首屏数据层: 行情/汇率缓存、SWR 刷新计划与看门狗.

从 portfolio_page.py 拆出, 页面渲染与侧边栏编辑器仍在 portfolio_page.py。
测试可通过 portfolio_page 的再导出继续访问这些函数。
"""

from __future__ import annotations

import time

import streamlit as st

from tracker import cache as cache_mod
from tracker import prices
from tracker.fx import get_fx_rates
from tracker.services import warm as warm_mod

from .portfolio_format import _local_quote_errors, _norm_sym
from .progress import FetchProgress

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
# 看门狗片段轮询间隔 (秒): 后台刷新完成后尽快 rerun 换新数据
_REFRESH_POLL = 2.0
# session_state 里的首屏刷新计划 (每次渲染重建, 看门狗据此调度)
_PLAN_KEY = "_first_screen_plan"
# 本会话最近一次写入历史净值的记录指纹: 用户每次交互都会 rerun, 数值没变时
# 不必反复 upsert 同一个 (day, base) 行 (每次都是磁盘写 + schema 检查)。
_NAV_SNAPSHOT_KEY = "_nav_snapshot_written"


# ---------- 周期保温: 服务长期运行时数据持续更新 ----------
# 页面开着 → 看门狗片段按周期主动重取 (用户看到的数据滚动更新);
# 页面全关 → 进程级保温线程照常刷磁盘缓存 (后来者打开即是新鲜数据)。
# 具体实现已抽到 tracker/services/warm.py (无 Streamlit 依赖, CLI
# `tracker warm` 复用同一套协调器); 这里保留页面常量别名与薄封装。
_REFRESH_MIN_INTERVAL = warm_mod.REFRESH_MIN_INTERVAL
_REFRESH_PERIOD = warm_mod.REFRESH_PERIOD
_REFRESH_PERIOD_PER_SYMBOL = warm_mod.REFRESH_PERIOD_PER_SYMBOL
_REFRESH_PERIOD_MAX = warm_mod.REFRESH_PERIOD_MAX
_REFRESH_TICK = warm_mod.REFRESH_TICK
_REFRESH_HISTORY = warm_mod.REFRESH_HISTORY


def _refresh_period(n_symbols: int) -> float:
    """保温周期 (秒): 基准 300s 起, 按符号集规模拉长, 上限 30min."""
    return warm_mod.refresh_period(n_symbols)


def _plan() -> dict:
    """当前会话的首屏刷新计划 (惰性创建; 原地更新, 字段跨渲染保留).

    - quotes/fx: 取数参数 (周期保温刷新要用)
    - dirty: 是否有待后台刷新的数据面
    - seen: 已消费的刷新完成时间戳 (只消费一次, 防热循环)
    - q_age/f_age: 行情/汇率磁盘缓存的最旧年龄 (秒), 周期保温据此判断
    - checked_at: q_age/f_age 的测量时刻; 看门狗片段没有完整 rerun 时,
      用「测量时的年龄 + 此后流逝的时间」估算当前年龄, 否则页面静置后
      年龄永远停在 0, 周期保温永远不会触发
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
            "checked_at": 0.0,
        },
    )


def _plan_reset() -> None:
    """每次渲染重建刷新计划 (行情/汇率两个槽位由各自 loader 填写).

    只清脏标记, 其余字段原地保留: seen 丢失会让一次完成事件被反复当成
    "新完成"触发 rerun (数据仍脏时形成热循环); quotes/fx 参数与年龄丢失
    则周期保温无依据。checked_at 在这里重置: 下方 loader 马上会重新测量
    q_age/f_age, 之后看门狗用「年龄 + 流逝时间」延续计时。
    """
    plan = _plan()
    plan["dirty"] = False
    plan["checked_at"] = time.time()


def _plan_key(plan: dict) -> tuple:
    """刷新任务键: 行情参数 + 汇率参数 (任一变化都是新任务)."""
    return warm_mod.refresh_key(plan)


def _refresh_state(key: tuple) -> tuple[bool, float | None]:
    return warm_mod.refresh_state(key)


def _start_refresh(key: tuple, plan: dict) -> bool:
    """启动一次后台刷新 (行情 + 汇率); 返回是否真正发起.

    实现与进程级保温线程共用 tracker.services.warm: 同 key 不并发、失败
    节流、线程内只跑取数链路 (不碰 st.* API)。
    """
    return warm_mod.start_refresh(key, plan)



def _fetch_quotes_with_progress(
    fetchable: tuple[str, ...],
    prefer_akshare: bool,
    use_ibkr: bool,
    use_longport: bool,
    crypto_source: str,
) -> tuple[dict, dict, list]:
    """冷启动阻塞取行情 (无任何可展示缓存) + 实时进度条.

    只有真正要发网络时才走到这里: 缓存陈旧/部分缺失走后台 SWR, 不会卡住
    首屏。进度按已结算代码数推进 (批量返回、逐代码成功/失败都计入), 避免
    大组合 (数百自选) 冷启动时页面只转一个无信息量的 spinner。
    """
    with FetchProgress(
        "拉取行情", "拉取行情 (首次加载需初始化数据引擎)..."
    ) as progress:
        return prices.get_quotes(
            list(fetchable),
            prefer_akshare=prefer_akshare,
            use_ibkr=use_ibkr,
            use_longport=use_longport,
            crypto_source=crypto_source,
            progress=progress,
        )


def _load_quotes_cached(
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
        try:
            out = _fetch_quotes_with_progress(
                fetchable, prefer_akshare, use_ibkr, use_longport, crypto_source
            )
        except Exception as e:  # noqa: BLE001 - 取数链路异常不能穿透成整页 traceback
            # 网络超时/HTTP 失败多在 providers 内部被吞掉返回 errors (走
            # render_portfolio_page 的优雅降级分支), 但取数链路自身抛异常
            # (数据源冷启动失败 / sqlite locked / provider 未预期异常) 会
            # 直接穿到 Streamlit 显示 traceback。这里接住: 落空结果 +
            # 可读错误, 保持 dirty + age=inf 让看门狗按最短间隔自动补刷。
            plan["q_age"] = float("inf")
            plan["dirty"] = True
            return {}, {**errors, "行情": f"行情获取异常: {e}"}, []
        plan["q_age"] = 0.0  # 刚同步取完数, 不必立刻保温
        # 本地 errors (无法识别的代码) 必须并回: 这些代码已被排除出
        # fetchable, get_quotes 的 errors 不可能包含它们, 直接 return out
        # 会让用户看不到「代码无法识别」
        return out[0], {**errors, **out[1]}, out[2]
    # 陈旧数据秒渲染 + 挂后台刷新
    plan["dirty"] = True
    return hits, errors, []


def _load_quotes(
    fetch_symbols: tuple[str, ...],
    prefer_akshare: bool,
    use_ibkr: bool,
    use_longport: bool,
    crypto_source: str,
    refresh_symbols: tuple[str, ...] = (),
) -> tuple[dict, dict, list]:
    """首屏行情加载 + 可选「只刷新选中代码」.

    常规路径全部走 _load_quotes_cached (缓存优先/SWR); refresh_symbols 非空时
    (用户在主表选中 1-2 只后点「刷新选中」), 只对这些代码绕过缓存与负缓存
    回源, 再把结果合并进本次快照 —— 不会像全量强制刷新那样把当前范围内所有
    代码都重新打一遍数据源。
    """
    quotes, errors, notes = _load_quotes_cached(
        fetch_symbols, prefer_akshare, use_ibkr, use_longport, crypto_source
    )
    targets = tuple(
        dict.fromkeys(
            _norm_sym(str(s)) for s in refresh_symbols if str(s).strip()
        )
    )
    if not targets:
        return quotes, errors, notes
    try:
        with st.spinner(f"刷新 {'、'.join(targets)} …"):
            fresh, fresh_errors, fresh_notes = prices.get_quotes(
                list(targets),
                prefer_akshare=prefer_akshare,
                use_ibkr=use_ibkr,
                use_longport=use_longport,
                crypto_source=crypto_source,
                refresh=True,
            )
    except Exception as e:  # noqa: BLE001 - 单次刷新失败不能穿透成整页 traceback
        errors = {**errors, "刷新": f"刷新失败: {e}"}
        return quotes, errors, notes
    quotes.update(fresh)
    # 选中代码的旧错误作废 (可能刚从负缓存/超时中恢复), 以本轮结果为准
    target_set = set(targets)
    errors = {
        k: v for k, v in errors.items() if _norm_sym(str(k)) not in target_set
    }
    errors.update(fresh_errors)
    return quotes, errors, [*notes, *fresh_notes]


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
        bar = st.progress(0.0, text="拉取汇率...")
        try:
            bar.progress(0.4, text="拉取汇率 (CFETS/Yahoo)...")
            out = cached_fx(base, currencies, use_ibkr)
            bar.progress(1.0, text="汇率已获取")
        except Exception as e:  # noqa: BLE001 - 同 _load_quotes: 异常不穿透成 traceback
            plan["f_age"] = float("inf")
            plan["dirty"] = True
            return {base: 1.0}, [f"汇率获取异常: {e}"]
        finally:
            bar.empty()
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
        if not _plan_needs_periodic_refresh(plan):
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
    """计划覆盖数据的磁盘年龄 (秒): 取行情/汇率两者最旧; 缺数据视为无限旧.

    看门狗片段每 2s 只重跑片段而不是整页, 不会重新读盘测年龄。这里在最近
    一次测量值上叠加流逝时间, 页面静置时年龄才会继续增长到保温周期。
    """
    age = max(plan.get("q_age") or 0.0, plan.get("f_age") or 0.0)
    if age == float("inf"):
        return age
    checked_at = plan.get("checked_at") or 0.0
    if checked_at:
        age += max(0.0, time.time() - checked_at)
    return age


def _plan_needs_periodic_refresh(plan: dict) -> bool:
    """周期保温是否到点 (无持仓/自选、数据仍年轻都返回 False)."""
    if not (plan.get("quotes") or plan.get("fx")):
        return False
    return _plan_data_age(plan) >= _refresh_period(_plan_symbol_count(plan))


def _clear_quote_caches(*, fx: bool = True) -> None:
    """清行情缓存; 汇率只在持仓/自选结构影响本币换算时一起清."""
    cached_quotes.clear()
    if fx:
        cached_fx.clear()


