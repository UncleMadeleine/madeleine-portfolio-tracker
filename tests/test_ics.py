"""iCalendar 导出测试: RFC 5545 结构 / 转义 / 折行 / CLI 落盘 (全离线)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from tracker.ics import build_ics, ics_filename


def _unfold(text: str) -> list[str]:
    """还原折叠行 (续行以空格开头)."""
    out: list[str] = []
    for line in text.split("\r\n"):
        if not line:
            continue
        if line.startswith(" ") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _event(**over):
    ev = {
        "symbol": "AAPL",
        "name": "苹果公司",
        "type": "earnings",
        "date": "2026-11-03",
        "date_status": "estimate",
        "note": "EPS 估计 ~1.98",
        "source": "yfinance",
        "market_label": "美股",
    }
    ev.update(over)
    return ev


def test_build_ics_basic_structure():
    text = build_ics(
        [_event()],
        now=datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc),
    )
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")  # 只允许 CRLF
    lines = _unfold(text)
    assert "VERSION:2.0" in lines
    assert "BEGIN:VEVENT" in lines and "END:VEVENT" in lines
    assert "DTSTAMP:20261101T093000Z" in lines
    assert "DTSTART;VALUE=DATE:20261103" in lines
    # 全天事件 DTEND 为开区间 → 次日
    assert "DTEND;VALUE=DATE:20261104" in lines
    assert "SUMMARY:财报 AAPL 苹果公司" in lines
    assert "CATEGORIES:财报" in lines
    assert "TRANSP:TRANSPARENT" in lines
    desc = next(line for line in lines if line.startswith("DESCRIPTION:"))
    assert "代码: AAPL" in desc
    assert "市场: 美股" in desc
    assert "状态: 估计" in desc
    assert "来源: yfinance" in desc


def test_build_ics_escapes_text():
    ev = _event(
        name="苹果,公司;A\\B\n第二行",
        note="备注;含,逗号\\反斜杠\n换行",
    )
    lines = _unfold(build_ics([ev]))
    summary = next(line for line in lines if line.startswith("SUMMARY:"))
    assert summary == "SUMMARY:财报 AAPL 苹果\\,公司\\;A\\\\B\\n第二行"
    desc = next(line for line in lines if line.startswith("DESCRIPTION:"))
    assert "备注\\;含\\,逗号\\\\反斜杠\\n换行" in desc


def test_build_ics_folds_long_line_by_utf8_bytes():
    ev = _event(name="非常长的公司名称" * 12)
    text = build_ics([ev])
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    summary = next(line for line in _unfold(text) if line.startswith("SUMMARY:"))
    assert "非常长的公司名称" * 12 in summary


def test_build_ics_uid_is_stable_and_dedupes():
    ev = _event()
    first = build_ics([ev], now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = build_ics([ev], now=datetime(2026, 2, 2, tzinfo=timezone.utc))
    uid1 = next(line for line in _unfold(first) if line.startswith("UID:"))
    uid2 = next(line for line in _unfold(second) if line.startswith("UID:"))
    assert uid1 == uid2  # 重复导出不会在日历里新增重复事件

    doubled = build_ics([ev, dict(ev)])
    assert doubled.count("BEGIN:VEVENT") == 1


def test_build_ics_uid_survives_earnings_date_shift():
    """财报同季度内改期 → UID 不变; 分红等一次性日期仍按日区分."""
    old = build_ics([_event(date="2026-11-03")])
    new = build_ics([_event(date="2026-11-05")])
    uid_old = next(line for line in _unfold(old) if line.startswith("UID:"))
    uid_new = next(line for line in _unfold(new) if line.startswith("UID:"))
    assert uid_old == uid_new
    assert "DTSTART;VALUE=DATE:20261105" in _unfold(new)

    div_old = build_ics([_event(type="ex_dividend", date="2026-11-03")])
    div_new = build_ics([_event(type="ex_dividend", date="2026-11-05")])
    div_uid_old = next(line for line in _unfold(div_old) if line.startswith("UID:"))
    div_uid_new = next(line for line in _unfold(div_new) if line.startswith("UID:"))
    assert div_uid_old != div_uid_new


def test_build_ics_skips_invalid_and_empty_is_valid():
    text = build_ics(
        [
            _event(date="not-a-date"),
            _event(date=None),
            "not-a-dict",
        ]
    )
    assert "BEGIN:VEVENT" not in text
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.endswith("END:VCALENDAR\r\n")
    # 不同类型/日期生成不同 UID, 不会被误去重
    two = build_ics(
        [
            _event(type="earnings", date="2026-11-03"),
            _event(type="ex_dividend", date="2026-11-03"),
        ]
    )
    assert two.count("BEGIN:VEVENT") == 2


def test_ics_filename_uses_window():
    assert (
        ics_filename({"start": "2026-10-01", "end": "2026-11-30"})
        == "calendar_20261001_20261130.ics"
    )
    assert ics_filename(None) == "calendar.ics"


def test_cli_calendar_ics_output(tmp_path, monkeypatch):
    """CLI --ics -o: 落盘为 UTF-8 + CRLF, 并打印导出摘要."""
    from tracker.cli import calendar as cal_mod

    result = {
        "window": {"start": "2026-11-01", "end": "2026-11-30", "months": 1},
        "events": [_event()],
        "issues": [],
        "sources": {"yfinance": 1},
        "names": {},
    }
    calls = {}

    def fake_fetch(symbols, **kwargs):
        calls["symbols"] = symbols
        calls["progress"] = kwargs.get("progress")
        return result

    monkeypatch.setattr(cal_mod.events_mod, "fetch_calendar", fake_fetch)

    out = tmp_path / "cal.ics"
    args = SimpleNamespace(
        symbols=["AAPL"],
        portfolio=str(tmp_path / "portfolio.json"),
        watchlist_file=str(tmp_path / "watchlist.json"),
        watchlist=None,
        months=1,
        refresh=False,
        longport=False,
        json=False,
        ics=True,
        output=str(out),
    )
    cal_mod.cmd_calendar(args)

    raw = out.read_bytes()
    assert raw.startswith(b"BEGIN:VCALENDAR\r\n")
    assert raw.endswith(b"END:VCALENDAR\r\n")
    assert b"SUMMARY:" in raw
    assert calls["symbols"] == ["AAPL"]
    assert calls["progress"] is None  # 导出 ics 不打印进度, 避免污染文件


def test_cli_calendar_rejects_conflicting_flags(monkeypatch):
    import pytest

    from tracker.cli import calendar as cal_mod

    args = SimpleNamespace(
        symbols=["AAPL"],
        portfolio="p.json",
        watchlist_file="w.json",
        watchlist=None,
        months=2,
        refresh=False,
        longport=False,
        json=True,
        ics=True,
        output=None,
    )
    with pytest.raises(SystemExit):
        cal_mod.cmd_calendar(args)
