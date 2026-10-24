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
  {"symbol", "name"?, "market_label", "type": earnings|forecast|ex_dividend|
   record_date|dividend, "date": "YYYY-MM-DD", "date_status": planned|changed|
   actual|estimate, "note"?, "source": yfinance|akshare|longport}

缓存: tracker.cache 表 calendar_cache (key, payload, fetched_at)。
  - 单票 global: key="g:<symbol>", 有事件 TTL 6h (对齐 fundamentals, 低频
    报表数据); 空载荷 (Yahoo 无该标的财报数据, 小市值票居多) TTL 24h 负缓
    存, 避免大量空票每 6h 全量重打一遍 Yahoo。
  - 整批 cn/longport: key="cn:<period>" / "lp:<market>:<category>:<YYYY-MM>",
    全市场行整存, 读取时按代码+窗口过滤 (新增自选 6h 内不可见, refresh 取新)。
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from . import cache as cache_mod
from .symbols import parse

__all__ = [
    "EVENT_TYPE_LABELS",
    "DATE_STATUS_LABELS",
    "CAL_TTL",
    "CAL_NEG_TTL",
    "window_from_months",
    "cn_periods_for_window",
    "normalize_longport_event",
    "fetch_calendar",
]

# 单票缓存 TTL (秒): 有事件 6h (与 fundamentals 一致的低频报表口径)
CAL_TTL = 21600
# 单票空结果负缓存 TTL: Yahoo 无数据的标的 24h 内不重试
CAL_NEG_TTL = 86400
# 预约披露覆盖窗口: 报表期结束到实际/预约披露最长间隔约 4 个月 (年报),
# 报表期在窗口开始前 130 天内的仍可能披露落在窗口内, 一并拉取
_CN_PERIOD_BACKDAYS = 130
# 单票 yfinance 取数间隔 (秒): 防连发 429; 与 tahlocks 无关的简单限时
_YF_PAUSE = 0.2

# 事件类型 → 展示名 (CLI/UI 共用)
EVENT_TYPE_LABELS = {
    "earnings": "财报",
    "forecast": "业绩预告",
    "ex_dividend": "除息/除权",
    "record_date": "股权登记",
    "dividend": "派息日",
}
DATE_STATUS_LABELS = {
    "planned": "预约",
    "changed": "已变更",
    "actual": "已披露",
    "estimate": "估计",
}


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


def cn_periods_for_window(start: date, end: date) -> list[str]:
    """窗口内可能披露财报的报表期 → 东财 date 参数 ("YYYYMMDD") 升序.

    披露日 ≥ 报表期末 (财报不可能早于报告期结束), 更晚的报表期披露必然
    超出窗口, 无需请求。回看 _CN_PERIOD_BACKDAYS 覆盖年报 (~4 个月披露)。
    """
    earliest = start - timedelta(days=_CN_PERIOD_BACKDAYS)
    return [qe.strftime("%Y%m%d") for qe in _quarter_ends(earliest, end)]


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
    import time as _time

    try:
        return float(fetched_at) <= 0 or (_time.time() - float(fetched_at)) > ttl
    except (TypeError, ValueError):
        return True


def _is_valid_eps(v: float) -> bool:
    from math import isfinite

    return isfinite(v) and v > 0


# ---------------------------------------------------------------------------
# global 域: yfinance 单票 calendar
# ---------------------------------------------------------------------------


def _to_date(v) -> date | None:
    """yfinance calendar 值 → date; 列表/日期/datetime/字符串统一处理."""
    if isinstance(v, datetime):
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


def _yf_calendar_rows(symbol: str) -> list[dict]:
    """yf.Ticker(symbol).calendar → 原始事件行 (不含窗口过滤)."""
    import yfinance as yf

    cal = yf.Ticker(symbol).calendar
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


def _fetch_global_one(symbol: str, refresh: bool) -> list[dict]:
    """单票 global 事件 (磁盘缓存优先); 空结果同样落库 (24h 负缓存语义)."""
    row = cache_mod.get_calendar_row(f"g:{symbol}")
    if not refresh and row is not None:
        evs, ttl = row["events"], (CAL_TTL if row["events"] else CAL_NEG_TTL)
        if not _age_exceeded(row["fetched_at"], ttl):
            return evs
    evs = _yf_calendar_rows(symbol)
    cache_mod.set_calendar_row(f"g:{symbol}", evs)
    return evs


def _fetch_global_events(
    symbols: list[str], start: date, end: date, refresh: bool, progress=None
) -> tuple[dict[str, list[dict]], list[str], dict]:
    """yfinance 单票循环 → {symbol: 窗口过滤后事件}, issues, 执行统计.

    命中缓存的代码零请求零间隔; 未命中的逐只发出并隔 _YF_PAUSE (防连发
    429), 单票失败注记进 issues 不拖丢整批。
    """
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    issues: list[str] = []
    stats = {"fresh": 0, "cached": 0, "fail": 0}
    todo: list[str] = []
    for s in symbols:
        if not refresh:
            row = cache_mod.get_calendar_row(f"g:{s}")
            if row is not None:
                evs, ttl = row["events"], (CAL_TTL if row["events"] else CAL_NEG_TTL)
                if not _age_exceeded(row["fetched_at"], ttl):
                    out[s] = _window_rows(evs, start, end, source="yfinance", symbol=s)
                    stats["cached"] += 1
                    continue
        todo.append(s)
    for i, s in enumerate(todo):
        if progress:
            progress(i + 1, len(todo), s)
        try:
            evs = _fetch_global_one(s, refresh)
        except Exception as e:  # noqa: BLE001 - 单票失败不拖丢整批
            stats["fail"] += 1
            issues.append(f"{s}: yfinance 日历失败 ({type(e).__name__}: {e})"[:160])
            continue
        if i < len(todo) - 1:
            import time as _t

            _t.sleep(_YF_PAUSE)
        out[s] = _window_rows(evs, start, end, source="yfinance", symbol=s)
        stats["fresh"] += 1
    if progress and todo:
        progress(len(todo), len(todo), None)
    return out, issues, stats


def _window_rows(
    evs: list[dict], start: date, end: date, source: str | None = None,
    symbol: str | None = None,
) -> list[dict]:
    """缓存载荷行 → 窗口内事件行 (date 统一 date 对象; source/symbol 补齐)."""
    picked = []
    for r in evs or []:
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


# ---------------------------------------------------------------------------
# cn 域: akshare 整批 (预约披露 + 分红)
# ---------------------------------------------------------------------------


def _cn_code_to_yahoo(code: str) -> str | None:
    """东财 6 位代码 → 内部规范代码 (600519→600519.SS / 000001→.SZ / 830799→.BJ).

    裸 6 位数字 parse() 不放行 (被视为无法识别的代码), 这里按交易所号段
    直接映射: 60/68 沪主板/科创 (60/68)→.SS; 00/30 深主板/创业板→.SZ;
    43/83/87/88/920 北交所→.BJ; 900xx/200xxx B 股→.SS/.SZ; 其余 None。
    """
    c = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", c):
        return None
    if c.startswith("900"):
        return f"{c}.SS"  # B 股 (沪)
    if c.startswith("200"):
        return f"{c}.SZ"  # B 股 (深)
    if c.startswith(("60", "68")):
        return f"{c}.SS"
    if c.startswith(("00", "30")):
        return f"{c}.SZ"
    if c.startswith(("43", "83", "87", "88", "920")):
        return f"{c}.BJ"
    return None


def _pd_date(v) -> date | None:
    """pandas Timestamp / NaT / str 'YYYY-MM-DD' → date (脏输入容错).

    pandas NaT 是 datetime 子类 (isinstance 命中 datetime 分支) 且
    NaT.date() 返回 NaT 本身, 必须先用 NaN 比较判伪 (v != v 对 NaT 为 True)。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, datetime):
        if v != v:  # NaT (NaN-like): NaN 比较判伪对 datetime 值恒 False
            return None
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s or s.lower() in ("nat", "nan", "none"):
        return None
    s = s[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _cn_num(v) -> str | None:
    """数值格 (可能 NaN) → 展示串; None 表示缺失不拼 note."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    from math import isfinite

    if not isfinite(f):
        return None
    return f"{f:g}"


def _market_label_for_symbol(yahoo: str) -> str:
    try:
        return parse(yahoo).market_label
    except ValueError:
        return yahoo


def _cn_normalize_disclosure(df, period: str) -> list[dict]:
    """stock_yysj_em 全市场 DataFrame → 全市场财报事件行 (读取时再过滤)."""
    if df is None or df.empty:
        return []
    rows = []
    code_col = next(
        (c for c in df.columns if "代码" in str(c)), None
    )
    name_col = next((c for c in df.columns if "简称" in str(c)), None)
    if code_col is None:
        return []
    for _, r in df.iterrows():
        yahoo = _cn_code_to_yahoo(r.get(code_col))
        if yahoo is None:
            continue
        d, status, _chain = _pick_cn_scheduled_row(r)
        if d is None:
            continue
        ev = {
            "symbol": yahoo,
            "type": "earnings",
            "date": d.isoformat(),
            "date_status": status,
            "source": "akshare",
            "market_label": "A股",
            "period": period,
        }
        if name_col:
            nm = str(r.get(name_col) or "").strip()
            if nm:
                ev["name"] = nm
        rows.append(ev)
    return rows


def _pick_cn_scheduled_row(r) -> tuple[date | None, str, int]:
    """预约披露行 → (生效日期, date_status, 变更次数).

    预约已变更时取最深变更日 (当前预约), 实际披露 (已披露) 优先。
    旧 df 里的日期列可能是 Series/Timestamp/字符串混合, _pd_date 统一处理。
    """

    def g(name: str) -> date | None:
        try:
            return _pd_date(r.get(name))
        except Exception:  # noqa: BLE001 - 列名不齐时静默缺失
            return None

    first = g("首次预约时间")
    changed = None
    chain = 0
    for k in ("一次变更日期", "二次变更日期", "三次变更日期"):
        if g(k) is not None:
            changed = g(k)
            chain += 1
    actual = g("实际披露时间")
    d, status = (changed, "changed") if changed is not None else (first, "planned")
    if actual is not None:
        d, status = actual, "actual"
    return d, status, chain


def _cn_normalize_fhps(df) -> list[dict]:
    """stock_fhps_em 全市场 DataFrame → 分红事件行 (登记日/除权除息两种)."""
    if df is None or df.empty:
        return []
    code_col = next((c for c in df.columns if "代码" in str(c)), None)
    name_col = next((c for c in df.columns if "名称" in str(c)), None)
    progress_col = next((c for c in df.columns if "方案进度" in str(c)), None)
    if code_col is None:
        return []
    rows = []
    for _, r in df.iterrows():
        yahoo = _cn_code_to_yahoo(r.get(code_col))
        if yahoo is None:
            continue
        note_bits = []
        for label, col in (
            ("现金分红比例", "现金分红-现金分红比例"),
            ("送转总比例", "送转股份-送转总比例"),
        ):
            v = _cn_num(r.get(col)) if col in df.columns else None
            if v:
                note_bits.append(f"{label} {v}")
        note = "·".join(note_bits) or None
        if progress_col:
            prog = str(r.get(progress_col) or "").strip()
            if prog:
                note = f"{note} [{prog}]" if note else prog
        nm = str(r.get(name_col) or "").strip() if name_col else ""
        for label, col, typ in (
            ("股权登记日", "股权登记日", "record_date"),
            ("除权除息日", "除权除息日", "ex_dividend"),
        ):
            d = _pd_date(r.get(col)) if col in df.columns else None
            if d is None:
                continue
            rows.append(
                {
                    "symbol": yahoo,
                    "name": nm or None,
                    "type": typ,
                    "date": d.isoformat(),
                    "date_status": "planned",
                    "note": note,
                    "source": "akshare",
                    "market_label": "A股",
                }
            )
    return rows


@contextmanager
def _quiet_stderr():
    """静音 akshare 内层 tqdm 进度条 (tqdm 构造时捕获 sys.stderr).

    CLI/UI 日历请求一次拉全市场 5 千行, 内层进度条只会刷屏; 我们自己的
    单票循环进度 (fetch_calendar progress 回调) 打到 stderr 的提示不受
    影响 —— 重定向窗口最短。
    """
    import os
    import sys

    devnull = open(os.devnull, "w", encoding="utf-8")
    old = sys.stderr
    sys.stderr = devnull
    try:
        yield
    finally:
        sys.stderr = old
        devnull.close()


def _fetch_cn_period(period: str, refresh: bool, progress=None) -> tuple[dict, list[str]]:
    """一个报表期的整市场日历 (缓存优先) → {"events": [...]}, issues."""
    key = f"cn:{period}"
    row = None if refresh else cache_mod.get_calendar_row(key)
    if row is not None:
        return {"events": row["events"]}, []
    import akshare as ak

    evs: list[dict] = []
    issues: list[str] = []
    with _quiet_stderr():
        for board in ("沪深A股", "京市A股"):
            try:
                df = ak.stock_yysj_em(symbol=board, date=period)
            except Exception as e:  # noqa: BLE001 - 整批失败注记并继续另一板
                issues.append(f"akshare 预约披露 {period} {board} 失败 ({e})"[:200])
                continue
            evs.extend(_cn_normalize_disclosure(df, period))
        try:
            df2 = ak.stock_fhps_em(date=period)
            evs.extend(_cn_normalize_fhps(df2))
        except Exception as e:  # noqa: BLE001 - 分红失败只在 events 缺口上可见
            issues.append(f"akshare 分红 {period} 失败 ({e})"[:200])
    cache_mod.set_calendar_row(key, evs)
    return {"events": evs}, issues


def _fetch_cn_events(
    symbols: set[str], start: date, end: date, refresh: bool
) -> tuple[list[dict], list[str], dict[str, str]]:
    """cn 域事件: 每报表期整批缓存 + 本窗口过滤. → events, issues, names."""
    events: list[dict] = []
    issues: list[str] = []
    names: dict[str, str] = {}
    for period in cn_periods_for_window(start, end):
        data, p_issues = _fetch_cn_period(period, refresh)
        issues.extend(p_issues)
        for ev in data["events"]:
            sym = ev["symbol"]
            d = _pd_date(ev.get("date"))
            if sym in symbols and _in_window(d, start, end):
                events.append({**ev, "date": d})
                if sym not in names and ev.get("name"):
                    names[sym] = ev["name"]
    return events, issues, names


# ---------------------------------------------------------------------------
# longport 兜底: CalendarContext 整批 (US/HK/SH/SZ, 每市场每类别按月缓存)
# ---------------------------------------------------------------------------

_LP_MARKETS = ("US", "HK", "SH", "SZ")
_LP_CATEGORIES = ("Report", "Dividend")


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
    typ = {"Report": "earnings", "Dividend": "ex_dividend"}.get(category, "other")
    bits = []
    content = str(getattr(info, "content", "") or "").strip()
    if content:
        bits.append(content)
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
        "date_type": str(getattr(info, "date_type", "") or "") or None,
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


def _fetch_lp_month_events(
    market: str, category: str, seg_start: date, seg_end: date, cfg: dict | None = None
) -> tuple[list[dict], str | None]:
    """一个 (market, category, 月) 的整批日历 (缓存优先) → events, error."""
    key = f"lp:{market}:{category}:{seg_start.strftime('%Y-%m')}"
    row = cache_mod.get_calendar_row(key)
    if row is not None:
        return row["events"], None
    try:
        from longport.openapi import CalendarCategory, CalendarContext

        api = _lp_config_builder(cfg)
    except Exception as e:  # noqa: BLE001 - 统一转错误信息
        return [], f"长桥日历不可用: {type(e).__name__}: {e}"[:200]
    evs: list[dict] = []
    cursor = seg_start.isoformat()
    end_iso = seg_end.isoformat()
    try:
        while cursor <= end_iso:
            cat = getattr(CalendarCategory, category)
            resp = api.finance_calendar(cat, cursor, end_iso, market)
            for group in getattr(resp, "list", []) or []:
                for info in getattr(group, "infos", []) or []:
                    ev = normalize_longport_event(info, category)
                    if ev is not None and (
                        seg_start
                        <= (date.fromisoformat(ev["date"]))
                        <= seg_end
                    ):
                        evs.append(ev)
            cursor = str(getattr(resp, "next_date", "") or "").strip()
            if not cursor or cursor > end_iso:
                break
    except Exception as e:  # noqa: BLE001 - 带日期的错误兜底
        return evs, f"长桥日历({market}/{category})失败: {type(e).__name__}: {e}"[:200]
    cache_mod.set_calendar_row(key, evs)
    return evs, None


def _lp_config_builder(cfg: dict | None):
    """构造 LongPort CalendarContext (借用 tracker.longport 的 config 凭据链)."""
    from .longport import build_config

    from longport.openapi import CalendarContext

    return CalendarContext(build_config(cfg))


def _fetch_longport_events(
    symbols: set[str], start: date, end: date, cfg: dict | None = None
) -> tuple[dict[str, list[dict]], str | None]:
    """长桥整批兜底: 只拉长桥支持且自选中存在的市场 → {symbol: 窗口事件}."""
    markets = sorted({_lp_market_axis(s) for s in symbols} - {None})
    if not markets:
        return {}, None
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    err = None
    for market in markets:
        for category in _LP_CATEGORIES:
            for seg_start, seg_end in _month_starts(start, end):
                evs, e = _fetch_lp_month_events(
                    market, category, seg_start, seg_end, cfg=cfg
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
) -> dict:
    """组合/自选的财经日历 → {"window", "events", "issues", "sources", "names"}.

    symbols: 内部规范代码列表 (crypto/index 由调用方过滤, 这里再保一层静默跳过)。
    names: 调用方可带 {symbol: 展示名} (watchlist 名/组合备注), 事件行缺名称
    时按它补齐 —— yfinance 单票路径不回名称, 组合/自选名称由这层接上。
    events 按 (date, symbol) 升序; issues 为主源失败/兜底结果的可读注记。
    progress(done, total, symbol) 供 CLI 打进度 (refresh 大自选时可见)。
    """
    symbols = list(dict.fromkeys(str(s).strip().upper() for s in symbols if s))
    if end is None or start is None:
        s0, e0 = window_from_months(max(1, months))
        start = start or s0
        end = end or e0

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
    sources: dict[str, int] = {}

    # ---- cn 域 ----
    if cn_syms:
        cn_events, cn_issues, cn_names = _fetch_cn_events(
            cn_syms, start, end, refresh
        )
        events.extend(cn_events)
        issues.extend(cn_issues)
        names.update(cn_names)
        sources["akshare"] = sum(
            1 for e in cn_events if e.get("source") == "akshare"
        )
        # akshare 整批失败 (无任何事件) 且开兜底: SH/SZ 走 longport
        got = {e["symbol"] for e in cn_events}
        missing_cn = cn_syms - got
        if (cn_issues or not cn_events) and missing_cn and use_longport:
            try:
                lp_out, lp_err = _fetch_longport_events(missing_cn, start, end)
            except Exception as e:  # noqa: BLE001 - 兜底失败不拖主流程
                lp_out, lp_err = {}, f"长桥日历不可用: {e}"[:200]
            for _sym, evs2 in lp_out.items():
                events.extend(evs2)
            if lp_err:
                issues.append(lp_err)
            _bump_lp_sources(sources, lp_out)

    # ---- global 域 ----
    if global_syms:
        global_out, g_issues, g_stats = _fetch_global_events(
            global_syms, start, end, refresh, progress=progress
        )
        for _sym, evs2 in global_out.items():
            events.extend(evs2)
        issues.extend(g_issues)
        sources["yfinance"] = sum(
            1 for evs2 in global_out.values() for ev in evs2
        )
        # use_longport 兜底: 主链完全无事件的 US/HK 代码补长桥日历
        if use_longport:
            empty_syms = {s for s, evs2 in global_out.items() if not evs2}
            lpable = {
                s
                for s in empty_syms
                if _lp_market_axis(s) in ("US", "HK")
            }
            if lpable:
                try:
                    lp_out, lp_err = _fetch_longport_events(lpable, start, end)
                except Exception as e:  # noqa: BLE001
                    lp_out, lp_err = {}, f"长桥日历不可用: {e}"[:200]
                for _sym, evs3 in lp_out.items():
                    if evs3:
                        events.extend(evs3)
                if lp_err:
                    issues.append(lp_err)
                _bump_lp_sources(sources, lp_out)

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
    events = sorted(
        (e for e in events if e["date"] is not None),
        key=lambda e: (e["date"], e["symbol"], e["type"]),
    )
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
    }


def _bump_lp_sources(sources: dict, lp_out: dict) -> None:
    """longport 兜底命中条数计入 sources['longport'] (有命中才计)."""
    n = sum(
        1
        for evs in lp_out.values()
        for ev in evs
        if ev.get("source") == "longport"
    )
    if n:
        sources["longport"] = sources.get("longport", 0) + n
