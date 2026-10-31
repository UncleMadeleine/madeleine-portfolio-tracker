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
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from . import cache as cache_mod
from .symbols import parse

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

# 单票缓存 TTL (秒): 有事件 6h (与 fundamentals 一致的低频报表口径)
CAL_TTL = cache_mod.CALENDAR_TTL
# 单票空结果负缓存 TTL: Yahoo 无数据的标的 24h 内不重试
CAL_NEG_TTL = 86400
# 陈旧可渲染期: 超过正/负缓存 TTL 但未超过该年龄的日历仍先展示, 由页面/
# 后台线程静默重取 (与行情 cache.STALE_TTL 同构的 stale-while-revalidate)。
# 日历是低频数据, 展示 7 天内的旧结果远比首屏干等整批网络请求可用。
CAL_STALE_TTL = 7 * 86400
# 预约披露覆盖窗口: 报表期结束到实际/预约披露最长间隔约 4 个月 (年报),
# 报表期在窗口开始前 130 天内的仍可能披露落在窗口内, 一并拉取
_CN_PERIOD_BACKDAYS = 130
# 分红/送转覆盖窗口: 股权登记/除权除息日可能远晚于报告期末 (年中派息、方案
# 延后实施), 按 15 个月回看; 与预约披露的 130 天口径分开, 否则窗口内执行
# 的老报告期分红会被漏掉。
_CN_DIVIDEND_BACKDAYS = 450
# 单票 yfinance 取数间隔 (秒): 防连发 429; 与 tahlocks 无关的简单限时
_YF_PAUSE = 0.2

# 事件类型 → 展示名 (CLI/UI 共用)
EVENT_TYPE_LABELS = {
    "earnings": "财报",
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


def cn_dividend_periods_for_window(start: date, end: date) -> list[str]:
    """窗口内可能实施的 A 股分红 → 需要请求的报告期 ("YYYYMMDD") 升序.

    分红/送转的股权登记日与除权除息日可以在报告期结束后很久才执行 (年中
    派息、方案延后实施), 因此不能沿用预约披露的 130 天回看, 单独用
    _CN_DIVIDEND_BACKDAYS (15 个月) 覆盖老报告期的实施事件。
    """
    earliest = start - timedelta(days=_CN_DIVIDEND_BACKDAYS)
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


class _ProgressReporter:
    """取数进度累加器: 计划总量可后续追加 (长桥兜底按需才知道工作量).

    对外仍是 fetch_calendar(progress=(done, total, label)) 的旧契约:
    label 是当前代码/阶段的可读文本, total 可能随阶段增加。UI 用
    done/total 画进度条, CLI 直接打印文本。
    """

    def __init__(self, cb=None):
        self._cb = cb
        self.done = 0
        self.total = 0

    def plan(self, n: int) -> None:
        if n > 0:
            self.total += int(n)

    def step(self, label: str | None = None) -> None:
        self.done += 1
        if self._cb is not None:
            # total 只会追加不会回退; done 超过计划量时至少保证分母不倒退
            self._cb(self.done, max(self.total, self.done), label)

    def finish(self) -> None:
        if self._cb is not None:
            total = max(self.total, self.done)
            self._cb(total, total, None)


def _merge_cache_meta(meta: dict, part: dict | None) -> None:
    """合并分片缓存元数据 (stale/missing/stale_age/used) 到累计 meta."""
    if not part:
        return
    meta["stale"] = bool(meta.get("stale")) or bool(part.get("stale"))
    meta["missing"] = bool(meta.get("missing")) or bool(part.get("missing"))
    meta["used"] = bool(meta.get("used")) or bool(part.get("used"))
    try:
        meta["stale_age"] = max(
            float(meta.get("stale_age") or 0.0),
            float(part.get("stale_age") or 0.0),
        )
    except (TypeError, ValueError):
        pass


def _is_valid_eps(v: float) -> bool:
    from math import isfinite

    return isfinite(v) and v > 0


# ---------------------------------------------------------------------------
# global 域: yfinance 单票 calendar
# ---------------------------------------------------------------------------


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
    symbols: list[str],
    start: date,
    end: date,
    refresh: bool,
    reporter: _ProgressReporter | None = None,
    stale_ok: bool = False,
    cache_only: bool = False,
) -> tuple[dict[str, list[dict]], list[str], dict]:
    """yfinance 单票循环 → {symbol: 窗口过滤后事件}, issues, 执行统计.

    命中缓存的代码零请求零间隔; 未命中的逐只发出并隔 _YF_PAUSE (防连发
    429), 单票失败注记进 issues 不拖丢整批。

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
    for s in symbols:
        row = None if refresh else cache_mod.get_calendar_row(f"g:{s}")
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
    for i, s in enumerate(todo):
        try:
            evs = _fetch_global_one(s, refresh)
            out[s] = _window_rows(evs, start, end, source="yfinance", symbol=s)
            stats["fresh"] += 1
        except Exception as e:  # noqa: BLE001 - 单票失败不拖丢整批
            stats["fail"] += 1
            issues.append(f"{s}: yfinance 日历失败 ({type(e).__name__}: {e})"[:160])
        if reporter is not None:
            reporter.step(s)
        if i < len(todo) - 1:
            time.sleep(_YF_PAUSE)
    stats["stale_age"] = stale_age
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
        # 东财口径: 现金分红比例 = 每 10 股派息(元), 送转总比例 = 每 10 股送转(股)
        for label, col in (
            ("现金分红(元/10股)", "现金分红-现金分红比例"),
            ("送转(股/10股)", "送转股份-送转总比例"),
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


# sys.stderr 是进程级对象: 重定向窗口必须串行化, 否则 Streamlit 多会话/
# 多线程并发取日历时会互相覆盖, 甚至把 devnull 永久留在全局。
_STDERR_LOCK = threading.Lock()


@contextmanager
def _quiet_stderr():
    """静音 akshare 内层 tqdm 进度条 (tqdm 构造时捕获 sys.stderr).

    CLI/UI 日历请求一次拉全市场 5 千行, 内层进度条只会刷屏; 我们自己的
    单票循环进度 (fetch_calendar progress 回调) 打到 stderr 的提示不受
    影响 —— 重定向窗口最短。锁只包住一次网络请求, 并发会话因此串行取数,
    避免全局 stderr 被并发改写。
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


def _fetch_cn_yysj_board(
    board: str,
    period: str,
    refresh: bool,
    stale_ok: bool = False,
    cache_only: bool = False,
    reporter: _ProgressReporter | None = None,
) -> tuple[list[dict], str | None, dict]:
    """单个板块的预约披露 (缓存优先, TTL 6h) → events, error, meta.

    分板块独立缓存: 一块接口失败只影响该板, 不会把另一板的成功结果一起丢掉,
    也不会把「缺一块」的合并结果缓存 6h。meta 记录该行是否陈旧/缺失。
    """
    key = f"cn:yysj:{period}:{board}"
    meta = {"stale": False, "missing": False, "stale_age": 0.0, "used": False}
    row = None if refresh else cache_mod.get_calendar_row(key)
    if row is not None:
        age = _age_of(row["fetched_at"])
        if age <= CAL_TTL or (stale_ok and age <= CAL_STALE_TTL):
            meta.update(stale=age > CAL_TTL, stale_age=age, used=True)
            if reporter is not None:
                reporter.step(f"{period} {board}")
            return row["events"], None, meta
    if cache_only:
        meta["missing"] = True
        if reporter is not None:
            reporter.step(f"{period} {board}")
        return [], None, meta
    import akshare as ak

    try:
        with _quiet_stderr():
            df = ak.stock_yysj_em(symbol=board, date=period)
    except Exception as e:  # noqa: BLE001 - 单板失败注记并继续另一板
        if reporter is not None:
            reporter.step(f"{period} {board}")
        return [], f"akshare 预约披露 {period} {board} 失败 ({e})"[:200], meta
    evs = _cn_normalize_disclosure(df, period)
    cache_mod.set_calendar_row(key, evs)
    if reporter is not None:
        reporter.step(f"{period} {board}")
    return evs, None, meta


def _fetch_cn_fhps_period(
    period: str,
    refresh: bool,
    stale_ok: bool = False,
    cache_only: bool = False,
    reporter: _ProgressReporter | None = None,
) -> tuple[list[dict], str | None, dict]:
    """单个报表期的分红送转 (缓存优先, TTL 6h) → events, error, meta."""
    key = f"cn:fhps:{period}"
    meta = {"stale": False, "missing": False, "stale_age": 0.0, "used": False}
    row = None if refresh else cache_mod.get_calendar_row(key)
    if row is not None:
        age = _age_of(row["fetched_at"])
        if age <= CAL_TTL or (stale_ok and age <= CAL_STALE_TTL):
            meta.update(stale=age > CAL_TTL, stale_age=age, used=True)
            if reporter is not None:
                reporter.step(f"{period} 分红")
            return row["events"], None, meta
    if cache_only:
        meta["missing"] = True
        if reporter is not None:
            reporter.step(f"{period} 分红")
        return [], None, meta
    import akshare as ak

    try:
        with _quiet_stderr():
            df = ak.stock_fhps_em(date=period)
    except Exception as e:  # noqa: BLE001 - 分红失败只在 events 缺口上可见
        if reporter is not None:
            reporter.step(f"{period} 分红")
        return [], f"akshare 分红 {period} 失败 ({e})"[:200], meta
    evs = _cn_normalize_fhps(df)
    cache_mod.set_calendar_row(key, evs)
    if reporter is not None:
        reporter.step(f"{period} 分红")
    return evs, None, meta


def _fetch_cn_period(
    period: str,
    refresh: bool,
    stale_ok: bool = False,
    cache_only: bool = False,
    reporter: _ProgressReporter | None = None,
) -> tuple[dict, list[str]]:
    """一个报表期的整市场日历 (预约披露 + 分红, 各自独立缓存) → events, issues.

    meta 挂在 data 上 (stale/missing/stale_age/used), 保持原有二元组返回结构。
    """
    evs: list[dict] = []
    issues: list[str] = []
    meta = {"stale": False, "missing": False, "stale_age": 0.0, "used": False}
    for board in _CN_BOARDS:
        board_evs, issue, bmeta = _fetch_cn_yysj_board(
            board,
            period,
            refresh,
            stale_ok=stale_ok,
            cache_only=cache_only,
            reporter=reporter,
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
    )
    evs.extend(fhps_evs)
    if issue:
        issues.append(issue)
    _merge_cache_meta(meta, fmeta)
    return {"events": evs, **meta}, issues


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

    预约披露用 130 天回看; 分红送转单独用 15 个月回看 (旧报告期的方案可能在
    窗口内才实施), 只对预约披露集合之外的老报告期补拉 fhps。
    """
    events: list[dict] = []
    issues: list[str] = []
    names: dict[str, str] = {}
    meta = {"stale": False, "missing": False, "stale_age": 0.0, "used": False}

    def take(rows: list[dict]) -> None:
        for ev in rows:
            sym = ev["symbol"]
            d = _pd_date(ev.get("date"))
            if sym in symbols and _in_window(d, start, end):
                events.append({**ev, "date": d})
                if sym not in names and ev.get("name"):
                    names[sym] = ev["name"]

    earn_periods = cn_periods_for_window(start, end)
    earn_set = set(earn_periods)
    div_extra = [
        p for p in cn_dividend_periods_for_window(start, end) if p not in earn_set
    ]
    if reporter is not None:
        reporter.plan(len(earn_periods) * 3 + len(div_extra))
    for period in earn_periods:
        data, p_issues = _fetch_cn_period(
            period,
            refresh,
            stale_ok=stale_ok,
            cache_only=cache_only,
            reporter=reporter,
        )
        issues.extend(p_issues)
        take(data["events"])
        _merge_cache_meta(meta, data)
    for period in div_extra:
        period_evs, issue, pmeta = _fetch_cn_fhps_period(
            period,
            refresh,
            stale_ok=stale_ok,
            cache_only=cache_only,
            reporter=reporter,
        )
        if issue:
            issues.append(issue)
        take(period_evs)
        _merge_cache_meta(meta, pmeta)
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
    events.sort(key=lambda e: (e["date"], e["symbol"], e["type"]))
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
