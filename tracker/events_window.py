"""财经日历窗口与报表期工具 (纯函数).

从 events.py 拆出: 月份窗口、A股预约披露/分红报表期、日期窗口判定与
缓存年龄计算。不依赖网络/缓存, 供 events 与 CLI/UI 复用。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

_CN_DISCLOSURE_GRACE_DAYS = 30
# 候选报表期的回看范围: 年报截止日距期末最长 120 天 + 30 天宽限 = 150 天,
# 再留一个季度余量, 保证截止日刚好落在窗口开始附近的报表期不被漏掉。
_CN_PERIOD_LOOKBACK_DAYS = 160
# 分红/送转覆盖窗口: 股权登记/除权除息日可能远晚于报告期末 (年中派息、方案
# 延后实施), 按 15 个月回看; 与预约披露口径分开, 否则窗口内执行的老报告期
# 分红会被漏掉。
_CN_DIVIDEND_BACKDAYS = 450
# 单票 yfinance 请求启动间隔 (秒): 防连发 429; 多线程下由 _yf_rate_limit
# 串行化「启动时刻」, 不再依赖顺序循环里的 sleep。
# ---------------------------------------------------------------------------
# 窗口 / 报表期工具 (纯函数, 可测)
# ---------------------------------------------------------------------------


def window_from_months(months: int, today: date | None = None) -> tuple[date, date]:
    """月数窗口: 当前月 1 日 → 第 (months) 个自然月月末.

    「本月 + 下月」= months 2 (默认); months 1 = 仅本月, months 3 = 本月起三个月。
    """
    t = today or date.today()
    y, m = t.year, t.month
    end_m = m + months
    y2, m2 = y + (end_m - 1) // 12, (end_m - 1) % 12 + 1
    end = date(y2, m2, 1) - timedelta(days=1)
    return date(y, m, 1), end


def _quarter_ends(start: date, end: date) -> list[date]:
    """闭区间 [start, end] 内的所有报表期末日 (3/6/9/12 月最后一天), 升序.

    每命中一个季度末后跳到下一季首月, 同一季度末只生成一次。
    """
    out: list[date] = []
    d = date(start.year, start.month, 1)
    while d <= end:
        m = ((d.month - 1) // 3) * 3 + 3  # 本季末月
        qe = date(d.year, m, 1) + timedelta(days=32)
        qe = date(qe.year, qe.month, 1) - timedelta(days=1)
        if start <= qe <= end:
            out.append(qe)
        # 跳到下一季首月 (m+1 可跨年)
        y2, m2 = (d.year + 1, 1) if m == 12 else (d.year, m + 1)
        d = date(y2, m2, 1)
    return sorted(set(out))


def _cn_disclosure_deadline(period_end: date) -> date:
    """A 股定期报告的法定披露截止日 (按报表期末所在季度).

    Q1 → 4/30, H1 → 8/31, Q3 → 10/31, 年报 → 次年 4/30。用于判断一个
    报表期是否可能在本窗口内产生披露事件, 避免为了「可能」而整批请求全
    市场数据。
    """
    if period_end.month == 3:
        return date(period_end.year, 4, 30)
    if period_end.month == 6:
        return date(period_end.year, 8, 31)
    if period_end.month == 9:
        return date(period_end.year, 10, 31)
    return date(period_end.year + 1, 4, 30)


def cn_periods_for_window(start: date, end: date) -> list[str]:
    """窗口内可能披露财报的报表期 → 东财 date 参数 ("YYYYMMDD") 升序.

    披露日 ≥ 报表期末 (财报不可能早于报告期结束), 所以 qe >= 窗口结束的
    报表期不可能落入窗口; 截止日+宽限 < 窗口开始的报表期也早已披露完毕。
    只保留真正可能命中的报表期, 冷启动时少发多次全市场请求 (每期约 5k 行,
    沪深板块单次要拉 10+ 页)。
    """
    if end <= start:
        return []
    earliest = start - timedelta(days=_CN_PERIOD_LOOKBACK_DAYS)
    grace = timedelta(days=_CN_DISCLOSURE_GRACE_DAYS)
    out: list[str] = []
    for qe in _quarter_ends(earliest, end):
        if qe >= end:
            continue
        if _cn_disclosure_deadline(qe) + grace < start:
            continue
        out.append(qe.strftime("%Y%m%d"))
    return out


def cn_dividend_periods_for_window(start: date, end: date) -> list[str]:
    """窗口内可能实施的 A 股分红 → 需要请求的报告期 ("YYYYMMDD") 升序.

    分红/送转的股权登记日与除权除息日可以在报告期结束后很久才执行 (年中
    派息、方案延后实施), 因此不能沿用预约披露的截止日口径, 单独用
    _CN_DIVIDEND_BACKDAYS (15 个月) 覆盖老报告期的实施事件。不过实施日
    必然晚于报告期末, qe >= 窗口结束的报表期同样不可能命中。
    """
    if end <= start:
        return []
    earliest = start - timedelta(days=_CN_DIVIDEND_BACKDAYS)
    return [qe.strftime("%Y%m%d") for qe in _quarter_ends(earliest, end) if qe < end]


def _month_starts(start: date, end: date) -> list[tuple[date, date]]:
    """窗口切分为自然月 (首尾裁进窗口): longport 整批缓存的分片粒度."""
    out: list[tuple[date, date]] = []
    d = date(start.year, start.month, 1)
    while d <= end:
        nxt = date(d.year + int(d.month == 12), (d.month % 12) + 1, 1)
        seg_start = max(d, start)
        seg_end = min(nxt - timedelta(days=1), end)
        if seg_start <= seg_end:
            out.append((seg_start, seg_end))
        d = nxt
    return out


def _in_window(d: date | None, start: date, end: date) -> bool:
    return d is not None and start <= d <= end


def _age_exceeded(fetched_at, ttl: float) -> bool:
    """缓存行时间戳 + ttl 是否过期 (脏时间戳当过期处理)."""
    return _age_of(fetched_at) > ttl


def _age_of(fetched_at) -> float:
    """缓存行年龄 (秒); 脏/缺失时间戳视为无限旧 (必然回源)."""
    try:
        ts = float(fetched_at)
    except (TypeError, ValueError):
        return float("inf")
    if ts <= 0:
        return float("inf")
    return max(0.0, time.time() - ts)




def _to_date(v) -> date | None:
    """yfinance calendar 值 → date; 列表/日期/datetime/字符串统一处理."""
    if isinstance(v, datetime):
        if v != v:  # pandas NaT: NaT.date() 仍是 NaT, 不能让比较阶段抛 TypeError
            return None
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()[:10]
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None

def _window_rows(
    evs: list[dict], start: date, end: date, source: str | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """缓存载荷行 → 窗口内事件行 (date 统一 date 对象; source/symbol 补齐)."""
    picked = []
    for r in evs or []:
        if not isinstance(r, dict):
            continue
        # 旧版本/手改缓存可能混进缺 type 的行; 排序阶段 e["type"] 会 KeyError,
        # 让整张日历挂掉。脏行只跳过, 其余事件照常渲染。
        typ = r.get("type")
        if not isinstance(typ, str) or not typ.strip():
            continue
        d = _to_date(r.get("date"))
        if _in_window(d, start, end):
            r = dict(r)
            r["date"] = d
            if source and r.get("source") is None:
                r["source"] = source
            if symbol and not r.get("symbol"):
                r["symbol"] = symbol
            picked.append(r)
    return picked

