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

from datetime import date

from . import events_longport
from .symbols import parse
from .events_cn import (  # noqa: F401 - 兼容既有导入/测试
    _cn_code_to_yahoo,
    _cn_normalize_disclosure,
    _cn_normalize_fhps,
    _market_label_for_symbol,
    _pd_date,
    _pick_cn_scheduled_row,
)
from .events_cn_source import (  # noqa: F401 - 兼容再导出
    _cn_boards_for,
    _fetch_cn_cached,
    _fetch_cn_events,
    _fetch_cn_fhps_period,
    _fetch_cn_period,
    _fetch_cn_yysj_board,
    _quiet_stderr,
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
from .events_longport import (  # noqa: F401 - 兼容再导出
    _LP_CATEGORIES,
    _LP_MARKETS,
    _fetch_longport_events,
    _fetch_lp_month_events,
    _lp_config_builder,
    _lp_dividend_type,
    _lp_market_axis,
    _seg_rows,
    normalize_longport_event,
)
from .events_window import (  # noqa: F401 - 兼容再导出
    _age_exceeded,
    _age_of,
    _in_window,
    _month_starts,
    _quarter_ends,
    _to_date,
    _window_rows,
    cn_dividend_periods_for_window,
    cn_periods_for_window,
    window_from_months,
)

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
            return events_longport._fetch_longport_events(
                symset,
                start,
                end,
                refresh=refresh,
                stale_ok=stale_ok,
                cache_only=cache_only,
                stats=lp_stats,
                reporter=reporter,
            )
        return events_longport._fetch_longport_events(symset, start, end, refresh=refresh)

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
