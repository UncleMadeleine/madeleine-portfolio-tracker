"""孤儿缓存清理: 只删当前持仓/自选之外的派生缓存."""

from __future__ import annotations

import pandas as pd

from tracker import cache as cache_mod
from tracker import storage
from tracker.providers.base import Quote


def _portfolio(*symbols):
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": [
                {"symbol": s, "quantity": 1, "avg_cost": 1.0} for s in symbols
            ],
        },
        storage.PORTFOLIO_PATH,
    )


def _watchlist(*symbols):
    storage.save_watchlist(
        {"watchlist": [{"symbol": s} for s in symbols]}, storage.WATCHLIST_PATH
    )


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2026-01-01", "2026-01-02"],
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [10.0, 20.0],
        }
    )


def _seed():
    cache_mod.set_cached(
        {
            "AAPL": Quote("AAPL", "Apple", 1.0, None, None, "USD", source="yfinance"),
            "MSFT": Quote("MSFT", "Microsoft", 2.0, None, None, "USD", source="yfinance"),
        }
    )
    for sym in ("AAPL", "MSFT", "index:IX.DXY"):
        cache_mod.set_ohlc_cached(sym, 12, _frame())
    cache_mod.set_json_cached("valuation_snapshot:AAPL", {"pe_ttm": 30.0})
    cache_mod.set_json_cached("valuation_snapshot:MSFT", {"pe_ttm": 35.0})
    cache_mod.set_json_cached("filings:AAPL", {"items": []})
    cache_mod.set_json_cached("other:MSFT", {"keep": True})
    cache_mod.set_fundamental_cached("AAPL", 1.0, 2.0)
    cache_mod.set_fundamental_cached("MSFT", 3.0, 4.0)
    cache_mod.set_series_cached("shares_full:AAPL", _frame()[["date", "close"]])
    cache_mod.set_series_cached("shares_full:MSFT", _frame()[["date", "close"]])
    cache_mod.set_calendar_row("g:AAPL", [{"symbol": "AAPL"}])
    cache_mod.set_calendar_row("g:MSFT", [{"symbol": "MSFT"}])
    cache_mod.set_calendar_row("g:UNKNOWN_OK", [])
    cache_mod.set_calendar_row("cn:fhps:20260331", [])


def test_prune_orphans_dry_run_then_delete():
    _seed()
    _portfolio("AAPL")
    _watchlist("0700.HK")

    dry = cache_mod.prune_orphans(dry_run=True)
    assert dry["skipped"] is False
    assert dry["deleted"] == 0
    assert dry["would_delete"] > 0
    assert dry["tables"]["quotes"]["orphans"] == 1
    assert cache_mod.get_cached(["MSFT"])  # 干跑不删

    result = cache_mod.prune_orphans()
    assert result["deleted"] > 0
    assert not cache_mod.get_cached(["MSFT"])
    assert cache_mod.get_cached(["AAPL"])
    assert cache_mod.get_ohlc_cached("MSFT", 12) is None
    assert cache_mod.get_ohlc_cached("AAPL", 12) is not None
    # 指数缓存不在持仓/自选域, 保留
    assert cache_mod.get_ohlc_cached("index:IX.DXY", 12) is not None
    assert cache_mod.get_json_cached("valuation_snapshot:MSFT", ttl=10**9) is None
    assert cache_mod.get_json_cached("valuation_snapshot:AAPL", ttl=10**9) is not None
    # 未知前缀不归本函数管理
    assert cache_mod.get_json_cached("other:MSFT", ttl=10**9) is not None
    assert cache_mod.get_fundamental_cached("MSFT", ttl=10**9) is None
    assert cache_mod.get_series_cached("shares_full:MSFT", ttl=10**9) is None
    assert cache_mod.get_calendar_row("g:MSFT") is None
    assert cache_mod.get_calendar_row("g:AAPL") is not None
    # 哨兵/整批日历键保留
    assert cache_mod.get_calendar_row("g:UNKNOWN_OK") is not None
    assert cache_mod.get_calendar_row("cn:fhps:20260331") is not None


def test_prune_orphans_skips_empty_scope():
    cache_mod.set_cached(
        {"MSFT": Quote("MSFT", "Microsoft", 2.0, None, None, "USD")}
    )
    _portfolio()
    _watchlist()
    result = cache_mod.prune_orphans()
    assert result["skipped"] is True
    assert result["deleted"] == 0
    assert cache_mod.get_cached(["MSFT"])


def test_prune_orphans_keeps_unparseable_keys():
    _seed()
    _portfolio("AAPL")
    _watchlist()
    result = cache_mod.prune_orphans()
    # g:UNKNOWN_OK 解析失败 → 保守保留
    assert cache_mod.get_calendar_row("g:UNKNOWN_OK") is not None
    assert result["skipped"] is False
