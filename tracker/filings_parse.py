"""财报链接的通用数据模型与解析工具 (无网络).

从 filings.py 拆出: Filing 数据结构、HTML/日期清洗、按日期倒序去重与
每种报告保留最近两期的规则。各市场解析器与公开入口共用。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_LIMIT_PER_KIND = 2  # 每种报告最多保留最近两期 (本期 + 上期)


@dataclass(frozen=True)
class Filing:
    """一份已披露的财报/定期报告 (链接指向官方原文)."""

    title: str
    date: str  # YYYY-MM-DD
    url: str
    kind: str
    source: str


def _clean_text(value: object) -> str:
    """去 HTML 标签/实体并压缩空白 (巨潮/港交所标题可能带 <em>/<br/>)."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _iso_date(value: object) -> str:
    """任意常见日期字符串 → YYYY-MM-DD; 无法识别返回空串."""
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", str(value or ""))
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _hk_date(value: object) -> str:
    """港交所 DD/MM/YYYY HH:MM → YYYY-MM-DD."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(value or ""))
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _finalize(
    items: list[Filing], limit: int, *, same_day: bool = False
) -> list[Filing]:
    """按日期倒序去重, 每种报告最多保留最近两期.

    same_day=False: 同 (标题, 日期) 去重;
    same_day=True:  同 (类型, 日期) 去重 —— ASX / Companies House / TDnet /
    DART 同一天常同时发多份同类型公告 (英文/日文、原件/更正、集团/母公司),
    只保留最先出现的一条。
    """
    ordered = sorted(items, key=lambda f: f.date, reverse=True)
    seen: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}
    out: list[Filing] = []
    for item in ordered:
        key = (item.kind, item.date) if same_day else (item.title, item.date)
        if key in seen:
            continue
        seen.add(key)
        if counts.get(item.kind, 0) >= _LIMIT_PER_KIND:
            continue
        counts[item.kind] = counts.get(item.kind, 0) + 1
        out.append(item)
    return out[: max(1, int(limit))]


def _finalize_same_day(items: list[Filing], limit: int) -> list[Filing]:
    return _finalize(items, limit, same_day=True)
