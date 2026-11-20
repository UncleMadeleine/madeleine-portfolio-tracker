"""长桥日历兜底源: CalendarContext 整批 (US/HK/SH/SZ, 按月缓存)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from . import cache as cache_mod
from .events_common import (
    CAL_STALE_TTL,
    CAL_TTL,
    _ProgressReporter,
)
from .events_window import _age_of, _in_window, _month_starts
from .events_cn import _pd_date
from .symbols import parse

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


