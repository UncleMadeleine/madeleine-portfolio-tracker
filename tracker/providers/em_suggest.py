"""东财 suggest 搜索客户端 (global/cn 两域共用的底层 HTTP 封装).

接口: searchapi.eastmoney.com/api/suggest/get, 支持中文名/拼音/代码片段,
一次请求 ~1s; 返回原始行, 由各 provider 按域过滤并归一为规范代码。
provider 之外 (tracker.search 聚合层) 不感知该接口细节。
"""
from __future__ import annotations

import json
import re

import requests

from ..util import with_timeout

_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
_SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"  # 东财网页公开 token
_TIMEOUT = 5.0

_CODE_RE = re.compile(r"[0-9A-Za-z]+")


def suggest_raw(query: str) -> list[dict] | None:
    """东财 suggest 原始行; None=网络失败 (区别于 []=在线可达但无结果)."""
    params = {
        "input": query,
        "type": "14",
        "token": _SUGGEST_TOKEN,
        "count": "40",
    }
    try:
        resp = with_timeout(
            requests.get,
            _TIMEOUT,
            _SUGGEST_URL,
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.eastmoney.com/",
            },
        )
        data = resp.json()
    except Exception:
        return None
    return (data.get("QuotationCodeTable") or {}).get("Data") or []


def suggest_merged(query: str) -> tuple[list[dict], bool] | None:
    """suggest_raw + 纯数字补零二次请求 (700 → 00700 命中港股), 按行内容去重.

    返回 (合并行, 是否任一次请求网络失败); 接口整体不可达时为 None。
    """
    first = suggest_raw(query)
    rows, failed = first or [], first is None
    if query.isdigit():
        padded = query.zfill(5)
        if padded != query:
            extra = suggest_raw(padded)
            if extra is None:
                failed = True
            else:
                got = {json.dumps(r, sort_keys=True) for r in rows}
                rows += [
                    r for r in extra if json.dumps(r, sort_keys=True) not in got
                ]
    if failed and not rows:
        return None
    return rows, failed


def normalize_suggest_row(row: dict) -> tuple[str, str] | None:
    """单行 → (东财代码, SecurityTypeName); 形态不完整返回 None."""
    stype = row.get("SecurityTypeName")
    code = str(row.get("Code", "")).strip()
    name = str(row.get("Name", "")).strip()
    if not stype or not code or not name or not _CODE_RE.fullmatch(code):
        return None
    return code, stype


def em_code_to_yahoo(code: str, stype: str) -> str:
    """东财代码 + SecurityTypeName → Yahoo 规范代码."""
    if stype == "港股":
        return f"{int(code):04d}.HK"
    if stype in ("沪A", "沪B", "科创版", "科创板"):
        return f"{code}.SS"
    if stype in ("深A", "深B"):
        return f"{code}.SZ"
    if stype == "京A":
        return f"{code}.BJ"
    return code.upper()  # 美股


def parse_symbol_safe(yahoo: str):
    """parse 包装: 非法代码返回 None, 供搜索结果过滤。"""
    from ..symbols import parse

    try:
        return parse(yahoo)
    except ValueError:
        return None
