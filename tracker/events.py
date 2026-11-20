"""财经日历域: 财报 / 重大事件 (预约披露 / 除权除息) 的批量获取与磁盘缓存.

数据链与 provider 层同构, 严格分域; 长桥只作兜底 (可选依赖, 需 use_longport):

  global (美股/港股/日韩/欧洲等): yf.Ticker().calendar 单票循环主源 —— 未来
    财报日期 (Yahoo 为估计口径) + 除息日 / 派息日; 单票拿不到或 yfinance
    限流时, use_longport 开启则兜底走长桥 CalendarContext 整批日历 (覆盖
    US/HK, 每市场每类别按月整批请求); SG/DE/JP/等长桥不支持的市场无兜底,
    注记进 issues。
  cn (A股/B股/北交所): akshare 整批主源 ——
    stock_yysj_em (东财预约披露时间, 沪深A/科创/创业/京市全板, 含首次预约/
    一次/二次/三次变更/实际披露列链) + stock_fhps_em (分红进度, 含预案公告
    日/股权登记日/除权除息日); 每报表期 2 个请求拿全市场, 按代码过滤。
    akshare 整批失败时同样回落长桥 (SH/SZ)。
  crypto / index 不在日历域 (调用方先用 parse().type 过滤)。

事件统一为 dict 行:
  {"symbol", "name"?, "market_label", "type": earnings|ex_dividend|
   record_date|dividend, "date": "YYYY-MM-DD", "date_status": planned|changed|
   actual|estimate, "note"?, "source": yfinance|akshare|longport}

缓存: tracker.cache 表 calendar_cache (key, payload, fetched_at)。
  - 单票 global: key="g:<symbol>", 有事件 TTL 6h (对齐 fundamentals, 低频
    报表数据); 空载荷 (Yahoo 无该标的财报数据, 小市值票居多) TTL 24h 负缓
    存, 避免大量空票每 6h 全量重打一遍 Yahoo。
  - 整批 cn: key="cn:yysj:<period>:<board>" (预约披露, 分板块独立缓存, 单板
    失败不污染另一板) / "cn:fhps:<period>" (分红送转), 全市场行整存, 读取时
    按代码+窗口过滤 (新增自选 6h 内不可见, refresh 取新)。
  - 整批 longport: key="lp:<market>:<category>:<YYYY-MM>", 永远按整月请求/
    缓存, 读取时再裁到窗口分片, 避免月中窗口把半个月结果缓存成整月。

首屏性能: fetch_calendar(stale_ok=True, cache_only=True) 只读缓存并允许
CAL_STALE_TTL (7 天) 内的旧行, 页面据此秒开 + 后台重取; cache_only 且无
任何可用缓存行时返回 None, 调用方改走带 progress 回调的阻塞取数。

取数性能: cn 各板块/报表期与 global 各票分别用线程池并发; 预约披露只
请求窗口真正可能命中的报表期, 并按范围跳过无关板块, 避免为每个报表期
重复拉全市场数据。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from . import cache as cache_mod
from .deps import akshare as _akshare
from .symbols import parse
from .events_cn import (
    _cn_code_to_yahoo,  # noqa: F401 - 兼容既有导入/测试
    _cn_normalize_disclosure,
    _cn_normalize_fhps,
    _market_label_for_symbol,
    _pd_date,
    _pick_cn_scheduled_row,  # noqa: F401 - 兼容既有导入/测试
)
from .events_common import (  # noqa: F401 - 兼容再导出
    CAL_NEG_TTL,
    CAL_STALE_TTL,
    CAL_TTL,
    DATE_STATUS_LABELS,
    EVENT_TYPE_LABELS,
    _ProgressReporter,
    _is_valid_eps,
    _merge_cache_meta,
)
from .events_global import (  # noqa: F401 - 兼容再导出
    _fetch_global_events,
    _fetch_global_one,
    _yf_calendar_rows,
    _yf_rate_limit,
)
from .events_window import (  # noqa: F401 - 兼容再导出
    _age_exceeded,
    _age_of,
    _to_date,
    _window_rows,
    _in_window,
    _month_starts,
    _quarter_ends,
    cn_dividend_periods_for_window,
    cn_periods_for_window,
    window_from_months,
)
from .util import with_timeout

__all__ = [
    "EVENT_TYPE_LABELS",
    "DATE_STATUS_LABELS",
    "CAL_TTL",
    "CAL_NEG_TTL",
    "CAL_STALE_TTL",
    "window_from_months",
    "cn_periods_for_window",
    "cn_dividend_periods_for_window",
    "normalize_longport_event",
    "fetch_calendar",
]

# 预约披露: 报表期 qe 的法定披露截止日 = {Q1: 4/30, Q2: 8/31, Q3: 10/31,
# 年报: 次年 4/30}; 实际/变更披露可能略晚于截止日, 留 30 天宽限。只请求
# 「截止日+宽限 >= 窗口开始」且 qe < 窗口结束 的报表期 —— 否则整批全市场
# 请求纯属浪费 (例如 11-12 月窗口只需 9/30 一期, 而不是 6/30、9/30、12/31)。
# 单票 yfinance 并发数: 自选几百只时, 顺序请求会把冷启动拖到十分钟级。
# 24 路实测 40 只港股约 6s (顺序约 1 分钟); 每请求仍有 _YF_TIMEOUT 硬超时,
# 单票失败只丢自己。启动频率另由 _YF_PAUSE 限速, 避免瞬间打满 Yahoo。
# A股整批请求并发数: 沪深/京市/分红是不同接口, 彼此独立; 并发后冷启动从
# 「板块×报表期」的累加耗时降到最慢一路的耗时。
_CN_MAX_WORKERS = 6
# yfinance 跨线程启动限速状态 (只在 _YF_MAX_WORKERS 个 worker 内使用)。
# 单次网络请求硬超时 (秒): 日历是可选附加信息, 不能让一个 TCP 半开的
# yfinance/akshare 请求把整页/整条 CLI 拖到分钟级。
_AK_TIMEOUT = 15.0








# ---------------------------------------------------------------------------
# global 域: yfinance 单票 calendar
# ---------------------------------------------------------------------------






_UNSET = object()










# ---------------------------------------------------------------------------
# cn 域: akshare 整批 (预约披露 + 分红)
# ---------------------------------------------------------------------------
















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


# ---------------------------------------------------------------------------
# longport 兜底: CalendarContext 整批 (US/HK/SH/SZ, 每市场每类别按月缓存)
# ---------------------------------------------------------------------------

_LP_MARKETS = ("US", "HK", "SH", "SZ")
_LP_CATEGORIES = ("Report", "Dividend")


def _lp_dividend_type(date_type: str) -> str:
    """长桥分红日标签 → 内部类型; 无法判断时按除权除息处理.

    长桥 Dividend 类别会同时返回除净/派息/登记等日期, 统一成 ex_dividend 会
    把派息日、股权登记日误标成除息日; 这里按 date_type 标签做保守映射。
    """
    s = (date_type or "").lower()
    if any(k in s for k in ("派息", "支付", "发放", "dividend payment", "pay")):
        return "dividend"
    if any(k in s for k in ("登记", "record")):
        return "record_date"
    return "ex_dividend"


def normalize_longport_event(info, category: str) -> dict | None:
    """SDK CalendarEventInfo → 内部事件行; 无法映射代码返回 None."""
    from .longport import longport_to_yahoo

    sym = str(getattr(info, "symbol", "") or "").strip()
    yahoo = longport_to_yahoo(sym) if sym else None
    if not yahoo:
        return None
    d = None
    raw_date = str(getattr(info, "date", "") or "").strip()
    for fmt in ("%Y.%m.%d", "%Y-%m-%d"):
        try:
            d = datetime.strptime(raw_date, fmt).date()
            break
        except ValueError:
            continue
    if d is None:
        return None
    try:
        p = parse(yahoo)
        market_label = p.market_label
    except ValueError:
        market_label = yahoo
    date_type = str(getattr(info, "date_type", "") or "").strip()
    if category == "Report":
        typ = "earnings"
    elif category == "Dividend":
        typ = _lp_dividend_type(date_type)
    else:
        typ = "other"
    bits = []
    content = str(getattr(info, "content", "") or "").strip()
    if content:
        bits.append(content)
    if date_type and date_type not in content:
        bits.append(date_type)
    kv = getattr(info, "data_kv", None) or []
    for item in kv or []:
        k = str(getattr(item, "key", "") or "").strip()
        v = str(getattr(item, "value", "") or "").strip()
        if v:
            bits.append(f"{k}={v}" if k else v)
    star = getattr(info, "star", None)
    if isinstance(star, int) and star > 0:
        bits.append("★" * star)
    note = "; ".join(bits) or None
    ev = {
        "symbol": yahoo,
        "name": str(getattr(info, "counter_name", "") or "").strip() or None,
        "type": typ,
        "date": d.isoformat(),
        "date_status": "estimate" if category == "Report" else "planned",
        "note": note,
        "source": "longport",
        "market_label": market_label,
        "date_type": date_type or None,
    }
    return ev


def _lp_market_axis(yahoo: str) -> str | None:
    """内部代码 → longport 日历 market 过滤值 (US/HK/SH/SZ; 其它 None)."""
    s = yahoo.upper()
    if "." not in s:
        return "US"
    suffix = s.rpartition(".")[2]
    if suffix == "HK":
        return "HK"
    if suffix == "SS":
        return "SH"
    if suffix == "SZ":
        return "SZ"
    return None


def _seg_rows(evs: list[dict], start: date, end: date) -> list[dict]:
    """按日期裁到 [start, end] (长桥缓存按整月存, 读取时按窗口分片裁)."""
    return [ev for ev in evs if _in_window(_pd_date(ev.get("date")), start, end)]


def _fetch_lp_month_events(
    market: str,
    category: str,
    seg_start: date,
    seg_end: date,
    cfg: dict | None = None,
    refresh: bool = False,
    stale_ok: bool = False,
    cache_only: bool = False,
    stats: dict | None = None,
    reporter: _ProgressReporter | None = None,
) -> tuple[list[dict], str | None]:
    """一个 (market, category, 月) 的整批日历 (缓存优先, TTL 6h) → events, error.

    永远按**整月**请求并缓存 (key 只到 YYYY-MM), 返回时再裁到 [seg_start,
    seg_end]。否则月中窗口会先把半个月的结果缓存成整月, 后续完整月窗口
    复用时会缺前半月的日期。

    stats/reporter 为可选副作用 (公开返回结构保持二元组): 记录 stale/missing
    供首屏 SWR 判定, reporter 供进度条按请求推进。
    """
    month_start = seg_start.replace(day=1)
    nxt = date(
        month_start.year + int(month_start.month == 12),
        (month_start.month % 12) + 1,
        1,
    )
    month_end = nxt - timedelta(days=1)
    key = f"lp:{market}:{category}:{month_start.strftime('%Y-%m')}"
    row = None if refresh else cache_mod.get_calendar_row(key)
    if row is not None:
        age = _age_of(row["fetched_at"])
        if age <= CAL_TTL or (stale_ok and age <= CAL_STALE_TTL):
            if stats is not None:
                stats["used"] = True
                if age > CAL_TTL:
                    stats["stale"] = True
                    stats["stale_age"] = max(
                        float(stats.get("stale_age") or 0.0), age
                    )
            if reporter is not None:
                reporter.step(f"{market}/{category} {month_start:%Y-%m}")
            return _seg_rows(row["events"], seg_start, seg_end), None
    if cache_only:
        if stats is not None:
            stats["missing"] = True
        if reporter is not None:
            reporter.step(f"{market}/{category} {month_start:%Y-%m}")
        return [], None
    try:
        from longport.openapi import CalendarCategory

        api = _lp_config_builder(cfg)
    except Exception as e:  # noqa: BLE001 - 统一转错误信息
        if reporter is not None:
            reporter.step(f"{market}/{category} {month_start:%Y-%m}")
        return [], f"长桥日历不可用: {type(e).__name__}: {e}"[:200]
    evs: list[dict] = []
    cursor = month_start.isoformat()
    end_iso = month_end.isoformat()
    try:
        while cursor <= end_iso:
            cat = getattr(CalendarCategory, category)
            resp = api.finance_calendar(cat, cursor, end_iso, market)
            for group in getattr(resp, "list", []) or []:
                for info in getattr(group, "infos", []) or []:
                    ev = normalize_longport_event(info, category)
                    if ev is not None and (
                        month_start
                        <= (date.fromisoformat(ev["date"]))
                        <= month_end
                    ):
                        evs.append(ev)
            prev = cursor
            cursor = str(getattr(resp, "next_date", "") or "").strip()
            # next_date 不前进时立即收尾: 服务端异常返回同一游标会死循环
            if not cursor or cursor > end_iso or cursor <= prev:
                break
    except Exception as e:  # noqa: BLE001 - 带日期的错误兜底
        if reporter is not None:
            reporter.step(f"{market}/{category} {month_start:%Y-%m}")
        return _seg_rows(evs, seg_start, seg_end), (
            f"长桥日历({market}/{category})失败: {type(e).__name__}: {e}"[:200]
        )
    cache_mod.set_calendar_row(key, evs)
    if reporter is not None:
        reporter.step(f"{market}/{category} {month_start:%Y-%m}")
    return _seg_rows(evs, seg_start, seg_end), None


def _lp_config_builder(cfg: dict | None):
    """构造 LongPort CalendarContext (借用 tracker.longport 的 config 凭据链)."""
    from .longport import build_config

    from longport.openapi import CalendarContext

    return CalendarContext(build_config(cfg))


def _fetch_longport_events(
    symbols: set[str],
    start: date,
    end: date,
    cfg: dict | None = None,
    refresh: bool = False,
    stale_ok: bool = False,
    cache_only: bool = False,
    stats: dict | None = None,
    reporter: _ProgressReporter | None = None,
) -> tuple[dict[str, list[dict]], str | None]:
    """长桥整批兜底: 只拉长桥支持且自选中存在的市场 → {symbol: 窗口事件}."""
    markets = sorted({_lp_market_axis(s) for s in symbols} - {None})
    if not markets:
        return {}, None
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    err = None
    months = _month_starts(start, end)
    if reporter is not None:
        reporter.plan(len(markets) * len(_LP_CATEGORIES) * len(months))
    for market in markets:
        for category in _LP_CATEGORIES:
            for seg_start, seg_end in months:
                evs, e = _fetch_lp_month_events(
                    market,
                    category,
                    seg_start,
                    seg_end,
                    cfg=cfg,
                    refresh=refresh,
                    stale_ok=stale_ok,
                    cache_only=cache_only,
                    stats=stats,
                    reporter=reporter,
                )
                if e and err is None:
                    err = e
                if not evs:
                    continue
                syms_of_market = {
                    s for s in symbols if _lp_market_axis(s) == market
                }
                for ev in evs:
                    if ev["symbol"] in syms_of_market and _in_window(
                        _pd_date(ev["date"]), start, end
                    ):
                        ev = {**ev, "date": _pd_date(ev["date"])}
                        out[ev["symbol"]].append(ev)
    return out, err


# ---------------------------------------------------------------------------
# 顶层面板: fetch_calendar
# ---------------------------------------------------------------------------


def fetch_calendar(
    symbols,
    *,
    months: int = 2,
    start: date | None = None,
    end: date | None = None,
    refresh: bool = False,
    use_longport: bool = False,
    names: dict[str, str] | None = None,
    progress=None,
    stale_ok: bool = False,
    cache_only: bool = False,
) -> dict | None:
    """组合/自选的财经日历 → {"window", "events", "issues", "sources", "names"}.

    symbols: 内部规范代码列表 (crypto/index 由调用方过滤, 这里再保一层静默跳过)。
    names: 调用方可带 {symbol: 展示名} (watchlist 名/组合备注), 事件行缺名称
    时按它补齐 —— yfinance 单票路径不回名称, 组合/自选名称由这层接上。
    start/end: 显式窗口; 只给一侧时以给定日期为锚用 months 推另一侧, 两侧
    都缺省才用「本月起 months 个月」。start 晚于 end 视为调用错误。
    events 按 (date, symbol) 升序; issues 为主源失败/兜底结果的可读注记。
    progress(done, total, label) 供 CLI/UI 打进度 (label 为代码或阶段文本)。

    stale_ok: 过期但未超过 CAL_STALE_TTL 的缓存行先返回, 由调用方决定是否
    后台重取 (页面首屏 SWR)。
    cache_only: 只读缓存, 绝不发网络。无任何可用缓存行时返回 None, 调用方
    改走阻塞取数; 部分缺失/陈旧时结果 cache 字段标记 partial/stale。
    """
    symbols = list(dict.fromkeys(str(s).strip().upper() for s in symbols if s))
    if start is None and end is None:
        start, end = window_from_months(max(1, months))
    elif start is None or end is None:
        anchor = start or end
        s0, e0 = window_from_months(max(1, months), today=anchor)
        start = start or s0
        end = end or e0
    if start > end:
        raise ValueError(f"start ({start}) 不能晚于 end ({end})")

    global_syms: list[str] = []
    cn_syms: set[str] = set()
    for s in symbols:
        try:
            p = parse(s)
        except ValueError:
            continue
        if p.type in ("crypto", "index"):
            continue
        elif p.type == "cn":
            cn_syms.add(p.yahoo)
        else:
            global_syms.append(p.yahoo)

    events: list[dict] = []
    issues: list[str] = []
    names: dict[str, str] = dict(names or {})  # 调用方映射为基底, 域内名称补齐
    meta = {"stale": False, "missing": False, "stale_age": 0.0, "used": False}
    reporter = _ProgressReporter(progress) if progress is not None else None

    def _lp_fetch(symset: set[str], lp_stats: dict):
        """长桥兜底取数.

        正常路径 (无 stale/cache_only/progress) 保持旧调用签名, 兼容既有
        monkeypatch; 需要缓存元数据/进度时才追加关键字。
        """
        if stale_ok or cache_only or reporter is not None:
            return _fetch_longport_events(
                symset,
                start,
                end,
                refresh=refresh,
                stale_ok=stale_ok,
                cache_only=cache_only,
                stats=lp_stats,
                reporter=reporter,
            )
        return _fetch_longport_events(symset, start, end, refresh=refresh)

    # ---- cn 域 ----
    if cn_syms:
        cn_events, cn_issues, cn_names, cn_meta = _fetch_cn_events(
            cn_syms,
            start,
            end,
            refresh,
            stale_ok=stale_ok,
            cache_only=cache_only,
            reporter=reporter,
        )
        events.extend(cn_events)
        issues.extend(cn_issues)
        names.update(cn_names)
        _merge_cache_meta(meta, cn_meta)
        # 开兜底时对 akshare 没有事件的 SH/SZ 代码逐只补长桥 (不再要求整批
        # 失败: 混合持仓下缺票同样应该兜底)
        got = {e["symbol"] for e in cn_events}
        missing_cn = cn_syms - got
        if missing_cn and use_longport:
            lp_stats = {"stale": False, "missing": False, "stale_age": 0.0}
            try:
                lp_out, lp_err = _lp_fetch(missing_cn, lp_stats)
            except Exception as e:  # noqa: BLE001 - 兜底失败不拖主流程
                lp_out, lp_err = {}, f"长桥日历不可用: {e}"[:200]
            for _sym, evs2 in lp_out.items():
                events.extend(evs2)
            if lp_err:
                issues.append(lp_err)
            _merge_cache_meta(meta, lp_stats)

    # ---- global 域 ----
    if global_syms:
        global_out, g_issues, g_stats = _fetch_global_events(
            global_syms,
            start,
            end,
            refresh,
            reporter=reporter,
            stale_ok=stale_ok,
            cache_only=cache_only,
        )
        for _sym, evs2 in global_out.items():
            events.extend(evs2)
        issues.extend(g_issues)
        if g_stats["stale"]:
            meta["stale"] = True
            meta["stale_age"] = max(meta["stale_age"], g_stats["stale_age"])
        if g_stats["missing"]:
            meta["missing"] = True
        if g_stats["cached"]:
            meta["used"] = True
        # use_longport 兜底: 主链完全无事件的 US/HK 代码补长桥日历
        if use_longport:
            empty_syms = {s for s, evs2 in global_out.items() if not evs2}
            lpable = {
                s
                for s in empty_syms
                if _lp_market_axis(s) in ("US", "HK")
            }
            if lpable:
                lp_stats = {"stale": False, "missing": False, "stale_age": 0.0}
                try:
                    lp_out, lp_err = _lp_fetch(lpable, lp_stats)
                except Exception as e:  # noqa: BLE001
                    lp_out, lp_err = {}, f"长桥日历不可用: {e}"[:200]
                for _sym, evs3 in lp_out.items():
                    if evs3:
                        events.extend(evs3)
                if lp_err:
                    issues.append(lp_err)
                _merge_cache_meta(meta, lp_stats)

    if reporter is not None:
        reporter.finish()

    names = {k: v for k, v in (names or {}).items() if k and v}
    for ev in events:
        sym = ev.get("symbol")
        if sym not in names and ev.get("name"):
            names[sym] = ev["name"]
        if not ev.get("name") and sym in names:
            ev["name"] = names[sym]
        # 统一展示字段
        if ev.get("source") is None:
            ev["source"] = "yfinance"
        if not ev.get("market_label"):
            ev["market_label"] = _market_label_for_symbol(ev["symbol"])
        # date 统一 ISO 字符串 (json 兼容; date 对象由 _to_date 判定时已过滤)
        if isinstance(ev.get("date"), date):
            ev["date"] = ev["date"].isoformat()
    events = [e for e in events if e.get("date") is not None]
    # 来源计数按最终事件条数统一聚合 (不再按域分别统计, 避免各分支口径不一)
    sources: dict[str, int] = {}
    for ev in events:
        src = ev.get("source") or "yfinance"
        sources[src] = sources.get(src, 0) + 1
    events.sort(key=lambda e: (e["date"], e.get("symbol") or "", e.get("type") or ""))
    if cache_only and not meta["used"]:
        # 完全没有可渲染的缓存: 首屏只能阻塞取数, 由调用方处理
        return None
    return {
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "months": max(1, months),
        },
        "events": events,
        "issues": issues,
        "sources": sources,
        "names": names,
        "cache": {
            "used": bool(meta["used"]),
            "stale": bool(meta["stale"]),
            "stale_age": round(float(meta["stale_age"] or 0.0), 1),
            "partial": bool(meta["missing"]),
        },
    }
