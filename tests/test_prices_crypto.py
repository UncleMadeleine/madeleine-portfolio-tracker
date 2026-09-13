"""行情路由离线测试: 加密货币与股票混查时的分流与降级."""
import types

import pandas as pd
import pytest

from tracker import prices
from tracker.prices import Quote, get_quotes


def _q(sym, price, ccy="USD", name=None):
    return Quote(symbol=sym, name=name or sym, price=price, prev_close=price,
                 change_pct=0.0, currency=ccy)


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    """每个测试用临时 DB 隔离, 并清掉 get_quotes 的缓存命中路径."""
    monkeypatch.setattr("tracker.cache.CACHE_DB", tmp_path / "test_quotes_cache.db")
    import tracker.cache as cache_mod
    cache_mod._ensure_db()
    monkeypatch.setattr(prices.cache_mod, "get_cached", lambda syms, ttl=300: {})
    monkeypatch.setattr(prices.cache_mod, "set_cached", lambda q: None)


def test_get_quotes_mixed_crypto_and_stocks_split_routing(monkeypatch):
    """混合查询: crypto 走直连逐个取, 股票走批量; 两边互不干扰."""
    batch_called, direct_called = [], []

    def fake_batch(parsed):
        batch_called.extend(p.yahoo for p in parsed)
        return {"AAPL": _q("AAPL", 150.0), "0700.HK": _q("0700.HK", 330.0, ccy="HKD")}

    def fake_fetch(p, prefer_akshare=False):
        direct_called.append(p.yahoo)
        return _q(p.yahoo, 60000.0)

    monkeypatch.setattr(prices, "_yahoo_batch", fake_batch)
    monkeypatch.setattr(prices, "_fetch_quote", fake_fetch)

    quotes, errors, notes = get_quotes(["AAPL", "0700.HK", "BTC-USD", "ETH-USD"])

    assert errors == {} and notes == []
    assert set(quotes) == {"AAPL", "0700.HK", "BTC-USD", "ETH-USD"}
    # crypto 不进股票批量管道
    assert "BTC-USD" not in batch_called and "ETH-USD" not in batch_called
    assert set(direct_called) == {"BTC-USD", "ETH-USD"}
    assert quotes["BTC-USD"].price == 60000.0


def test_get_quotes_crypto_failure_does_not_block_stocks(monkeypatch):
    """crypto 直连失败只记 errors, 股票批量照常返回."""
    def fake_batch(parsed):
        return {"AAPL": _q("AAPL", 150.0)}

    def failing_fetch(p, prefer_akshare=False):
        raise RuntimeError(f"{p.yahoo}: yfinance 无最新价")

    monkeypatch.setattr(prices, "_yahoo_batch", fake_batch)
    monkeypatch.setattr(prices, "_fetch_quote", failing_fetch)

    quotes, errors, notes = get_quotes(["AAPL", "BTC-USD"])

    assert quotes["AAPL"].price == 150.0
    assert "BTC-USD" in errors and "yfinance" in errors["BTC-USD"]


def test_quote_route_crypto_yf_first():
    """crypto 路由固定 yfinance 直连在前, --akshare 也不改变 crypto 的首选."""
    route = prices._quote_route(prices.parse("BTC-USD"), prefer_akshare=True)
    assert route[0] is prices._yf_crypto_quote
    assert route[1] is prices._akshare_crypto_quote
    # 对照: A股在 prefer_akshare=True 时 akshare 在前
    cn_route = prices._quote_route(prices.parse("600519.SS"), prefer_akshare=True)
    assert cn_route[0] is prices._akshare_quote


def test_quote_route_stock_untouched():
    """无后缀代码路由不回归: 仍只有 yfinance 一条."""
    route = prices._quote_route(prices.parse("AAPL"), prefer_akshare=False)
    assert route == [prices._yahoo_quote]


def test_akshare_crypto_quote_pair_matching(monkeypatch):
    """akshare 降级: 无连字符交易品种列 (BTCUSD) 能匹配 BTC-USD."""
    ak = pytest.importorskip("akshare")
    df = pd.DataFrame({
        "交易品种": ["BTCUSD", "ETHUSD"],
        "最近报价": ["61000.5", "3000.25"],
        "涨跌幅": ["1.5", "-0.8"],
    })
    monkeypatch.setattr(ak, "crypto_js_spot", lambda: df)
    q = prices._akshare_crypto_quote(prices.parse("BTC-USD"))
    assert q.symbol == "BTC-USD"
    assert q.price == 61000.5
    assert q.currency == "USD"
    assert q.change_pct == 1.5


def test_akshare_crypto_quote_missing_pair(monkeypatch):
    """akshare 降级查不到对应交易品种时抛错 (供上层继续回退)."""
    ak = pytest.importorskip("akshare")
    df = pd.DataFrame({"交易品种": ["ETHUSD"], "最近报价": ["3000.0"], "涨跌幅": [None]})
    monkeypatch.setattr(ak, "crypto_js_spot", lambda: df)
    with pytest.raises(RuntimeError, match="BTCUSD"):
        prices._akshare_crypto_quote(prices.parse("BTC-USD"))


def _fake_yf(last, prev=None):
    """构造带 fast_info 的假 yfinance 模块."""
    info = {
        "lastPrice": last,
        "previousClose": prev,
        "yearChange": 0.8,
        "currency": "USD",
    }

    class FakeInfo:
        def get(self, key, default=None):
            return info.get(key, default)

    class FakeTicker:
        def __init__(self, symbol):
            assert symbol == "BTC-USD"

        @property
        def fast_info(self):
            return FakeInfo()

    return types.SimpleNamespace(Ticker=FakeTicker)


def test_yf_crypto_quote_from_fast_info(monkeypatch):
    """yfinance 直连: 从 fast_info 取 lastPrice, 日内涨跌由 last/prev 推算."""
    monkeypatch.setattr(prices, "_yf", lambda: _fake_yf(61000.0, 60000.0))

    q = prices._yf_crypto_quote(prices.parse("BTC-USD"))
    assert q.price == 61000.0
    assert q.prev_close == 60000.0
    assert q.change_pct == pytest.approx((61000 / 60000 - 1) * 100)
    assert q.currency == "USD"


def test_yf_crypto_quote_missing_price_raises(monkeypatch):
    """yfinance 直连无 lastPrice 时抛错, 供降级到 akshare."""
    monkeypatch.setattr(prices, "_yf", lambda: _fake_yf(None))

    with pytest.raises(RuntimeError, match="无最新价"):
        prices._yf_crypto_quote(prices.parse("BTC-USD"))
