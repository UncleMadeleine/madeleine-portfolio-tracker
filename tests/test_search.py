"""股票搜索模块测试 (纯函数, 不依赖网络)."""
import pytest

from tracker.providers.base import SymbolEntry
from tracker.providers.em_suggest import (
    em_code_to_yahoo,
    normalize_suggest_row,
    suggest_merged,
    suggest_raw,
)
from tracker.search import search_grouped, search_symbols


# ---------- 东财 suggest 底层封装 ----------


def _em_row(code, name, stype):
    return {"Code": code, "Name": name, "SecurityTypeName": stype}


def test_normalize_suggest_row_filters_junk():
    assert normalize_suggest_row(_em_row("00700", "腾讯控股", "港股")) == ("00700", "港股")
    # 只做形态校验; 域过滤 (债券/基金等) 是 provider 的职责
    assert normalize_suggest_row(_em_row("109988", "22河南75", "债券")) == ("109988", "债券")
    assert normalize_suggest_row({"Code": "", "Name": "x", "SecurityTypeName": "港股"}) is None
    assert normalize_suggest_row({"Code": "60 01", "Name": "x", "SecurityTypeName": "沪A"}) is None


def test_em_code_mapping():
    assert em_code_to_yahoo("00700", "港股") == "0700.HK"
    assert em_code_to_yahoo("600519", "沪A") == "600519.SS"
    assert em_code_to_yahoo("920799", "京A") == "920799.BJ"
    assert em_code_to_yahoo("200012", "深B") == "200012.SZ"
    assert em_code_to_yahoo("AAPL", "美股") == "AAPL"


def test_suggest_merged_pads_digits(monkeypatch):
    """纯数字查询补零二次请求 (700 → 00700), 行按内容去重。"""
    import tracker.providers.em_suggest as em

    def fake_raw(q):
        if q == "700":
            return [_em_row("600700", "*ST数码", "沪A")]
        if q == "00700":
            return [_em_row("00700", "腾讯控股", "港股")]
        return None

    monkeypatch.setattr(em, "suggest_raw", fake_raw)
    res = suggest_merged("700")
    assert res is not None
    rows, failed = res
    assert not failed
    assert {r["Code"] for r in rows} == {"600700", "00700"}


def test_suggest_merged_none_on_total_failure(monkeypatch):
    import tracker.providers.em_suggest as em

    monkeypatch.setattr(em, "suggest_raw", lambda q: None)
    assert suggest_merged("腾讯") is None


# ---------- GlobalStocksProvider.search (IBKR → yfinance → 代码直查/本地目录) ----------


@pytest.fixture
def no_ibkr(monkeypatch):
    """IBKR Gateway 不在线 (默认环境): search_matches 返回 None 触发降级。"""
    import tracker.ibkr as ibkr_mod

    monkeypatch.setattr(ibkr_mod, "search_matches", lambda q, cfg=None: None)


def test_global_search_yf_english_name(monkeypatch, no_ibkr):
    """yfinance 主源: 搜 Coinbase → COIN 美股第一。"""
    import tracker.providers.global_stocks as gs
    from tracker.providers import PROVIDERS

    monkeypatch.setattr(
        gs, "_yf_search",
        lambda q: [
            {"symbol": "COIN", "name": "Coinbase Global, Inc."},
            {"symbol": "COIN.TO", "name": "COINBASE CDR (CAD HEDGED)"},
        ],
    )
    res = PROVIDERS["global"].search("Coinbase", limit=5)
    assert res[0].code == "COIN"
    assert res[0].market == "美股" and res[0].type == "global"
    assert any(e.code == "COIN.TO" for e in res)  # 加股也在 global 域


def test_global_search_ibkr_first_when_online(monkeypatch):
    """IBKR 在线时作为第一源, 结果经 ibkr_to_yahoo 归一。"""
    import tracker.ibkr as ibkr_mod
    import tracker.providers.global_stocks as gs
    from tracker.providers import PROVIDERS

    monkeypatch.setattr(
        ibkr_mod, "search_matches",
        lambda q, cfg=None: [
            {
                "symbol": "TME", "exchange": "NYSE", "primary_exchange": "NYSE",
                "currency": "USD", "long_name": "TENCENT MUSIC ENTERTAINMENT",
            }
        ],
    )
    monkeypatch.setattr(gs, "_yf_search", lambda q: [])
    res = PROVIDERS["global"].search("TME", limit=5)
    assert res[0].code == "TME"
    assert res[0].name == "TENCENT MUSIC ENTERTAINMENT"


def test_global_search_ibkr_offline_falls_to_yf(monkeypatch):
    """IBKR 不可达 (None) → 自动落 yfinance, 不抛错。"""
    import tracker.ibkr as ibkr_mod
    import tracker.providers.global_stocks as gs
    from tracker.providers import PROVIDERS

    monkeypatch.setattr(ibkr_mod, "search_matches", lambda q, cfg=None: None)
    monkeypatch.setattr(
        gs, "_yf_search",
        lambda q: [{"symbol": "0700.HK", "name": "TENCENT"}],
    )
    res = PROVIDERS["global"].search("Tencent", limit=5)
    assert res[0].code == "0700.HK"
    assert res[0].market == "港股"


def test_global_search_bare_code_does_not_claim_cn(monkeypatch, no_ibkr):
    """纯 6 位数字属 cn 域: global 不认领, 也不进本地目录外兜底。"""
    import tracker.providers.global_stocks as gs
    from tracker.providers import PROVIDERS

    monkeypatch.setattr(gs, "_yf_search", lambda q: [])
    assert PROVIDERS["global"].search("600519", limit=5) == []


def test_global_search_code_pass_through(monkeypatch, no_ibkr):
    """带后缀代码直查兜底 (SAP.DE, yf/ibkr 都无结果时)。"""
    import tracker.providers.global_stocks as gs
    from tracker.providers import PROVIDERS

    monkeypatch.setattr(gs, "_yf_search", lambda q: [])
    res = PROVIDERS["global"].search("SAP.DE", limit=5)
    assert res[0].code == "SAP.DE"
    assert res[0].type == "global"


# ---------- CNStocksProvider.search (东财 suggest, A股/北交所) ----------


def test_cn_search_maotai(monkeypatch):
    """搜「茅台」→ cn 域出 600519.SS, 美股/港股结果不混入。"""
    import tracker.providers.em_suggest as em
    from tracker.providers import PROVIDERS

    rows = [
        _em_row("600519", "贵州茅台", "沪A"),
        _em_row("00700", "腾讯控股", "港股"),
        _em_row("AAPL", "苹果", "美股"),
    ]
    monkeypatch.setattr(em, "suggest_raw", lambda q: rows)
    res = PROVIDERS["cn"].search("茅台", limit=10)
    assert [e.code for e in res] == ["600519.SS"]
    assert res[0].type == "cn" and res[0].market == "A股"


def test_cn_search_bj_and_filters(monkeypatch):
    import tracker.providers.em_suggest as em
    from tracker.providers import PROVIDERS

    rows = [
        _em_row("920799", "艾融软件", "京A"),
        _em_row("109988", "22河南75", "债券"),
    ]
    monkeypatch.setattr(em, "suggest_raw", lambda q: rows)
    res = PROVIDERS["cn"].search("艾融", limit=10)
    assert [e.code for e in res] == ["920799.BJ"]
    assert res[0].type == "cn"


# ---------- CryptoProvider.search (yfinance Search) ----------


def test_crypto_search_yf(monkeypatch):
    """crypto 搜索 = yfinance Search 的 CRYPTOCURRENCY 过滤 (ETF 等不混入)。"""
    import tracker.providers.crypto as cm
    from tracker.providers import PROVIDERS

    class _FakeSearch:
        def __init__(self, query, **kw):
            pass
        quotes = [
            {"symbol": "BTC-USD", "shortname": "Bitcoin USD", "quoteType": "CRYPTOCURRENCY"},
            {"symbol": "BCH-USD", "shortname": "Bitcoin Cash USD", "quoteType": "CRYPTOCURRENCY"},
            {"symbol": "IBIT", "shortname": "iShares Bitcoin Trust", "quoteType": "ETF"},
            {"symbol": "BTC=F", "shortname": "Bitcoin Futures", "quoteType": "FUTURE"},
        ]

    monkeypatch.setattr("yfinance.Search", _FakeSearch)
    res = PROVIDERS["crypto"].search("bitcoin", limit=5)
    codes = [e.code for e in res]
    assert codes == ["BTC-USD", "BCH-USD"]
    assert all(e.market == "加密货币" and e.type == "crypto" for e in res)


def test_crypto_search_exact_code(monkeypatch):
    """BTC-USD 直查 → 精确命中排首位。"""
    import tracker.providers.crypto as cm
    from tracker.providers import PROVIDERS

    class _FakeSearch:
        def __init__(self, query, **kw):
            pass
        quotes = [
            {"symbol": "BTC-USD", "shortname": "Bitcoin USD", "quoteType": "CRYPTOCURRENCY"},
            {"symbol": "CBBTC-USD", "shortname": "Wrapped BTC", "quoteType": "CRYPTOCURRENCY"},
        ]

    monkeypatch.setattr("yfinance.Search", _FakeSearch)
    res = PROVIDERS["crypto"].search("BTC-USD", limit=5)
    assert res[0].code == "BTC-USD"


def test_crypto_search_empty_on_network_fail(monkeypatch):
    """yf 不可达 → 空结果 (无本地目录兜底)。"""
    import tracker.providers.crypto as cm
    from tracker.providers import PROVIDERS

    def _boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr("yfinance.Search", _boom)
    assert PROVIDERS["crypto"].search("bitcoin", limit=5) == []


# ---------- 聚合层 (search_grouped / search_symbols) ----------


def test_search_grouped_keeps_domains_separate(monkeypatch):
    """分域聚合: cn (东财) / global (yf) / crypto (yf Search) 各自独立, 结果不混排。"""
    import tracker.providers.em_suggest as em
    import tracker.providers.global_stocks as gs
    import tracker.ibkr as ibkr_mod

    # cn 域: 东财 suggest
    monkeypatch.setattr(
        em, "suggest_raw",
        lambda q: [_em_row("600519", "贵州茅台", "沪A")],
    )
    # global 域: yfinance
    monkeypatch.setattr(ibkr_mod, "search_matches", lambda q, cfg=None: None)
    monkeypatch.setattr(
        gs, "_yf_search", lambda q: [{"symbol": "AAPL", "name": "Apple Inc."}],
    )
    # crypto 域: yf Search mock — "苹果" 无 crypto 结果, "BTC" 返回 BTC-USD
    class _FakeSearch:
        def __init__(self, query, **kw):
            self.quotes = (
                [{"symbol": "BTC-USD", "shortname": "Bitcoin USD", "quoteType": "CRYPTOCURRENCY"}]
                if query == "BTC"
                else []
            )

    monkeypatch.setattr("yfinance.Search", _FakeSearch)
    g = search_grouped("苹果", limit_per_domain=5)
    assert [r["code"] for r in g["cn"]] == ["600519.SS"]
    assert g["global"][0]["code"] == "AAPL"
    assert g["crypto"] == []
    g2 = search_grouped("BTC", limit_per_domain=5)
    assert g2["crypto"][0]["code"] == "BTC-USD"
    for items in g.values():
        for item in items:
            assert set(item.keys()) == {"code", "name", "market", "type"}


def test_search_symbols_flattens_domains(monkeypatch):
    """平铺聚合: cn 在前 global 在后 (固定域序), global 有命中即出。"""
    import tracker.providers.em_suggest as em
    import tracker.providers.global_stocks as gs
    import tracker.ibkr as ibkr_mod

    # cn 有命中 (贵州茅台), global 也有命中 (TENCENT)
    monkeypatch.setattr(
        em, "suggest_raw",
        lambda q: [_em_row("600519", "贵州茅台", "沪A")],
    )
    monkeypatch.setattr(ibkr_mod, "search_matches", lambda q, cfg=None: None)
    monkeypatch.setattr(
        gs, "_yf_search",
        lambda q: [{"symbol": "0700.HK", "name": "TENCENT"}],
    )

    class _EmptySearch:
        def __init__(self, query, **kw):
            pass
        quotes = []

    monkeypatch.setattr("yfinance.Search", _EmptySearch)
    res = search_symbols("腾讯", limit=10)
    codes = [r["code"] for r in res]
    assert codes == ["600519.SS", "0700.HK"]  # 域序 cn → global → crypto
    assert all(r["type"] in ("cn", "global", "crypto") for r in res)


def test_search_symbols_limit_truncates_in_domain_order(monkeypatch):
    """limit 截断按域序分配名额: cn 域优先, 小 limit 时后两域可无露出
    (钉住实现行为 —— 需要三域均衡露出应走 search_grouped)."""
    import tracker.search as search_mod
    from unittest.mock import patch

    def mk(domain, n):
        return lambda q, limit=10: [
            SymbolEntry(f"{domain}{i}", "N", "M", domain) for i in range(n)
        ]

    fake = {
        "cn": type("P", (), {"search": staticmethod(mk("cn", 5))})(),
        "global": type("P", (), {"search": staticmethod(mk("gl", 5))})(),
        "crypto": type("P", (), {"search": staticmethod(mk("cr", 5))})(),
    }
    with patch.dict(search_mod.PROVIDERS, fake):
        res = search_symbols("x", limit=7)
        assert len(res) == 7
        domains = [r["type"] for r in res]
        assert domains == ["cn"] * 5 + ["gl"] * 2  # 域序 + 截断
        # 更小的 limit: 只剩 cn 域
        res3 = search_symbols("x", limit=3)
        assert [r["type"] for r in res3] == ["cn"] * 3


# ---------- 兜底直查 (_fallback_match) ----------


def test_fallback_match_passes_valid_code():
    """合法代码直查: 经 parse 归一, 权威 type 由 parse 推导."""
    from tracker.search import _fallback_match

    res = _fallback_match("600519.SS", 5)
    assert len(res) == 1
    assert res[0]["code"] == "600519.SS"
    assert res[0]["type"] == "cn"
    assert res[0]["market"] == "A股"


def test_fallback_match_normalizes_alias():
    """别名归一: 00700.HK → 0700.HK."""
    from tracker.search import _fallback_match

    res = _fallback_match("00700.HK", 5)
    assert res[0]["code"] == "0700.HK"
    assert res[0]["type"] == "global"


def test_fallback_match_rejects_invalid():
    from tracker.search import _fallback_match

    assert _fallback_match("NOT-ACODE!!", 5) == []
    assert _fallback_match("   ", 5) == []


# ---------- 其它 ----------


def test_search_empty_query():
    assert search_symbols("", limit=5) == []


