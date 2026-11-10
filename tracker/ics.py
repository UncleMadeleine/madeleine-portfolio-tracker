"""财经日历 → iCalendar (.ics) 导出 (RFC 5545, 无第三方依赖).

把 ``tracker.events.fetch_calendar()`` 的事件转成全天 ``VEVENT``, 可直接
导入 Apple 日历 / Google Calendar / Outlook 等本地日历应用。

设计约定:

- **稳定 UID**: 由 ``(代码, 事件类型, 期次)`` 哈希生成。重复导出同一事件时
  UID 不变, 日历应用会更新原事件而不是新增一条; 财报按自然季度做期次,
  同季度内改期也仍是同一个 UID (不会留下一旧一新两条)。
- **全天事件**: ``DTSTART;VALUE=DATE`` + ``DTEND`` 次日 (RFC 5545 的结束日期
  为开区间), 不依赖时区, 也不占用用户的具体时间段。
- **文本转义 + 75 字节折行**: 名称/说明里的逗号、分号、反斜杠、换行按 RFC
  转义; 长中文行按 UTF-8 字节折行, 避免部分日历客户端解析失败。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from .events import DATE_STATUS_LABELS, EVENT_TYPE_LABELS

# 日历元数据 (SUMMARY/UID 域名等对外可见)
PRODID = "-//Madeleine Portfolio Tracker//Financial Calendar//ZH"
CALENDAR_NAME = "财经日历"
CALENDAR_DESC = "持仓与自选的财报、股权登记、除权除息与派息事件"
UID_DOMAIN = "madeleine-tracker"

# RFC 5545 规定每行 (含折行后的 UTF-8 字节) 不超过 75 octets
_FOLD_LIMIT = 75


def _escape_text(value: object) -> str:
    """按 RFC 5545 TEXT 规则转义 (反斜杠/分号/逗号/换行)."""
    text = "" if value is None else str(value)
    text = (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )
    return text


def _fold_line(line: str, limit: int = _FOLD_LIMIT) -> list[str]:
    """把一条内容行按 UTF-8 字节折到 ``limit`` 以内 (续行以空格开头).

    不能按字符数折行: 一个中文字符是 3 字节, 按字符切会让物理行超过 75
    字节, 严格的客户端 (如 Outlook) 会拒绝整份日历。续行前导空格也计入
    75 字节上限, 因此非首行可用 74 字节。
    """
    if len(line.encode("utf-8")) <= limit:
        return [line]

    chunks: list[str] = []
    current: list[str] = []
    used = 0
    first = True
    for ch in line:
        size = len(ch.encode("utf-8"))
        cap = limit if first else limit - 1
        if current and used + size > cap:
            chunks.append("".join(current))
            first = False
            current = []
            used = 0
        current.append(ch)
        used += size
    if current:
        chunks.append("".join(current))
    return [chunks[0], *(" " + chunk for chunk in chunks[1:])]


def _as_date(value: object) -> date | None:
    """事件日期 → date; 支持 date/datetime/ISO 字符串, 非法值静默跳过."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _utc_stamp(now: datetime | None) -> str:
    """DTSTAMP 使用 UTC 紧凑格式 (无时区行为按 UTC 处理)."""
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _uid_period(event_type: str, day: date) -> str:
    """事件期次: 财报按自然季度, 其它类型按具体日期.

    财报改期很常见 (预约披露日调整)。若 UID 里带具体日期, 每次改期都会
    变成一条新事件, 旧日期那条仍留在用户日历里。用「年+季度」做期次后,
    同季度内改期只更新 DTSTART, UID 保持不变; 分红/除息等一次性日期仍按
    具体日期区分, 避免同季度多次派息被误合并。
    """
    if event_type == "earnings":
        return f"{day.year}Q{(day.month - 1) // 3 + 1}"
    return day.isoformat()


def _event_uid(symbol: str, event_type: str, day: date) -> str:
    """稳定且对客户端安全的 UID (避免 ^ / 空格等字符惹麻烦)."""
    seed = f"{symbol}|{event_type}|{_uid_period(event_type, day)}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]
    return f"{digest}@{UID_DOMAIN}"


def _summary(ev: dict, label: str) -> str:
    """标题: 「财报 AAPL 苹果公司」; 名称与代码重复时不重复追加."""
    symbol = str(ev.get("symbol") or "").strip()
    name = str(ev.get("name") or "").strip()
    parts = [label]
    if symbol:
        parts.append(symbol)
    if name and name != symbol:
        parts.append(name)
    return " ".join(parts)


def _description(ev: dict, label: str) -> str:
    """说明: 市场/状态/备注/来源, 空字段不输出."""
    rows: list[tuple[str, str]] = []
    market = str(ev.get("market_label") or "").strip()
    status = DATE_STATUS_LABELS.get(str(ev.get("date_status") or ""), "")
    note = str(ev.get("note") or "").strip()
    source = str(ev.get("source") or "").strip()
    symbol = str(ev.get("symbol") or "").strip()
    if symbol:
        rows.append(("代码", symbol))
    if market:
        rows.append(("市场", market))
    if status:
        rows.append(("状态", status))
    if note:
        rows.append(("说明", note))
    if source:
        rows.append(("来源", source))
    if not rows:
        return label
    return "\n".join(f"{key}: {value}" for key, value in rows)


def _event_lines(ev: dict, dtstamp: str) -> list[str]:
    """单个事件 → 未折行的属性行 (无有效日期时返回空列表)."""
    day = _as_date(ev.get("date"))
    if day is None:
        return []
    symbol = str(ev.get("symbol") or "").strip()
    event_type = str(ev.get("type") or "").strip()
    label = EVENT_TYPE_LABELS.get(event_type, event_type or "事件")
    uid = _event_uid(symbol, event_type, day)
    end = day + timedelta(days=1)

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
        f"DTEND;VALUE=DATE:{end:%Y%m%d}",
        f"SUMMARY:{_escape_text(_summary(ev, label))}",
        f"DESCRIPTION:{_escape_text(_description(ev, label))}",
        f"CATEGORIES:{_escape_text(label)}",
        "STATUS:CONFIRMED",
        "TRANSP:TRANSPARENT",
        "SEQUENCE:0",
        "END:VEVENT",
    ]
    return lines


def build_ics(
    events,
    *,
    calendar_name: str = CALENDAR_NAME,
    calendar_desc: str = CALENDAR_DESC,
    now: datetime | None = None,
) -> str:
    """事件列表 → iCalendar 文本 (CRLF 行尾, 可直接写入 .ics 文件).

    ``events`` 接受 ``fetch_calendar()`` 的 ``events`` 字段; 缺日期/日期非法
    的事件会被跳过, 相同 (代码, 类型, 日期) 的事件只保留第一条。空列表也会
    生成一份合法的空日历 (便于脚本无条件导出)。
    """
    dtstamp = _utc_stamp(now)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_escape_text(PRODID)}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape_text(calendar_name)}",
        f"X-WR-CALDESC:{_escape_text(calendar_desc)}",
    ]

    seen: set[str] = set()
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        day = _as_date(ev.get("date"))
        if day is None:
            continue
        symbol = str(ev.get("symbol") or "").strip()
        event_type = str(ev.get("type") or "").strip()
        uid = _event_uid(symbol, event_type, day)
        if uid in seen:
            continue
        seen.add(uid)
        lines.extend(_event_lines(ev, dtstamp))

    lines.append("END:VCALENDAR")
    folded: list[str] = []
    for line in lines:
        folded.extend(_fold_line(line))
    return "\r\n".join(folded) + "\r\n"


def ics_filename(window: dict | None = None) -> str:
    """导出文件名: ``calendar_20261101_20261130.ics`` (窗口缺失时用默认名)."""
    start = _as_date((window or {}).get("start"))
    end = _as_date((window or {}).get("end"))
    if start and end:
        return f"calendar_{start:%Y%m%d}_{end:%Y%m%d}.ics"
    return "calendar.ics"
