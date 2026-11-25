"""沪深可转债行情兜底: yfinance 不覆盖转债, 东财限流时走新浪单代码."""

from __future__ import annotations

import pandas as pd
import pytest

from tracker.providers import stock_akshare as gs
from tracker.symbols import parse


def test_cn_convertible_bond_symbol_detection():
    assert gs._cn_bond_sina_symbol(parse("110081.SS")) == "sh110081"
    assert gs._cn_bond_sina_symbol(parse("111000.SS")) == "sh111000"
    assert gs._cn_bond_sina_symbol(parse("113050.SS")) == "sh113050"
    assert gs._cn_bond_sina_symbol(parse("118000.SS")) == "sh118000"
    assert gs._cn_bond_sina_symbol(parse("128136.SZ")) == "sz128136"
    assert gs._cn_bond_sina_symbol(parse("123456.SZ")) == "sz123456"
    # 股票/B股/港股/北交所都不走转债兜底
    assert gs._cn_bond_sina_symbol(parse("600585.SS")) is None
    assert gs._cn_bond_sina_symbol(parse("300750.SZ")) is None
    assert gs._cn_bond_sina_symbol(parse("900901.SS")) is None
    assert gs._cn_bond_sina_symbol(parse("0700.HK")) is None
    assert gs._cn_bond_sina_symbol(parse("830799.BJ")) is None


def test_sina_bond_quote_raw_field_mapping(monkeypatch):
    body = (
        "闻泰转债,86.200,84.734,85.499,86.580,85.200,85.494,85.543,"
        "593490,50908824.000,10,85.494,150,85.468,10,85.467,10,85.459,"
        "250,85.425,50,85.543,180,85.544,1900,85.546,600,85.556,10,"
        "85.557,2026-11-24,10:00:48,00,"
    )

    class _Resp:
        encoding = None
        text = f'var hq_str_sh110081="{body}";'

    class _Req:
        def get(self, *_a, **_k):
            return _Resp()

    monkeypatch.setattr(gs, "_requests", lambda: _Req())
    monkeypatch.setattr(gs, "_sina_quote_cache", {})

    d = gs._sina_bond_quote_raw(parse("110081.SS"))
    assert d is not None
    assert d["name"] == "闻泰转债"
    assert d["price"] == "85.499"
    assert d["prev_close"] == "84.734"
    assert d["date"] == "2026-11-24"
    assert d["time"] == "10:00:48"


def test_akshare_quote_falls_back_to_sina_for_bond(monkeypatch):
    """东财限流时转债仍能取到新浪行情, 不会因 yfinance 无数据而稳定失败."""
    monkeypatch.setattr(gs, "_ak_quote_raw", lambda p: None)
    monkeypatch.setattr(
        gs,
        "_sina_bond_quote_raw",
        lambda p: {
            "name": "闻泰转债",
            "prev_close": "84.734",
            "price": "85.499",
        },
    )
    q = gs._akshare_quote(parse("110081.SS"))
    assert q.name == "闻泰转债"
    assert q.price == 85.499
    assert q.prev_close == 84.734
    assert q.change_pct == pytest.approx((85.499 / 84.734 - 1) * 100)
    assert q.currency == "CNY"
    assert q.source == "akshare_em"


def test_stock_quote_does_not_use_bond_fallback(monkeypatch):
    monkeypatch.setattr(gs, "_ak_quote_raw", lambda p: None)
    monkeypatch.setattr(
        gs,
        "_sina_bond_quote_raw",
        lambda p: pytest.fail("股票不应走可转债兜底"),
    )
    with pytest.raises(RuntimeError, match="akshare 行情不可用"):
        gs._akshare_quote(parse("600585.SS"))


def test_bond_without_valid_price_raises(monkeypatch):
    monkeypatch.setattr(gs, "_ak_quote_raw", lambda p: None)
    monkeypatch.setattr(
        gs,
        "_sina_bond_quote_raw",
        lambda p: {"name": "停牌转债", "prev_close": "-", "price": "-"},
    )
    with pytest.raises(RuntimeError, match="可转债无有效最新价"):
        gs._akshare_quote(parse("110081.SS"))


def test_akshare_history_uses_bond_daily(monkeypatch):
    calls: list[str] = []

    class _FakeAk:
        @staticmethod
        def bond_zh_hs_cov_daily(symbol):
            calls.append(symbol)
            return pd.DataFrame(
                {
                    "date": ["2026-08-01", "2026-11-20", "2026-11-23"],
                    "open": [100.0, 82.6, 83.8],
                    "high": [101.0, 84.1, 84.9],
                    "low": [99.0, 81.1, 83.4],
                    "close": [100.5, 83.923, 84.734],
                    "volume": [1, 2, 3],
                }
            )

        @staticmethod
        def stock_zh_a_hist(**_kwargs):
            pytest.fail("可转债不应调用股票日K接口")

    monkeypatch.setattr(gs, "_ak", lambda: _FakeAk)

    df = gs._akshare_history(parse("110081.SS"), "2026-11-01", "2026-11-21")
    assert calls == ["sh110081"]
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 1
    assert df.iloc[0]["close"] == 83.923
    assert str(df.iloc[0]["date"].date()) == "2026-11-20"
