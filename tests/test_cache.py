"""缓存模块测试: 隔离的临时 DB, 不污染真实缓存."""
import time

import pytest

from tracker.cache import get_cached, set_cached
from tracker.prices import Quote


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    """每个测试用临时 DB 隔离."""
    db = tmp_path / "test_quotes_cache.db"
    monkeypatch.setattr("tracker.cache.CACHE_DB", db)
    import tracker.cache as cache_mod
    cache_mod._ensure_db()


class TestCache:
    def test_write_and_read(self):
        q1 = Quote(symbol="AAPL", name="Apple", price=150.0, prev_close=148.0, change_pct=1.35, currency="USD")
        q2 = Quote(symbol="600519.SS", name="茅台", price=1500.0, prev_close=1490.0, change_pct=0.67, currency="CNY")
        set_cached({"AAPL": q1, "600519.SS": q2})
        hits = get_cached(["AAPL", "600519.SS", "MISSING"])
        assert hits["AAPL"].price == 150.0
        assert hits["AAPL"].currency == "USD"
        assert hits["600519.SS"].price == 1500.0
        assert "MISSING" not in hits

    def test_cache_miss(self):
        hits = get_cached(["AAPL"])
        assert hits == {}

    def test_empty_input(self):
        assert get_cached([]) == {}
        set_cached({})  # should not error

    def test_ttl_expiration(self):
        q = Quote(symbol="AAPL", name="A", price=100.0, prev_close=99.0, change_pct=1.0, currency="USD")
        set_cached({"AAPL": q})
        # 立即读取应命中
        assert "AAPL" in get_cached(["AAPL"], ttl=60)
        # 强制过期
        assert get_cached(["AAPL"], ttl=-1) == {}

    def test_overwrite_on_update(self):
        q1 = Quote(symbol="AAPL", name="Apple", price=100.0, prev_close=99.0, change_pct=1.0, currency="USD")
        q2 = Quote(symbol="AAPL", name="Apple New", price=110.0, prev_close=108.0, change_pct=1.85, currency="USD")
        set_cached({"AAPL": q1})
        set_cached({"AAPL": q2})
        hits = get_cached(["AAPL"])
        assert hits["AAPL"].price == 110.0
        assert hits["AAPL"].change_pct == 1.85

    def test_none_price_not_cached(self):
        """price <= 0 的 quote 不被写入."""
        import tracker.cache as cm
        # set_cached 过滤 price<=0; get_cached 也过滤, 双重保险
        import sqlite3
        db = cm.CACHE_DB
        with sqlite3.connect(db) as con:
            con.execute(
                "INSERT INTO quotes (symbol, name, price, prev_close, change_pct, currency, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("TEST", "Test", -1.0, 0.0, 0.0, "USD", time.time()),
            )
            con.commit()
        assert get_cached(["TEST"]) == {}

    def test_set_cached_filters_bad_price(self):
        """set_cached 应过滤 price<=0 的脏数据."""
        bad = Quote(symbol="BAD", name="", price=-1.0, prev_close=None, change_pct=None, currency="USD")
        set_cached({"BAD": bad})
        assert get_cached(["BAD"]) == {}

    def test_invalid_symbol_in_get_cached(self):
        hits = get_cached(["AAPL", "NOT_FOUND"])
        assert "NOT_FOUND" not in hits
