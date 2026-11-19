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
  - 美股: OpenBB ``equity.fundamental.filings(provider="sec")``, 过滤
    10-K/10-Q/20-F/40-F/6-K/8-K 等定期与重大事项披露。

crypto/指数及其它市场没有统一的公开财报库; 页面会提示当前覆盖范围。
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from dataclasses import asdict, dataclass

from . import cache as cache_mod
from .deps import openbb as _obb
from .deps import requests as _requests
from .deps import yfinance as _yf
from .symbols import Market, normalize, parse

_TTL = 21600  # 秒 (6h): 财报属低频披露数据
_LIMIT_PER_KIND = 2  # 每种报告最多保留最近两期 (本期 + 上期)
_CN_TIMEOUT = 20.0
_US_TIMEOUT = 25.0
_HTTP_TIMEOUT = 15.0

_HKEX_BASE = "https://www1.hkexnews.hk"
_ASX_BASE = "https://www.asx.com.au"
_UK_BASE = "https://find-and-update.company-information.service.gov.uk"
_JP_BASE = "https://finance.yahoo.co.jp"
_DART_BASE = "https://dart.fss.or.kr"
_UA = "Mozilla/5.0 (compatible; madeleine-tracker)"

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

_CN_EXCLUDE = re.compile(r"摘要|英文|公告")
_CN_INTERIM = re.compile(r"半年度报告|中期报告")
_CN_ANNUAL = re.compile(r"年度报告|年报")
_CN_QUARTER = re.compile(r"季度报告|一季报|三季报")

_HK_ESG = re.compile(r"環境、社會及管治|环境、社会及管治|ESG", re.IGNORECASE)
_HK_ANNUAL = re.compile(r"年報|年报")
_HK_INTERIM = re.compile(r"中期|半年度|interim", re.IGNORECASE)
_HK_QUARTER = re.compile(r"季度|quarter", re.IGNORECASE)

# ASX 公告标题 (英文): 只保留正式定期报告/业绩公告, 过滤演示材料、会议通知、
# 回购/分红、季度产量/销售/交易更新等运营公告。
_AU_EXCLUDE = re.compile(
    r"presentation|transcript|media release|news release|briefing|notice of|"
    r"advance notice|annual general meeting|\bagm\b|appendix 3|appendix 4g|"
    r"pillar 3|buy-?back|dividend|distribution|cessation|"
    r"quotation of securities|substantial holding|director|"
    r"corporate governance|email to shareholders|management discussion|"
    r"summary|supplementary disclosure|production report|sales results|"
    r"trading update|performance update|operational|items impacting|"
    r"us annual report|form 20-f|form 6-k",
    re.IGNORECASE,
)
_AU_ANNUAL = re.compile(
    r"annual (?:financial )?report|appendix 4e|preliminary final report|"
    r"statutory accounts for (?:the )?(?:full )?year|"
    r"statutory accounts for fy\d*|full[- ]?year financial report",
    re.IGNORECASE,
)
_AU_INTERIM = re.compile(
    r"half[- ]?year(?:ly)? report|interim (?:financial )?report|appendix 4d|"
    r"statutory accounts for the half[- ]?year|"
    r"half[- ]?year financial report|consolidated half[- ]?year financial report",
    re.IGNORECASE,
)
_AU_QUARTER = re.compile(
    r"appendix 4c|appendix 5b|quarterly (?:cash ?flow|financial) report|"
    r"quarterly report",
    re.IGNORECASE,
)
_AU_ANNUAL_RESULT = re.compile(
    r"full[- ]?year (?:financial )?results?|fy\d{2,4} (?:financial )?results?",
    re.IGNORECASE,
)
_AU_INTERIM_RESULT = re.compile(
    r"half[- ]?year (?:financial )?results?|half[- ]?yearly results?|"
    r"interim (?:financial )?results?|financial results for half[- ]?year|"
    r"results for half[- ]?year",
    re.IGNORECASE,
)

# 日本 TDnet 決算短信: 只保留日文标题 (Yahoo 同时挂英文翻译), 过滤更正/说明材料.
_JP_EXCLUDE = re.compile(
    r"訂正|補足資料|説明会|質疑応答|プレゼンテーション|業績予想|配当|"
    r"自己株式|株式分割|組織改正|人事"
)
_JP_Q1 = re.compile(r"第[1１一]四半期")
_JP_Q2 = re.compile(r"第[2２二]四半期|中間")
_JP_Q3 = re.compile(r"第[3３三]四半期")

# 英国 Companies House: 只取年报 (AA)
_UK_AA = "AA"
_UK_INTERIM = re.compile(r"interim accounts", re.IGNORECASE)

_US_FORMS = {
    "10-K": "年度报告 (10-K)",
    "10-K/A": "年度报告修订 (10-K/A)",
    "10-Q": "季度报告 (10-Q)",
    "10-Q/A": "季度报告修订 (10-Q/A)",
    "20-F": "年度报告 (20-F)",
    "20-F/A": "年度报告修订 (20-F/A)",
    "40-F": "年度报告 (40-F)",
    "6-K": "临时报告 (6-K)",
    "8-K": "重大事项 (8-K)",
    "DEF 14A": "股东会委托书 (DEF 14A)",
}

_cache_lock = threading.Lock()
# (抓取时间, 结果, 抓取时的 limit): limit 用于避免「先取 2 条、后要 6 条」
# 时把 2 条当成完整缓存。磁盘缓存 json_cache 里存同样结构。
_cache: dict[str, tuple[float, tuple["Filing", ...], int]] = {}
_KEY = "filings:"


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


# ---------- A股 / 北交所: 巨潮资讯 ----------


def _cn_kind(title: str) -> str:
    if _CN_INTERIM.search(title):
        return "半年度报告"
    if _CN_ANNUAL.search(title):
        return "年度报告"
    if _CN_QUARTER.search(title):
        return "季度报告"
    return ""


def _cn_filings_from_df(df, limit: int = 6) -> list[Filing]:
    """巨潮公告表 → Filing 列表 (纯函数, 便于离线测试)."""
    if df is None or df.empty:
        return []
    cols = {str(c).strip(): c for c in df.columns}
    title_col = cols.get("公告标题")
    date_col = cols.get("公告时间")
    url_col = cols.get("公告链接")
    if not (title_col and date_col and url_col):
        return []

    out: list[Filing] = []
    for _, row in df.iterrows():
        title = _clean_text(row[title_col])
        if not title or _CN_EXCLUDE.search(title):
            continue
        kind = _cn_kind(title)
        if not kind:
            continue
        url = str(row[url_col] or "").strip()
        if not url:
            continue
        if url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        day = _iso_date(row[date_col])
        if not day:
            continue
        out.append(Filing(title, day, url, kind, "巨潮资讯"))
    return _finalize(out, limit)


def _cn_filings(code: str, limit: int) -> list[Filing]:
    import datetime as _dt

    from .ah_premium import _ak
    from .util import with_timeout

    end = _dt.date.today()
    start = end - _dt.timedelta(days=1100)  # 约三年, 覆盖本期与上期报告
    df = with_timeout(
        _ak().stock_zh_a_disclosure_report_cninfo,
        _CN_TIMEOUT,
        symbol=code,
        market="沪深京",
        keyword="报告",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    return _cn_filings_from_df(df, limit)


# ---------- 港股: HKEXnews ----------


def _hk_kind(text: str) -> str:
    if _HK_ANNUAL.search(text):
        return "年度报告"
    if _HK_INTERIM.search(text):
        return "中期报告"
    if _HK_QUARTER.search(text):
        return "季度报告"
    return ""


def _hk_filings_from_items(items, limit: int = 6) -> list[Filing]:
    """HKEXnews 标题检索结果 → Filing 列表 (纯函数, 便于离线测试)."""
    out: list[Filing] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("TITLE"))
        if not title or _HK_ESG.search(title):
            continue
        category = _clean_text(item.get("LONG_TEXT"))
        kind = _hk_kind(f"{category} {title}")
        if not kind:
            continue
        link = str(item.get("FILE_LINK") or "").strip()
        if not link:
            continue
        if link.startswith("/"):
            link = _HKEX_BASE + link
        day = _hk_date(item.get("DATE_TIME"))
        if not day:
            continue
        out.append(Filing(title, day, link, kind, "香港交易所"))
    return _finalize(out, limit)


def _get(url: str, params: dict | None = None):
    resp = _requests().get(
        url,
        params=params,
        timeout=_HTTP_TIMEOUT,
        headers={"User-Agent": _UA},
    )
    resp.raise_for_status()
    return resp


def _jsonp_payload(text: str) -> dict:
    """解析 HKEXnews 的 ``callback({...})`` 或纯 JSON 响应."""
    raw = str(text or "").strip()
    start = raw.find("(")
    end = raw.rfind(")")
    if start >= 0 and end > start:
        raw = raw[start + 1 : end]
    return json.loads(raw)


def _hk_stock_id(code: str) -> str | None:
    resp = _get(
        f"{_HKEX_BASE}/search/prefix.do",
        {
            "callback": "callback",
            "lang": "ZH",
            "type": "A",
            "name": code,
            "market": "SEHK",
        },
    )
    data = _jsonp_payload(resp.text)
    infos = data.get("stockInfo") or []
    if not infos:
        return None
    target = str(code).lstrip("0").zfill(5)
    for info in infos:
        if str(info.get("code") or "") == target:
            return str(info.get("stockId") or "") or None
    stock_id = infos[0].get("stockId")
    return str(stock_id) if stock_id else None


def _hk_filings(code: str, limit: int) -> list[Filing]:
    import datetime as _dt

    stock_id = _hk_stock_id(code)
    if not stock_id:
        return []
    end = _dt.date.today()
    start = end - _dt.timedelta(days=1100)
    resp = _get(
        f"{_HKEX_BASE}/search/titleSearchServlet.do",
        {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "-1",
            "fromDate": start.strftime("%Y%m%d"),
            "toDate": end.strftime("%Y%m%d"),
            "title": "",
            "searchType": "0",
            # 40000 = 財務報表/環境、社會及管治資料 (年報、中期報告、ESG)
            "t1code": "40000",
            "t2Gcode": "-2",
            "t2code": "-2",
            "rowRange": "100",
            "lang": "ZH",
        },
    )
    payload = resp.json()
    raw = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            items = []
    else:
        items = raw or []
    return _hk_filings_from_items(items, limit)


# ---------- 澳股: ASX 公告检索 ----------
# ASX 没有公开 JSON API, 这里解析公告检索页 HTML; 页面结构变化由
# tests/test_provider_contract_live.py 的 live 用例提前发现。


def _au_kind(title: str) -> str:
    if _AU_EXCLUDE.search(title):
        return ""
    if _AU_ANNUAL.search(title):
        return "年度报告"
    if _AU_INTERIM.search(title):
        return "半年度报告"
    if _AU_QUARTER.search(title):
        return "季度报告"
    if _AU_ANNUAL_RESULT.search(title):
        return "年度业绩"
    if _AU_INTERIM_RESULT.search(title):
        return "半年度业绩"
    return ""


def _au_filings_from_html(text: str, limit: int = 6) -> list[Filing]:
    """ASX 年度公告检索 HTML → Filing 列表 (纯函数, 便于离线测试)."""
    out: list[Filing] = []
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", text or "", re.S | re.I):
        row = row_match.group(1)
        link_match = re.search(
            r'<a[^>]*href="([^"]*displayAnnouncement[^"]*)"[^>]*>(.*?)</a>',
            row,
            re.S | re.I,
        )
        if link_match is None:
            continue
        day = _iso_date(row)
        if not day:
            # ASX 日期是 DD/MM/YYYY, 与 _iso_date 的 YYYY-MM-DD 口径不同
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", row)
            if not m:
                continue
            d, mo, y = m.groups()
            day = f"{y}-{int(mo):02d}-{int(d):02d}"
        title = _clean_text(link_match.group(2).split("<img", 1)[0])
        if not title:
            continue
        kind = _au_kind(title)
        if not kind:
            continue
        link = html.unescape(link_match.group(1)).strip()
        if not link:
            continue
        if link.startswith("/"):
            link = _ASX_BASE + link
        out.append(Filing(title, day, link, kind, "ASX"))
    return _finalize_same_day(out, limit)


def _fetch_concurrent(items, max_workers: int, worker) -> list[Filing]:
    """并发执行 worker(item); 全部失败时抛第一个错误 (不缓存空结果).

    单页/单年/单类公告失败只丢自己那一份, 不拖垮其它并发项。
    """
    from concurrent.futures import ThreadPoolExecutor

    errors: list[BaseException] = []

    def _one(item) -> list[Filing]:
        try:
            return worker(item)
        except Exception as e:  # noqa: BLE001 - 单任务失败不影响其它任务
            errors.append(e)
            return []

    out: list[Filing] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for items_ in pool.map(_one, items):
            out.extend(items_)
    if not out and errors:
        raise errors[0]
    return out


def _au_filings(code: str, limit: int) -> list[Filing]:
    import datetime as _dt

    year = _dt.date.today().year

    def _year(y: int) -> list[Filing]:
        resp = _get(
            f"{_ASX_BASE}/asx/v2/statistics/announcements.do",
            {
                "by": "asxCode",
                "asxCode": code,
                "timeframe": "Y",
                "year": str(y),
            },
        )
        return _au_filings_from_html(resp.text, limit)

    return _finalize_same_day(
        _fetch_concurrent((year, year - 1), 2, _year), limit
    )


# ---------- 英股: Companies House ----------


_UK_PLC = re.compile(r"\bp\W*l\W*c\b", re.IGNORECASE)
_UK_SUFFIX = re.compile(
    r"\b(?:p\W*l\W*c|plc|limited|ltd|group|holdings?|company|co)\b", re.IGNORECASE
)


def _uk_norm_name(value: object) -> str:
    text = _clean_text(value).lower()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    text = _UK_SUFFIX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _uk_search_companies(query: str) -> list[tuple[str, str]]:
    resp = _get(f"{_UK_BASE}/search/companies", {"q": query})
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'href="/company/([A-Z0-9]+)"[^>]*>(.*?)</a>',
        resp.text,
        re.S | re.I,
    ):
        number = m.group(1).upper()
        # FC/BR 是海外公司/分支机构, 不是英国本地申报主体
        if number.startswith(("FC", "BR")) or number in seen:
            continue
        title = _clean_text(m.group(2))
        if not title:
            continue
        seen.add(number)
        out.append((number, title))
    return out


def _uk_company_name(symbol: str) -> str | None:
    """用 Yahoo 搜索/行情链路拿公司法定名, 用于 Companies House 搜索."""
    # 先用 Yahoo 搜索 (约 1-2s), 避免行情链路在冷启动/负缓存时拖到 ~8s
    try:
        resp = _get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            {"q": symbol, "quotesCount": 10, "newsCount": 0},
        )
        for row in resp.json().get("quotes") or []:
            if str(row.get("symbol") or "").upper() == symbol.upper():
                name = row.get("longname") or row.get("shortname")
                if name:
                    return str(name).strip()
    except Exception:  # noqa: BLE001 - 走行情链路兜底
        pass
    # yfinance Ticker.info (与 OpenBB 同一上游, 通常 0.5-2s); 包一层硬超时防挂起
    try:
        from .util import with_timeout

        def _load_info():
            return _yf().Ticker(symbol).info

        info = with_timeout(_load_info, 10.0) or {}
        name = info.get("longName") or info.get("shortName")
        if name:
            return str(name).strip()
    except Exception:  # noqa: BLE001 - 继续走项目行情链路
        pass
    try:
        from . import prices
        from .util import with_timeout

        quote = with_timeout(prices.get_quote, 8.0, symbol)
        if quote.name:
            return quote.name.strip()
    except Exception:  # noqa: BLE001 - 名称拿不到时由调用方明确报错
        pass
    return None


def _uk_company_number(symbol: str) -> str | None:
    """ticker → Companies House company number (公开网站搜索, 无需 API Key)."""
    import difflib

    name = _uk_company_name(symbol)
    if not name:
        # 不把「名称取数失败」当成「公司没有财报」: 抛错让页面提示并允许重试,
        # 避免 6h 空结果缓存。
        raise RuntimeError(f"{symbol}: 无法获取公司名称, 暂不能检索 Companies House")
    queries = [q for q in (name, symbol.split(".")[0]) if q]
    require_plc = bool(_UK_PLC.search(name))
    for query in queries:
        results = _uk_search_companies(query)
        if not results:
            continue
        if require_plc:
            # 报价名是 plc 时不要把同名子公司 (LIMITED/LTD) 当成上市主体
            results = [
                (number, title)
                for number, title in results
                if _UK_PLC.search(title)
            ]
            if not results:
                continue
        target = _uk_norm_name(name)
        for number, title in results:
            if _uk_norm_name(title) == target:
                return number
        scored = [
            (
                difflib.SequenceMatcher(
                    None, target, _uk_norm_name(title)
                ).ratio(),
                number,
            )
            for number, title in results
        ]
        ratio, number = max(scored)
        if ratio >= 0.82:
            return number
    return None


def _uk_date(value: object) -> str:
    """Companies House 的 20 May 2026 → 2026-05-20."""
    import datetime as _dt

    try:
        return _dt.datetime.strptime(
            _clean_text(value), "%d %b %Y"
        ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _uk_filings_from_html(text: str, limit: int = 6) -> list[Filing]:
    """Companies House filing history HTML → AA 年报列表."""
    out: list[Filing] = []
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", text or "", re.S | re.I):
        row = row_match.group(1)
        if "filing-history/" not in row:
            continue
        type_match = re.search(
            r'<td class="filing-type[^"]*"[^>]*>\s*([^<]+?)\s*</td>',
            row,
            re.S | re.I,
        )
        if type_match is None or _clean_text(type_match.group(1)) != _UK_AA:
            continue
        desc_match = re.search(
            r"<td[^>]*>\s*<strong>(.*?)</strong>(.*?)</td>",
            row,
            re.S | re.I,
        )
        if desc_match is not None:
            description = _clean_text(
                f"{desc_match.group(1)} {desc_match.group(2)}"
            )
        else:
            description = ""
        link_match = re.search(
            r'href="([^"]*filing-history/[^"]*document[^"]*)"',
            row,
            re.S | re.I,
        )
        if link_match is None:
            continue
        day_match = re.search(r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", row)
        if day_match is None:
            continue
        day = _uk_date(day_match.group(1))
        if not day:
            continue
        link = html.unescape(link_match.group(1)).strip()
        if link.startswith("/"):
            link = _UK_BASE + link
        title = description or "Annual accounts"
        kind = "半年度报告" if _UK_INTERIM.search(title) else "年度报告"
        out.append(Filing(title, day, link, kind, "Companies House"))
    return _finalize_same_day(out, limit)


def _uk_filings(symbol: str, limit: int) -> list[Filing]:
    number = _uk_company_number(symbol)
    if not number:
        return []

    def _page(page: int) -> list[Filing]:
        resp = _get(
            f"{_UK_BASE}/company/{number}/filing-history",
            {"page": page},
        )
        return _uk_filings_from_html(resp.text, limit)

    return _finalize_same_day(_fetch_concurrent((1, 2, 3, 4), 4, _page), limit)


# ---------- 日股: Yahoo!ファイナンス / TDnet ----------


def _jp_filings_from_html(text: str, limit: int = 6) -> list[Filing]:
    """Yahoo!ファイナンス 適時開示 HTML → TDnet 決算短信列表."""
    out: list[Filing] = []
    for article in re.findall(r"<article[^>]*>(.*?)</article>", text or "", re.S):
        h3 = re.search(r"<h3[^>]*>(.*?)</h3>", article, re.S)
        if h3 is None:
            continue
        title = _clean_text(h3.group(1))
        if not title or "決算短信" not in title or _JP_EXCLUDE.search(title):
            continue
        link_match = re.search(r'<a[^>]+href="([^"]+)"', article, re.S)
        if link_match is None:
            continue
        link = html.unescape(link_match.group(1)).strip()
        if link.startswith("/"):
            link = _JP_BASE + link
        date_match = re.search(r'dateTime="([^"]+)"', article)
        day = _iso_date(date_match.group(1)) if date_match else ""
        if not day:
            continue
        if _JP_Q2.search(title):
            kind = "中期报告"
        elif _JP_Q1.search(title) or _JP_Q3.search(title):
            kind = "季度报告"
        else:
            kind = "年度报告"
        out.append(Filing(title, day, link, kind, "TDnet"))
    return _finalize_same_day(out, limit)


def _jp_filings(code: str, limit: int) -> list[Filing]:
    def _page(page: int) -> list[Filing]:
        resp = _get(
            f"{_JP_BASE}/quote/{code}.T/disclosure",
            {"page": page},
        )
        return _jp_filings_from_html(resp.text, limit)

    return _finalize_same_day(_fetch_concurrent((1, 2, 3, 4), 4, _page), limit)


# ---------- 韩股: DART 公示检索 ----------


def _kr_date(value: object) -> str:
    """DART 的 2026.03.10 → 2026-03-10."""
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", str(value or ""))
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _kr_filings_from_html(
    text: str, kind: str, limit: int = 6
) -> list[Filing]:
    """DART 공시서류검색 결과 HTML → Filing 列表."""
    out: list[Filing] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text or "", re.S | re.I):
        cells = [
            _clean_text(re.sub(r"<[^>]+>", " ", cell))
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        ]
        if len(cells) < 3:
            continue
        link_match = re.search(
            r'href="(/dsaf001/main\.do\?rcpNo=(\d+))"', row, re.S | re.I
        )
        if link_match is None:
            continue
        day = _kr_date(cells[-2])
        if not day:
            continue
        title = cells[2]
        if not title:
            continue
        url = f"{_DART_BASE}{html.unescape(link_match.group(1))}"
        out.append(Filing(title, day, url, kind, "DART"))
    return _finalize_same_day(out, limit)


def _post(url: str, data: dict, *, referer: str = "") -> object:
    headers = {"User-Agent": _UA}
    if referer:
        headers["Referer"] = referer
    resp = _requests().post(
        url, data=data, timeout=_HTTP_TIMEOUT, headers=headers
    )
    resp.raise_for_status()
    return resp


def _kr_filings(code: str, limit: int) -> list[Filing]:
    import datetime as _dt

    end = _dt.date.today()
    start = end - _dt.timedelta(days=1100)
    reports = (
        ("사업보고서", "年度报告"),
        ("반기보고서", "半年度报告"),
        ("분기보고서", "季度报告"),
    )

    def _report(report: tuple[str, str]) -> list[Filing]:
        report_name, kind = report
        data = {
            "currentPage": 1,
            "maxResults": 100,
            "maxLinks": 10,
            "sort": "",
            "series": "",
            "option": "corp",
            "textCrpCik": "",
            "lateKeyword": "",
            "keyword": "",
            "reportNamePopYn": "",
            "textkeyword": "",
            "businessCode": "all",
            "autoSearch": "N",
            "autoSearchCorp": "Y",
            "textCrpNm": code,
            "textCrpNm2": code,
            "reportName": report_name,
            "tocSrch": "",
            "textPresenterNm": "",
            "startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
            "finalReport": "recent",
            "businessNm": "전체",
            "reportName2": report_name,
            "tocSrch2": "",
        }
        resp = _post(
            f"{_DART_BASE}/dsab007/detailSearch.ax",
            data,
            referer=f"{_DART_BASE}/dsab007/main.do",
        )
        return _kr_filings_from_html(resp.text, kind, limit)

    return _finalize_same_day(_fetch_concurrent(reports, 3, _report), limit)


# ---------- 美股: SEC EDGAR ----------


def _us_filings_from_rows(rows, limit: int = 6) -> list[Filing]:
    """SEC filings 行 → Filing 列表 (纯函数, 便于离线测试)."""
    out: list[Filing] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        form = str(row.get("report_type") or "").strip().upper()
        kind = _US_FORMS.get(form)
        if kind is None:
            continue
        url = str(row.get("report_url") or "").strip()
        if not url:
            continue
        day = _iso_date(row.get("filing_date") or row.get("report_date"))
        if not day:
            continue
        desc = _clean_text(row.get("primary_doc_description"))
        title = form if not desc or desc.upper() == form else f"{form} · {desc}"
        out.append(Filing(title, day, url, kind, "SEC EDGAR"))
    return _finalize(out, limit)


def _us_filings(symbol: str, limit: int) -> list[Filing]:
    from .util import with_timeout

    def _load():
        res = _obb().equity.fundamental.filings(
            symbol=symbol,
            provider="sec",
            limit=200,
        )
        return [item.model_dump() for item in res.results]

    return _us_filings_from_rows(with_timeout(_load, _US_TIMEOUT), limit)


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
        items = _cn_filings(p.ak_code or p.yahoo.split(".")[0], limit)
    elif p.market is Market.HK:
        items = _hk_filings(p.ak_code or p.yahoo, limit)
    elif p.market is Market.AU:
        items = _au_filings(p.yahoo.split(".")[0], limit)
    elif p.market is Market.GB:
        items = _uk_filings(p.yahoo, limit)
    elif p.market is Market.JP:
        items = _jp_filings(p.yahoo.split(".")[0], limit)
    elif p.market is Market.KR:
        items = _kr_filings(p.yahoo.split(".")[0], limit)
    else:
        items = _us_filings(p.yahoo, limit)

    with _cache_lock:
        _cache[p.yahoo] = (now, tuple(items), limit)
    cache_mod.set_json_cached(f"{_KEY}{p.yahoo}", _payload(list(items), limit))
    return items


def clear_cache() -> None:
    """清空财报链接的进程内缓存 (手动刷新/测试)."""
    with _cache_lock:
        _cache.clear()
