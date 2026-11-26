"""无效/过期缓存 GC: 旧源签名、损坏载荷、过期序列与旧报表期."""

from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd

from tracker import cache as cache_mod
from tracker import prices, storage
from tracker.providers.base import Quote


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


def _age(table: str, key_col: str, key: str, days: float, extra: str = "") -> None:
    with cache_mod._connection() as con:
        con.execute(
            f"UPDATE {table} SET fetched_at=? WHERE {key_col}=?{extra}",
            (time.time() - days * 86400, key),
        )
        con.commit()


def _raw_exec(sql: str, params=()) -> None:
    with cache_mod._connection() as con:
        con.execute(sql, params)
        con.commit()


def test_gc_invalid_cleans_old_signature_corrupt_and_stale():
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": [{"symbol": "AAPL", "quantity": 1, "avg_cost": 1}],
        },
        storage.PORTFOLIO_PATH,
    )
    storage.save_watchlist(
        {
            "watchlist": [
                {"symbol": "0700.HK"},
                {"symbol": "600519.SS"},
                {"symbol": "601398.SS"},
            ]
        },
        storage.WATCHLIST_PATH,
    )
    df = _frame()
    current_key = prices.ohlc_cache_key("AAPL")

    cache_mod.set_cached(
        {"AAPL": Quote("AAPL", "Apple", 1.0, None, None, "USD", source="yfinance")}
    )
    cache_mod.set_ohlc_cached(current_key, 12, df)
    cache_mod.set_ohlc_cached(f"{current_key}|a1i0l0cauto", 12, df)
    _age("ohlc_cache", "symbol", f"{current_key}|a1i0l0cauto", 4)
    _raw_exec(
        "INSERT INTO ohlc_cache(symbol, months, payload, fetched_at) VALUES (?,?,?,?)",
        (current_key, 99, "not json", time.time()),
    )

    cache_mod.set_series_cached("valuation:AAPL:pe:12", df[["date", "close"]])
    cache_mod.set_series_cached("valuation:MSFT:pe:12", df[["date", "close"]])
    cache_mod.set_series_cached("valuation:0700.HK:pe:12", df[["date", "close"]])
    _age("series_cache", "key", "valuation:0700.HK:pe:12", 8)
    cache_mod.set_series_cached("shares_full_v2:AAPL", df[["date", "close"]])
    cache_mod.set_series_cached("shares_full_v2:MSFT", df[["date", "close"]])
    cache_mod.set_series_cached("shares_full:MSFT", df[["date", "close"]])
    cache_mod.set_series_cached("em_value:600519", df[["date", "close"]])
    cache_mod.set_series_cached("em_value:601398", df[["date", "close"]])
    cache_mod.set_series_cached("em_value:000001", df[["date", "close"]])
    _age("series_cache", "key", "em_value:601398", 31)
    _raw_exec(
        "INSERT INTO series_cache(key, payload, fetched_at) VALUES (?,?,?)",
        ("valuation:AAPL:pe:98", "not json", time.time()),
    )

    cache_mod.set_json_cached("valuation_snapshot:AAPL", {"pe_ttm": 30.0})
    cache_mod.set_json_cached("valuation_snapshot:MSFT", {"pe_ttm": 35.0})
    cache_mod.set_json_cached("filings:AAPL", {"items": []})
    _raw_exec(
        "INSERT INTO json_cache(key, payload, fetched_at) VALUES (?,?,?)",
        ("filings:0700.HK", "not json", time.time()),
    )

    cache_mod.set_calendar_row("g:AAPL", [{"symbol": "AAPL"}])
    cache_mod.set_calendar_row("cn:fhps:20250930", [])
    cache_mod.set_calendar_row("cn:fhps:20261231", [])
    today = date.today()
    old_month = (today.replace(day=1) - timedelta(days=120)).strftime("%Y-%m")
    new_month = today.strftime("%Y-%m")
    cache_mod.set_calendar_row(f"lp:US:earning:{old_month}", [])
    cache_mod.set_calendar_row(f"lp:US:earning:{new_month}", [])
    _raw_exec(
        "INSERT INTO calendar_cache(key, payload, fetched_at) VALUES (?,?,?)",
        ("g:0700.HK", "not json", time.time()),
    )

    cache_mod.set_fx_cached("CNY", {"USD": 7.1})
    _raw_exec(
        "INSERT INTO fx_rates(base, currency, rate, fetched_at) VALUES (?,?,?,?)",
        ("CNY", "JPY", 0.0, time.time()),
    )
    cache_mod.set_fundamental_cached("AAPL", 1.0, 2.0)
    _raw_exec(
        "INSERT INTO fundamentals(symbol, book_value, market_cap, fetched_at, "
        "book_value_checked, market_cap_checked) VALUES (?,?,?,?,1,1)",
        ("0700.HK", 0.0, 0.0, time.time()),
    )
    _raw_exec(
        "INSERT INTO quotes(symbol, name, price, prev_close, change_pct, currency, "
        "book_value, market_cap, pe_dynamic, source, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("BAD", "bad", 0.0, None, None, "USD", None, None, None, "yfinance", time.time()),
    )

    dry = cache_mod.gc_invalid(dry_run=True)
    assert dry["skipped"] is False
    assert dry["deleted"] == 0
    assert dry["would_delete"] > 0
    assert (
        cache_mod.get_ohlc_cached(f"{current_key}|a1i0l0cauto", 12, ttl=10**9)
        is not None
    )

    result = cache_mod.gc_invalid()
    assert result["deleted"] > 0
    # 当前源签名/正常快照保留
    assert cache_mod.get_ohlc_cached(current_key, 12) is not None
    assert (
        cache_mod.get_ohlc_cached(f"{current_key}|a1i0l0cauto", 12, ttl=10**9)
        is None
    )
    assert cache_mod.get_ohlc_cached(current_key, 99) is None
    assert cache_mod.get_series_cached("valuation:AAPL:pe:12", ttl=10**9) is not None
    assert cache_mod.get_series_cached("valuation:MSFT:pe:12", ttl=10**9) is None
    assert cache_mod.get_series_cached("valuation:0700.HK:pe:12", ttl=10**9) is None
    assert cache_mod.get_series_cached("shares_full_v2:AAPL", ttl=10**9) is not None
    assert cache_mod.get_series_cached("shares_full_v2:MSFT", ttl=10**9) is None
    assert cache_mod.get_series_cached("shares_full:MSFT", ttl=10**9) is None
    assert cache_mod.get_series_cached("em_value:600519", ttl=10**9) is not None
    assert cache_mod.get_series_cached("em_value:601398", ttl=10**9) is None
    assert cache_mod.get_series_cached("em_value:000001", ttl=10**9) is None
    assert cache_mod.get_json_cached("valuation_snapshot:AAPL", ttl=10**9) is not None
    assert cache_mod.get_json_cached("valuation_snapshot:MSFT", ttl=10**9) is None
    assert cache_mod.get_json_cached("filings:AAPL", ttl=10**9) is not None
    assert cache_mod.get_json_cached("filings:0700.HK", ttl=10**9) is None
    assert cache_mod.get_calendar_row("g:AAPL") is not None
    assert cache_mod.get_calendar_row("cn:fhps:20250930") is None
    assert cache_mod.get_calendar_row("cn:fhps:20261231") is not None
    assert cache_mod.get_calendar_row(f"lp:US:earning:{old_month}") is None
    assert cache_mod.get_calendar_row(f"lp:US:earning:{new_month}") is not None
    assert cache_mod.get_calendar_row("g:0700.HK") is None
    assert cache_mod.get_cached(["BAD"]) == {}
    assert cache_mod.get_fundamental_cached("0700.HK", ttl=10**9) is None
    rates, _ = cache_mod.get_fx_cached("CNY", ["USD", "JPY"], ttl=10**9)
    assert "USD" in rates and "JPY" not in rates
