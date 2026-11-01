"""缓存模块测试: 隔离的临时 DB, 不污染真实缓存."""

import sqlite3
import time

import pandas as pd
import pytest

import tracker.cache as cache_mod
import tracker.providers.orchestration as orch
from tracker.cache import get_cached, set_cached
from tracker.prices import Quote
from tracker.symbols import parse


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    """每个测试用临时 DB 隔离."""
    db = tmp_path / "test_quotes_cache.db"
    monkeypatch.setattr("tracker.cache.CACHE_DB", db)
    import tracker.cache as cache_mod

    cache_mod._ensure_db()


class TestCache:
    def test_write_and_read(self):
        q1 = Quote(
            symbol="AAPL",
            name="Apple",
            price=150.0,
            prev_close=148.0,
            change_pct=1.35,
            currency="USD",
        )
        q2 = Quote(
            symbol="600519.SS",
            name="茅台",
            price=1500.0,
            prev_close=1490.0,
            change_pct=0.67,
            currency="CNY",
        )
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
        q = Quote(
            symbol="AAPL",
            name="A",
            price=100.0,
            prev_close=99.0,
            change_pct=1.0,
            currency="USD",
        )
        set_cached({"AAPL": q})
        # 立即读取应命中
        assert "AAPL" in get_cached(["AAPL"], ttl=60)
        # 强制过期
        assert get_cached(["AAPL"], ttl=-1) == {}

    def test_overwrite_on_update(self):
        q1 = Quote(
            symbol="AAPL",
            name="Apple",
            price=100.0,
            prev_close=99.0,
            change_pct=1.0,
            currency="USD",
        )
        q2 = Quote(
            symbol="AAPL",
            name="Apple New",
            price=110.0,
            prev_close=108.0,
            change_pct=1.85,
            currency="USD",
        )
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
        bad = Quote(
            symbol="BAD",
            name="",
            price=-1.0,
            prev_close=None,
            change_pct=None,
            currency="USD",
        )
        set_cached({"BAD": bad})
        assert get_cached(["BAD"]) == {}

    def test_invalid_symbol_in_get_cached(self):
        hits = get_cached(["AAPL", "NOT_FOUND"])
        assert "NOT_FOUND" not in hits


class TestCacheCoalesce:
    def test_set_cached_keeps_book_value_and_mcap(self, tmp_path, monkeypatch):
        """yfinance 路径 (BVPS/市值恒 None) 不能把库里 akshare 写的值抹成 NULL."""
        db = tmp_path / "var" / "quotes_cache.db"
        monkeypatch.setattr(cache_mod, "CACHE_DB", db)
        cache_mod._ensure_db()
        cache_mod.set_cached(
            {
                "600519.SS": Quote(
                    "600519.SS", "贵州茅台", 1500.0, None, None, "CNY",
                    book_value=38.0, market_cap=1.9e12, source="akshare",
                )
            }
        )
        cache_mod.set_cached(
            {"600519.SS": Quote("600519.SS", None, 1510.0, None, None, "CNY")}
        )
        hits, _ = cache_mod.get_cached_with_age(["600519.SS"])
        assert hits["600519.SS"].book_value == pytest.approx(38.0)
        assert hits["600519.SS"].market_cap == pytest.approx(1.9e12)


def test_ensure_db_does_not_quarantine_locked_database(tmp_path, monkeypatch):
    """database is locked 属瞬时错误: 不能把正在使用的库 os.replace 隔离."""
    db = tmp_path / "cache.db"
    db.write_bytes(b"")
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)

    def boom(con):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cache_mod, "_create_schema", boom)
    cache_mod._ensure_db()
    assert db.exists()
    assert not list(tmp_path.glob("cache.db.corrupt-*"))


def test_ensure_db_still_recovers_corrupt_file(tmp_path, monkeypatch):
    """真正的坏库仍走隔离重建 (缓存可再生)."""
    db = tmp_path / "cache.db"
    db.write_bytes(b"this is definitely not a sqlite database")
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)
    cache_mod._ensure_db()
    assert db.exists()
    assert list(tmp_path.glob("cache.db.corrupt-*"))
    with sqlite3.connect(db) as con:
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "quotes" in tables


def test_kline_cache_key_includes_source_flags(monkeypatch):
    from tracker import prices

    calls: list[dict] = []

    def fake_history(symbol, months=12, **kwargs):
        calls.append(kwargs)
        base = 200.0 if kwargs.get("prefer_akshare") else 100.0
        return pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-10-20"]),
                "open": [base],
                "high": [base],
                "low": [base],
                "close": [base],
                "volume": [0.0],
            }
        )

    monkeypatch.setattr(prices, "get_history", fake_history)
    cache_mod.clear()
    default = prices.get_ohlc("0700.HK", months=12)
    akshare = prices.get_ohlc("0700.HK", months=12, prefer_akshare=True)
    assert default["close"].iloc[0] == pytest.approx(100.0)
    assert akshare["close"].iloc[0] == pytest.approx(200.0)
    assert len(calls) == 2


class TestCacheFixes:
    def test_fundamental_cache_coalesces_both_fields(self, tmp_path, monkeypatch):
        """mcap 写入不冲掉已缓存的 BVPS, BVPS 写入也不冲掉 mcap."""
        from tracker import cache as cache_mod

        monkeypatch.setenv(
            "TRACKER_CACHE_DB", str(tmp_path / "q.db")
        )
        importlib = __import__("importlib")
        importlib.reload(cache_mod)
        cache_mod.set_fundamental_cached("AAPL", 12.5)
        cache_mod.set_fundamental_cached("AAPL", None, market_cap=3e12)
        row = cache_mod.get_fundamental_cached("AAPL", ttl=99999)
        assert row["book_value"] == 12.5
        assert row["market_cap"] == 3e12
        cache_mod.set_fundamental_cached("AAPL", 4.0)
        row2 = cache_mod.get_fundamental_cached("AAPL", ttl=99999)
        assert row2["book_value"] == 4.0
        assert row2["market_cap"] == 3e12

    def test_neg_clear_all(self):
        orch.neg_mark("X")
        assert orch.neg_cached("X")
        orch.neg_clear_all()
        assert not orch.neg_cached("X")

    def test_disk_cache_hit_clears_negative(self, monkeypatch):
        """磁盘缓存命中同样是成功: 清出负缓存 (修复前只有源链路径清)."""
        orch.neg_mark("Z")
        monkeypatch.setattr(
            orch.cache_mod,
            "get_cached",
            lambda syms, ttl=300: {
                "Z": Quote(
                    symbol="Z", name="Z", price=1.0, prev_close=1.0,
                    change_pct=0.0, currency="USD",
                )
            },
        )
        quotes, _, _ = orch.get_quotes(["Z"])
        assert "Z" in quotes
        assert not orch.neg_cached("Z")

    def test_base_exception_does_not_destroy_batch(self, monkeypatch):
        """worker 抛 SystemExit 等 BaseException: 单代码记错, 不炸掉整批."""

        class _Provider:
            def fetch_quote(self, p, prefer_first=False):
                if p.yahoo == "BAD":
                    raise SystemExit("worker exit")
                return Quote(
                    symbol=p.yahoo, name=p.yahoo, price=1.0, prev_close=1.0,
                    change_pct=0.0, currency="USD",
                )

        monkeypatch.setattr(orch, "resolve", lambda t: _Provider())
        plist = [parse("BAD"), parse("OK1"), parse("OK2")]
        errors: dict[str, str] = {}
        out = orch._fetch_domain("global", plist, False, errors)
        assert sorted(out) == ["OK1", "OK2"]
        assert "BAD" in errors


def test_fundamental_cache_filters_non_finite():
    """±Infinity 不能当作有效基本面落库 (否则 6h 内在 snapshot JSON 里变非法)."""
    from tracker import cache as cache_mod

    cache_mod.set_fundamental_cached(
        "INF.US", float("inf"), market_cap=float("-inf")
    )
    hit = cache_mod.get_fundamental_cached("INF.US", ttl=3600)
    assert hit == {"book_value": None, "market_cap": None}
    # 正常值仍然有效
    cache_mod.set_fundamental_cached("OK.US", 12.5, market_cap=3e9)
    assert cache_mod.get_fundamental_cached("OK.US", ttl=3600) == {
        "book_value": 12.5,
        "market_cap": 3e9,
    }
