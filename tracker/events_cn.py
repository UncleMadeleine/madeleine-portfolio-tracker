"""CN 日历纯归一化 helpers (从 events.py 拆出, 便于单独维护/测试)."""

from __future__ import annotations

import re
from datetime import date, datetime

from .symbols import parse


def _cn_code_to_yahoo(code: str) -> str | None:
    """东财 6 位代码 → 内部规范代码 (600519→600519.SS / 000001→.SZ / 830799→.BJ)."""
    c = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", c):
        return None
    if c.startswith("900"):
        return f"{c}.SS"
    if c.startswith("200"):
        return f"{c}.SZ"
    if c.startswith(("60", "68")):
        return f"{c}.SS"
    if c.startswith(("00", "30")):
        return f"{c}.SZ"
    if c.startswith(("43", "83", "87", "88", "920")):
        return f"{c}.BJ"
    return None


def _pd_date(v) -> date | None:
    """pandas Timestamp / NaT / str 'YYYY-MM-DD' → date (脏输入容错)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, datetime):
        if v != v:  # NaT
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
    if f != f:
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


def _pick_cn_scheduled_row(r) -> tuple[date | None, str, int]:
    """预约披露行 → (生效日期, date_status, 变更次数)."""

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


def _cn_normalize_disclosure(df, period: str) -> list[dict]:
    """stock_yysj_em 全市场 DataFrame → 全市场财报事件行 (读取时再过滤)."""
    if df is None or df.empty:
        return []
    rows = []
    code_col = next((c for c in df.columns if "代码" in str(c)), None)
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
            ("现金分红(元/10股)", "现金分红-现金分红比例"),
            ("送转(股/10股)", "送转股份-送转总比例"),
        ):
            v = _cn_num(r.get(col)) if col in df.columns else None
            # 送转-only 方案 (现金分红=0) 不应出现 "现金分红 … 0" 的噪音;
            # _cn_num 返回字符串 "0"/"-0", 不能用真值判断。
            if v is not None and float(v) != 0:
                note_bits.append(f"{label} {v}")
        note = "·".join(note_bits) or None
        if progress_col:
            prog = str(r.get(progress_col) or "").strip()
            if prog:
                note = f"{note} [{prog}]" if note else prog
        nm = str(r.get(name_col) or "").strip() if name_col else ""
        for _label, col, typ in (
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
