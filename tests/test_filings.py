"""最新财报链接: 巨潮/HKEX/SEC 解析与缓存 (离线, 不联网)."""

from __future__ import annotations

import pandas as pd
import pytest

import tracker.filings as filings
import tracker.filings_markets as filings_markets


@pytest.fixture(autouse=True)
def _clear_cache():
    filings.clear_cache()
    yield
    filings.clear_cache()


def _cn_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "公告标题": "工商银行2026年第三季度<em>报告</em>",
                "公告时间": "2026-10-31",
                "公告链接": "http://www.cninfo.com.cn/new/disclosure/detail?id=3",
            },
            {
                "公告标题": "工商银行2026年第三季度报告摘要",
                "公告时间": "2026-10-31",
                "公告链接": "http://www.cninfo.com.cn/new/disclosure/detail?id=3s",
            },
            {
                "公告标题": "工商银行H股公告-2026年第三季度报告",
                "公告时间": "2026-10-31",
                "公告链接": "http://www.cninfo.com.cn/new/disclosure/detail?id=3h",
            },
            {
                "公告标题": "工商银行2026半年度报告",
                "公告时间": "2026-08-29",
                "公告链接": "http://www.cninfo.com.cn/new/disclosure/detail?id=2",
            },
            {
                "公告标题": "公司2025年年度报告业绩说明会预告公告",
                "公告时间": "2026-04-28",
                "公告链接": "http://www.cninfo.com.cn/new/disclosure/detail?id=notice",
            },
            {
                "公告标题": "工商银行2025年年度报告",
                "公告时间": "2026-03-28",
                "公告链接": "http://www.cninfo.com.cn/new/disclosure/detail?id=1",
            },
            {
                "公告标题": "工商银行2025年度董事会工作报告",
                "公告时间": "2026-03-31",
                "公告链接": "http://www.cninfo.com.cn/new/disclosure/detail?id=noise",
            },
        ]
    )


def test_cn_filings_filter_dedupe_and_upgrade_https():
    out = filings._cn_filings_from_df(_cn_df(), 6)

    assert [f.kind for f in out] == [
        "季度报告",
        "半年度报告",
        "年度报告",
    ]
    assert out[0].date == "2026-10-31"
    assert all("摘要" not in f.title for f in out)
    assert all("公告" not in f.title for f in out)
    assert all("H股公告" not in f.title for f in out)
    assert all("董事会" not in f.title for f in out)
    assert all(f.url.startswith("https://") for f in out)
    assert all(f.source == "巨潮资讯" for f in out)


def test_cn_filings_keep_two_latest_per_kind():
    rows = [
        {
            "公告标题": f"公司{year}年年度报告",
            "公告时间": f"{year + 1}-03-28",
            "公告链接": f"http://www.cninfo.com.cn/a/{year}",
        }
        for year in (2025, 2024, 2023)
    ]
    out = filings._cn_filings_from_df(pd.DataFrame(rows), 6)

    assert [f.date for f in out] == ["2026-03-28", "2025-03-28"]


def test_hk_filings_parse_entities_filter_esg_and_sort():
    items = [
        {
            "TITLE": "2025 年報",
            "LONG_TEXT": "財務報表&#x2f;環境、社會及管治資料 - [年報]",
            "DATE_TIME": "09/04/2026 17:21",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0409/2026040901232_c.pdf",
        },
        {
            "TITLE": "中期報告 2026",
            "LONG_TEXT": "財務報表&#x2f;環境、社會及管治資料 - [中期&#x2f;半年度報告]",
            "DATE_TIME": "25/08/2026 16:53",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0825/2026082500557_c.pdf",
        },
        {
            "TITLE": "2025年環境、社會及管治報告",
            "LONG_TEXT": "財務報表&... - [環境、社會及管治資料&#x2f;報告]",
            "DATE_TIME": "09/04/2026 17:26",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0409/esg.pdf",
        },
    ]
    out = filings._hk_filings_from_items(items, 6)

    assert [f.kind for f in out] == ["中期报告", "年度报告"]
    assert out[0].date == "2026-08-25"
    assert out[0].url.startswith("https://www1.hkexnews.hk/listedco/")
    assert out[1].title == "2025 年報"
    assert all("環境" not in f.title for f in out)


def _au_row(day: str, title: str, ids: str) -> str:
    return (
        "<tr><td>"
        f"{day}<br><span class=\"dates-time\">9:00 am</span></td>"
        "<td class=\"pricesens\"></td><td>"
        "<a target=\"_blank\" href=\"/asx/v2/statistics/"
        f"displayAnnouncement.do?display=pdf&amp;idsId={ids}\">"
        f"{title}<br><img src=\"/asx/v2/markets/image/pdf_icon.png\">"
        "<span class=\"page\">10 pages</span></a></td></tr>"
    )


_AU_HTML = "<table>" + "".join(
    [
        _au_row("12/08/2026", "2026 Annual Report", "1"),
        _au_row("12/08/2026", "2026 Annual Report Summary", "1s"),
        _au_row("19/08/2025", "2025 Annual Report", "0"),
        _au_row("18/08/2026", "FY26 Full Year Results", "2"),
        _au_row("17/02/2026", "Half Yearly Report and Accounts", "3"),
        _au_row("25/02/2026", "FY26 Half Year Results Announcement", "4"),
        _au_row("20/10/2026", "Appendix 4C - Quarterly Cash Flow Report", "5"),
        _au_row("22/10/2026", "September 2026 Quarterly Production Report", "6"),
        _au_row("05/11/2026", "2026 US Annual Report (Form 20-F)", "7"),
        _au_row("05/11/2026", "Notice of Annual General Meeting", "8"),
        _au_row("16/10/2025", "Items impacting FY25 results", "9"),
        _au_row("05/11/2026", "2026 Half Year Results Presentation", "10"),
        _au_row("07/11/2026", "Advance notice - 2027 Full-year results", "11"),
    ]
) + "</table>"


def test_au_filings_parse_filter_and_dedupe():
    out = filings._au_filings_from_html(_AU_HTML, 6)

    titles = [f.title for f in out]
    assert "2026 Annual Report" in titles
    assert "FY26 Full Year Results" in titles
    assert "Half Yearly Report and Accounts" in titles
    assert "FY26 Half Year Results Announcement" in titles
    assert "Appendix 4C - Quarterly Cash Flow Report" in titles
    assert all("Summary" not in t for t in titles)
    assert all("Production" not in t for t in titles)
    assert all("Form 20-F" not in t for t in titles)
    assert all("Notice" not in t for t in titles)
    assert all("Presentation" not in t for t in titles)
    assert all("Advance notice" not in t for t in titles)
    assert out[0].date == "2026-10-20"
    assert out[0].url.startswith("https://www.asx.com.au/asx/v2/")
    assert all(f.source == "ASX" for f in out)


def test_au_filings_same_kind_same_day_deduped():
    html = "<table>" + "".join(
        [
            _au_row("12/08/2026", "2026 Annual Report", "1"),
            _au_row("12/08/2026", "Rio Tinto releases 2026 Annual Report", "2"),
        ]
    ) + "</table>"
    out = filings._au_filings_from_html(html, 6)
    assert len(out) == 1
    assert out[0].title == "2026 Annual Report"


def test_au_filings_tolerates_one_year_failure(monkeypatch):
    import datetime as dt
    from types import SimpleNamespace

    current = dt.date.today().year

    def fake_get(url, params=None):
        assert params is not None
        if params["year"] == str(current):
            raise RuntimeError("current year timeout")
        return SimpleNamespace(text=_AU_HTML)

    monkeypatch.setattr(filings_markets, "_get", fake_get)
    out = filings._au_filings("CBA", 6)
    assert out


def test_au_filings_raises_when_both_years_fail(monkeypatch):
    def fake_get(url, params=None):
        raise RuntimeError("asx down")

    monkeypatch.setattr(filings_markets, "_get", fake_get)
    with pytest.raises(RuntimeError, match="asx down"):
        filings._au_filings("CBA", 6)


def test_au_kind_recognizes_financial_report_variants():
    assert filings._au_kind("2026 Annual Financial Report") == "年度报告"
    assert filings._au_kind("2026 Interim Financial Report") == "半年度报告"
    assert filings._au_kind("Quarterly Cash Flow Report") == "季度报告"
    assert filings._au_kind("Quarterly Activities Report") == ""
    assert filings._au_kind("2026 Annual Report Presentation") == ""


_UK_HTML = "".join(
    [
        (
            "<tr><td class=\"nowrap\">20 May 2026</td>"
            "<td class=\"filing-type\">AA</td>"
            "<td><strong>Group of companies' accounts</strong>"
            " made up to 31 December 2025</td>"
            "<td><a href=\"/company/00102498/filing-history/abc/document?"
            "format=pdf&amp;download=0\">View PDF</a></td></tr>"
        ),
        (
            "<tr><td class=\"nowrap\">09 Feb 2026</td>"
            "<td class=\"filing-type\">AA</td>"
            "<td><strong>Interim accounts</strong>"
            " made up to 31 December 2025</td>"
            "<td><a href=\"/company/00102498/filing-history/def/document?"
            "format=pdf&amp;download=0\">View PDF</a></td></tr>"
        ),
        (
            "<tr><td class=\"nowrap\">16 May 2026</td>"
            "<td class=\"filing-type\">CS01</td>"
            "<td><strong>Confirmation statement</strong></td>"
            "<td><a href=\"/company/00102498/filing-history/ghi/document?"
            "format=pdf&amp;download=0\">View PDF</a></td></tr>"
        ),
    ]
)


def test_uk_filings_parse_aa_accounts():
    out = filings._uk_filings_from_html(_UK_HTML, 6)

    assert [f.kind for f in out] == ["年度报告", "半年度报告"]
    assert out[0].date == "2026-05-20"
    assert out[0].url.startswith(
        "https://find-and-update.company-information.service.gov.uk/"
    )
    assert all(f.source == "Companies House" for f in out)
    assert all("interim" not in f.title.lower() for f in out[:1])


def test_uk_company_number_prefers_exact_match(monkeypatch):
    monkeypatch.setattr(filings_markets, "_uk_company_name", lambda s: "BP p.l.c.")
    monkeypatch.setattr(
        filings_markets,
        "_uk_search_companies",
        lambda q: [
            ("SC725848", "Aberdeen City Energy Limited"),
            ("00102498", "BP P.L.C."),
        ],
    )
    assert filings._uk_company_number("BP.L") == "00102498"


def test_uk_name_normalization_ignores_legal_suffixes():
    assert filings._uk_norm_name("BP p.l.c.") == "bp"
    assert filings._uk_norm_name("Rio Tinto Group") == filings._uk_norm_name(
        "RIO TINTO PLC"
    )


def test_uk_filings_raises_without_company_name(monkeypatch):
    monkeypatch.setattr(filings_markets, "_uk_company_name", lambda s: None)
    with pytest.raises(RuntimeError, match="无法获取公司名称"):
        filings._uk_filings("BP.L", 6)


_JP_HTML = "".join(
    [
        (
            "<article><a href=\"https://finance-frontend-pc-dist.example/20261105/a.pdf\">"
            "<h3>2027年3月期 第2四半期（中間期）決算短信〔IFRS〕（連結)</h3>"
            "<ul><li><time dateTime=\"2026-11-05T13:55:00+09:00\">11/5</time></li></ul>"
            "</a></article>"
        ),
        (
            "<article><a href=\"/disclosure/20260508/b.pdf\">"
            "<h3>2026年3月期 決算短信〔IFRS〕（連結)</h3>"
            "<ul><li><time dateTime=\"2026-05-08T13:55:00+09:00\">5/8</time></li></ul>"
            "</a></article>"
        ),
        (
            "<article><a href=\"https://example/c.pdf\">"
            "<h3>（訂正）「2026年3月期 決算短信」の一部訂正について</h3>"
            "<ul><li><time dateTime=\"2026-05-15T18:00:00+09:00\">5/15</time></li></ul>"
            "</a></article>"
        ),
        (
            "<article><a href=\"https://example/d.pdf\">"
            "<h3>FY2027 Semi-Annual Consolidated Financial Results (IFRS)</h3>"
            "<ul><li><time dateTime=\"2026-11-05T13:55:00+09:00\">11/5</time></li></ul>"
            "</a></article>"
        ),
    ]
)


def test_jp_filings_parse_tdnet_earnings_reports():
    out = filings._jp_filings_from_html(_JP_HTML, 6)

    assert [f.kind for f in out] == ["中期报告", "年度报告"]
    assert out[0].date == "2026-11-05"
    assert out[0].url.startswith("https://finance-frontend-pc-dist.example/")
    assert out[1].url.startswith("https://finance.yahoo.co.jp/")
    assert all("訂正" not in f.title for f in out)
    assert all("FY2027" not in f.title for f in out)
    assert all(f.source == "TDnet" for f in out)


_KR_HTML = "".join(
    [
        (
            "<tr><td>1</td><td>삼성전자</td>"
            "<td><a href=\"/dsaf001/main.do?rcpNo=20260310002820\">"
            "사업보고서 (2025.12)</a></td><td>삼성전자</td>"
            "<td>2026.03.10</td><td></td></tr>"
        ),
        (
            "<tr><td>2</td><td>삼성전자</td>"
            "<td><a href=\"/dsaf001/main.do?rcpNo=20260814003699\">"
            "반기보고서 (2026.06)</a></td><td>삼성전자</td>"
            "<td>2026.08.14</td><td></td></tr>"
        ),
    ]
)


def test_kr_filings_parse_dart_rows():
    annual = filings._kr_filings_from_html(_KR_HTML, "年度报告", 6)
    interim = filings._kr_filings_from_html(_KR_HTML, "半年度报告", 6)

    assert len(annual) == 2 and len(interim) == 2
    assert annual[0].date == "2026-08-14"
    assert annual[0].url == (
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260814003699"
    )
    assert all(f.kind == "年度报告" for f in annual)
    assert all(f.source == "DART" for f in annual)


def test_us_filings_filter_forms_and_keep_latest_per_form():
    rows = [
        {
            "report_type": "8-K",
            "filing_date": "2026-11-09",
            "report_url": "https://www.sec.gov/a/8k-latest",
            "primary_doc_description": "",
        },
        {
            "report_type": "10-Q",
            "filing_date": "2026-07-31",
            "report_url": "https://www.sec.gov/a/10q-2",
            "primary_doc_description": "",
        },
        {
            "report_type": "10-K",
            "filing_date": "2025-10-31",
            "report_url": "https://www.sec.gov/a/10k",
            "primary_doc_description": "",
        },
        {
            "report_type": "4",
            "filing_date": "2026-11-12",
            "report_url": "https://www.sec.gov/a/form4",
            "primary_doc_description": "FORM 4",
        },
        {
            "report_type": "10-Q",
            "filing_date": "2026-05-01",
            "report_url": "https://www.sec.gov/a/10q-1",
            "primary_doc_description": "",
        },
        {
            "report_type": "10-Q",
            "filing_date": "2026-01-30",
            "report_url": "https://www.sec.gov/a/10q-0",
            "primary_doc_description": "",
        },
    ]
    out = filings._us_filings_from_rows(rows, 6)

    assert [f.kind for f in out] == [
        "重大事项 (8-K)",
        "季度报告 (10-Q)",
        "季度报告 (10-Q)",
        "年度报告 (10-K)",
    ]
    assert all("FORM 4" not in f.title for f in out)
    assert all(f.source == "SEC EDGAR" for f in out)


def test_us_rows_from_submissions_builds_primary_document_url():
    """SEC submissions → 行结构: accession 去横线 + 主文档名拼原文 URL."""
    payload = {
        "filings": {
            "recent": {
                "form": ["10-K", "4"],
                "filingDate": ["2026-11-03", "2026-11-12"],
                "reportDate": ["2026-09-26", "2026-11-11"],
                "accessionNumber": [
                    "0000320193-26-000026",
                    "0001140361-26-045775",
                ],
                "primaryDocument": ["aapl-20260926.htm", "xslF345X06/form4.xml"],
                "primaryDocDescription": ["", "FORM 4"],
            }
        }
    }

    rows = filings_markets._us_rows_from_submissions("320193", payload)
    out = filings._us_filings_from_rows(rows, 6)

    assert len(out) == 1
    assert out[0].kind == "年度报告 (10-K)"
    assert out[0].url == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000026/aapl-20260926.htm"
    )


def test_fetch_dispatch_and_cache(monkeypatch):
    calls: list[tuple[str, int]] = []
    fake = filings.Filing("2025 年报", "2026-03-28", "https://x/1", "年度报告", "测试")

    def fake_cn(code, limit):
        calls.append((code, limit))
        return [fake]

    monkeypatch.setattr(filings_markets, "_cn_filings", fake_cn)
    first = filings.fetch_filings("601398.SS")
    second = filings.fetch_filings("601398.SS")
    assert first == second == [fake]
    assert calls == [("601398", 6)]

    filings.fetch_filings("601398.SS", refresh=True)
    assert calls == [("601398", 6), ("601398", 6)]


def test_fetch_dispatch_hk_and_us(monkeypatch):
    fake = filings.Filing("10-K", "2026-01-01", "https://x", "年度报告", "SEC")
    seen: list[str] = []

    monkeypatch.setattr(
        filings_markets, "_hk_filings", lambda code, limit: seen.append(code) or [fake]
    )
    monkeypatch.setattr(
        filings_markets, "_us_filings", lambda symbol, limit: seen.append(symbol) or [fake]
    )
    monkeypatch.setattr(
        filings_markets, "_au_filings", lambda code, limit: seen.append(code) or [fake]
    )
    monkeypatch.setattr(
        filings_markets, "_uk_filings", lambda symbol, limit: seen.append(symbol) or [fake]
    )
    monkeypatch.setattr(
        filings_markets, "_jp_filings", lambda code, limit: seen.append(code) or [fake]
    )
    monkeypatch.setattr(
        filings_markets, "_kr_filings", lambda code, limit: seen.append(code) or [fake]
    )

    assert filings.fetch_filings("0700.HK") == [fake]
    assert filings.fetch_filings("AAPL") == [fake]
    assert filings.fetch_filings("CBA.AX") == [fake]
    assert filings.fetch_filings("BP.L") == [fake]
    assert filings.fetch_filings("7203.T") == [fake]
    assert filings.fetch_filings("005930.KS") == [fake]
    assert seen == ["00700", "AAPL", "CBA", "BP.L", "7203", "005930"]


def test_filings_supported_and_unsupported():
    assert filings.filings_supported("601398.SS")
    assert filings.filings_supported("430047.BJ")
    assert filings.filings_supported("0700.HK")
    assert filings.filings_supported("AAPL")
    assert filings.filings_supported("CBA.AX")
    assert filings.filings_supported("BP.L")
    assert filings.filings_supported("7203.T")
    assert filings.filings_supported("005930.KS")
    assert not filings.filings_supported("BTC-USD")
    assert not filings.filings_supported("IX.DXY")

    with pytest.raises(ValueError, match="仅支持"):
        filings.fetch_filings("BTC-USD")


def test_every_stock_market_is_classified():
    """新增股票市场时必须显式接入或写明未接入原因, 不能静默当作支持."""
    from tracker.symbols import Market

    for market in Market:
        if market in (Market.CRYPTO, Market.INDEX):
            continue
        assert market in filings.FILINGS_SUPPORTED or (
            market in filings.FILINGS_UNAVAILABLE
        ), market


def test_unavailable_reason_for_other_markets():
    assert "Bundesanzeiger" in (filings.unavailable_reason("SAP.DE") or "")
    assert "AFM" in (filings.unavailable_reason("PRX.AS") or "")
    assert "SEDAR" in (filings.unavailable_reason("SHOP.TO") or "")
    assert "SGX" in (filings.unavailable_reason("D05.SI") or "")
    assert "CVM" in (filings.unavailable_reason("PETR4.SA") or "")
    assert filings.unavailable_reason("AAPL") is None
    assert filings.unavailable_reason("0700.HK") is None
    assert filings.unavailable_reason("CBA.AX") is None
    assert filings.unavailable_reason("BP.L") is None
    assert filings.unavailable_reason("7203.T") is None
    assert filings.unavailable_reason("005930.KS") is None


def _fake_us(monkeypatch, calls, count=2):
    def fake(symbol, limit):
        calls.append((symbol, limit))
        return [
            filings.Filing(
                title=f"2025 年年度报告 {i}",
                date="2026-03-28",
                url=f"https://www.sec.gov/{symbol}/{i}",
                kind="年度报告",
                source="SEC EDGAR",
            )
            for i in range(min(count, limit))
        ]

    monkeypatch.setattr(filings_markets, "_us_filings", fake)


def test_fetch_filings_disk_cache_survives_memory_clear(monkeypatch):
    calls: list[tuple[str, int]] = []
    _fake_us(monkeypatch, calls)
    filings.clear_cache()

    first = filings.fetch_filings("AAPL")
    # 模拟进程重启: 只清内存, 6h 磁盘缓存仍应命中
    filings.clear_cache()
    second = filings.fetch_filings("AAPL")

    assert [f.title for f in second] == [f.title for f in first]
    assert calls == [("AAPL", 6)]
    # 强制刷新穿透磁盘
    filings.fetch_filings("AAPL", refresh=True)
    assert calls == [("AAPL", 6), ("AAPL", 6)]


def test_fetch_filings_larger_limit_misses_smaller_cache(monkeypatch):
    calls: list[tuple[str, int]] = []
    _fake_us(monkeypatch, calls)
    filings.clear_cache()

    assert len(filings.fetch_filings("MSFT", limit=1)) == 1
    filings.clear_cache()
    # 缓存只有 1 条, 本次要 6 条: 不能把残缺结果当完整缓存
    assert len(filings.fetch_filings("MSFT", limit=6)) == 2
    assert calls == [("MSFT", 1), ("MSFT", 6)]
