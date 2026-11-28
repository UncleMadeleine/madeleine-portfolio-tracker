"""最新财报原文链接: A股 / 港股 / 澳股 / 英股 / 日股 / 韩股 / 美股.

只返回「报告标题 + 披露日期 + 官方原文 URL」, 不下载/解析 PDF, 也不做
财务数据抽取 —— 报告内容以交易所/监管机构原文为准; 结果进程内缓存 6h。

数据源:
  - A股/北交所: akshare 巨潮资讯公告检索 (keyword=报告), 只保留定期报告
    (年度/半年度/季度), 去掉摘要、英文版与 H 股重复公告;
  - 港股: HKEXnews 标题检索 (财务报表/ESG 资料分类), 只保留年报/中期报告;
  - 澳股: ASX 公司公告检索 (按年), 只保留年报/半年报/季报及对应业绩公告;
  - 英股: Companies House 公开网站 (公司搜索 + filing history), 取 AA 年报/
    中期账目;
  - 日股: Yahoo!ファイナンス 適時開示 (TDnet PDF 镜像), 取決算短信
    (年度/中期/季度);
  - 韩股: DART 公示检索 (사업/반기/분기보고서), 无需 API Key;
  - 美股: SEC EDGAR submissions API (``data.sec.gov``, CIK 由官方
    company_tickers.json 映射), 过滤
    10-K/10-Q/20-F/40-F/6-K/8-K 等定期与重大事项披露。

crypto/指数及其它市场没有统一的公开财报库; 页面会提示当前覆盖范围。
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict

from . import cache as cache_mod
from . import filings_markets
from .filings_parse import (  # noqa: F401 - 兼容再导出
    Filing,
    _clean_text,
    _finalize,
    _finalize_same_day,
    _hk_date,
    _iso_date,
)
from .filings_markets import (  # noqa: F401 - 兼容再导出
    _au_filings,
    _au_filings_from_html,
    _au_kind,
    _cn_filings,
    _cn_filings_from_df,
    _cn_kind,
    _get,
    _hk_filings,
    _hk_filings_from_items,
    _hk_kind,
    _jp_filings,
    _jp_filings_from_html,
    _kr_filings,
    _kr_filings_from_html,
    _post,
    _uk_company_name,
    _uk_company_number,
    _uk_filings,
    _uk_filings_from_html,
    _uk_norm_name,
    _us_filings,
    _us_filings_from_rows,
)
from .symbols import Market, normalize, parse

_TTL = 21600  # 秒 (6h): 财报属低频披露数据
_LIMIT_PER_KIND = 2  # 每种报告最多保留最近两期 (本期 + 上期)

#: 已接入财报元数据的市场: A股/北交所、港股、美股、澳股、英股、日股、韩股
FILINGS_SUPPORTED: frozenset[Market] = frozenset(
    {
        Market.CN,
        Market.BJ,
        Market.HK,
        Market.US,
        Market.AU,
        Market.GB,
        Market.JP,
        Market.KR,
    }
)

#: 项目支持行情、但暂未接入财报链接的市场 → 原因 (页面直接展示, 避免误解)
FILINGS_UNAVAILABLE: dict[Market, str] = {
    Market.DE: "德国联邦公报 Bundesanzeiger 无公开 API",
    Market.NL: "荷兰 AFM 无公开财报检索 API",
    Market.CA: "加拿大 SEDAR+ 无公开 API 且有反爬",
    Market.SG: "SGX 公告 API 有访问限制",
    Market.BR: "巴西 CVM 无公开披露 API",
}


_cache_lock = threading.Lock()
# (抓取时间, 结果, 抓取时的 limit): limit 用于避免「先取 2 条、后要 6 条」
# 时把 2 条当成完整缓存。磁盘缓存 json_cache 里存同样结构。
_cache: dict[str, tuple[float, tuple["Filing", ...], int]] = {}
_KEY = "filings:"


# ---------- 对外入口 ----------


def filings_supported(symbol: str) -> bool:
    """该代码是否支持最新财报链接 (A股/北交所/港股/美股/澳股/英股/日股/韩股)."""
    try:
        return parse(normalize(symbol)).market in FILINGS_SUPPORTED
    except ValueError:
        return False


def unavailable_reason(symbol: str) -> str | None:
    """未接入市场的原因; 已支持/无法识别返回 None."""
    try:
        p = parse(normalize(symbol))
    except ValueError:
        return None
    if p.market in FILINGS_SUPPORTED:
        return None
    return FILINGS_UNAVAILABLE.get(p.market, "该市场暂无公开财报链接源")


def _payload(items: list[Filing], limit: int) -> dict:
    """财报列表 → json_cache 载荷 (带上本次 limit, 供更大 limit 判未命中)."""
    return {"limit": int(limit), "items": [asdict(f) for f in items]}


def _from_payload(payload: object, limit: int) -> list[Filing] | None:
    """json_cache 载荷 → Filing 列表; 缓存结果不足 limit 时返回 None."""
    if not isinstance(payload, dict):
        return None
    try:
        cached_limit = int(payload.get("limit") or 0)
    except (TypeError, ValueError):
        return None
    if cached_limit < limit:
        # 之前只缓存了更少的条数: 本次需要更多, 按未命中重新取。
        return None
    out: list[Filing] = []
    for row in payload.get("items") or []:
        if not isinstance(row, dict):
            return None
        data = {
            key: str(row.get(key) or "")
            for key in ("title", "date", "url", "kind", "source")
        }
        out.append(Filing(**data))
    return out[:limit]


def fetch_filings(
    symbol: str, *, limit: int = 6, refresh: bool = False
) -> list[Filing]:
    """取单只股票最近的财报链接 (内存 + 磁盘缓存 6h; refresh=True 穿透)."""
    p = parse(normalize(symbol))
    if p.market not in FILINGS_SUPPORTED:
        raise ValueError(
            f"{p.yahoo}: 最新财报链接仅支持 A股/北交所、港股、美股、澳股、"
            "英股、日股、韩股"
        )

    now = time.time()
    if not refresh:
        with _cache_lock:
            hit = _cache.get(p.yahoo)
        if hit is not None and now - hit[0] < _TTL and hit[2] >= limit:
            return list(hit[1])[:limit]

        cached = _from_payload(
            cache_mod.get_json_cached(f"{_KEY}{p.yahoo}", ttl=_TTL), limit
        )
        if cached is not None:
            with _cache_lock:
                _cache[p.yahoo] = (now, tuple(cached), limit)
            return cached

    if p.market in (Market.CN, Market.BJ):
        items = filings_markets._cn_filings(
            p.ak_code or p.yahoo.split(".")[0], limit
        )
    elif p.market is Market.HK:
        items = filings_markets._hk_filings(p.ak_code or p.yahoo, limit)
    elif p.market is Market.AU:
        items = filings_markets._au_filings(p.yahoo.split(".")[0], limit)
    elif p.market is Market.GB:
        items = filings_markets._uk_filings(p.yahoo, limit)
    elif p.market is Market.JP:
        items = filings_markets._jp_filings(p.yahoo.split(".")[0], limit)
    elif p.market is Market.KR:
        items = filings_markets._kr_filings(p.yahoo.split(".")[0], limit)
    else:
        items = filings_markets._us_filings(p.yahoo, limit)

    with _cache_lock:
        _cache[p.yahoo] = (now, tuple(items), limit)
    cache_mod.set_json_cached(f"{_KEY}{p.yahoo}", _payload(list(items), limit))
    return items


def clear_cache() -> None:
    """清空财报链接的进程内缓存 (手动刷新/测试)."""
    with _cache_lock:
        _cache.clear()
