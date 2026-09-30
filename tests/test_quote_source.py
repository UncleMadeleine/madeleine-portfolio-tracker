"""数据来源标记 (Quote.source): 端到端从取数链路 → 磁盘缓存 → 视图/CLI 输出."""

from __future__ import annotations

import pandas as pd
import pytest

import tracker.cache as cache_mod
import tracker.providers.orchestration as orch
from tracker.cache import clear_quotes, get_cached, set_cached
from tracker.prices import Quote


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """临时缓存 DB + 清空负缓存, 每个测试互不污染."""
    monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "test_source_cache.db")
    cache_mod._ensure_db()
    orch._neg_cache.clear()


def _q(symbol: str, source: str | None = None) -> Quote:
    return Quote(
        symbol=symbol,
        name=symbol,
        price=10.0,
        prev_close=9.9,
        change_pct=1.0,
        currency="USD",
        source=source,
    )


def test_source_round_trips_through_disk_cache():
    """写入带 source 的行情后, 缓存命中的 Quote 保留原来源 (前端缓存命中也可见)."""
    set_cached({"AAPL": _q("AAPL", source="longport")})
    hit = get_cached(["AAPL"])
    assert hit["AAPL"].source == "longport"


def test_source_none_is_tolerated_by_cache():
    """source 为 None (旧调用方/未打标) 不破坏缓存读写."""
    set_cached({"MSFT": _q("MSFT", source=None)})
    hit = get_cached(["MSFT"])
    assert hit["MSFT"].source is None


def test_clear_quotes_keeps_other_tables():
    """clear_quotes 只清实时行情表: K线/基本面缓存不受数据源切换影响."""
    set_cached({"AAPL": _q("AAPL", source="yfinance")})
    cache_mod.set_ohlc_cached("AAPL", 12, _empty_ohlc())
    cache_mod.set_fundamental_cached("AAPL", 6.1)

    assert clear_quotes() >= 1
    assert get_cached(["AAPL"]) == {}
    assert cache_mod.get_ohlc_cached("AAPL", 12) is not None
    assert cache_mod.get_fundamental_cached("AAPL") == {"book_value": 6.1}


def _empty_ohlc() -> pd.DataFrame:
    """set_ohlc_cached 忽略空表: 用单行真实载荷确保写入成功."""
    return pd.DataFrame(
        [
            {
                "date": "2026-01-05",
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "volume": 1000,
            }
        ]
    )


def test_cache_clear_preserved_quote_fields():
    """除 source 外, 原有字段序列化不回退 (书净资产/涨跌幅等)."""
    set_cached(
        {
            "0700.HK": Quote(
                symbol="0700.HK",
                name="腾讯",
                price=330.0,
                prev_close=325.0,
                change_pct=1.54,
                currency="HKD",
                book_value=45.2,
                source="akshare",
            )
        }
    )
    q = get_cached(["0700.HK"])["0700.HK"]
    assert q.price == 330.0
    assert q.currency == "HKD"
    assert q.book_value == 45.2
    assert q.change_pct == 1.54
    assert q.source == "akshare"


def test_orchestration_preserves_provider_source(monkeypatch):
    """长桥前置命中的行情保留其 source 标记 (批量层不抹掉来源)."""

    def fake_lp(parsed, cfg=None):
        return {p.yahoo: _q(p.yahoo, source="longport") for p in parsed}, None

    monkeypatch.setattr("tracker.longport.get_quotes_longport", fake_lp)
    monkeypatch.setattr(cache_mod, "get_cached", lambda syms, ttl=300: {})
    monkeypatch.setattr(cache_mod, "set_cached", lambda q: None)

    quotes, _errors, _notes = orch.get_quotes(["AAPL"], use_longport=True)
    assert quotes["AAPL"].source == "longport"
