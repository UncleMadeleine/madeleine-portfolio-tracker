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

    def test_invalid_currency_normalized_on_write(self):
        """数据源占位币种 "-" 落库前回退代码权威币种 (A股 → CNY)."""
        set_cached(
            {
                "600519.SS": Quote(
                    symbol="600519.SS",
                    name="茅台",
                    price=1500.0,
                    prev_close=None,
                    change_pct=None,
                    currency="-",
                )
            }
        )
        hits, _ = cache_mod.get_cached_with_age(["600519.SS"])
        assert hits["600519.SS"].currency == "CNY"

    def test_legacy_invalid_currency_normalized_on_read(self):
        """旧版本已写入的 currency='-' 行读取时也要纠正, 不能交给汇率链."""
        set_cached(
            {
                "600519.SS": Quote(
                    symbol="600519.SS",
                    name="茅台",
                    price=1500.0,
                    prev_close=None,
                    change_pct=None,
                    currency="CNY",
                )
            }
        )
        with sqlite3.connect(cache_mod.CACHE_DB) as con:
            con.execute(
                "UPDATE quotes SET currency='-' WHERE symbol='600519.SS'"
            )
            con.commit()
        hits, _ = cache_mod.get_cached_with_age(["600519.SS"])
        assert hits["600519.SS"].currency == "CNY"

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

    def test_set_cached_keeps_name_when_new_source_has_none(self, tmp_path, monkeypatch):
        """yfinance 路径常回 name=None: 不能把长桥/akshare 写的中文名抹掉."""
        db = tmp_path / "var" / "quotes_cache.db"
        monkeypatch.setattr(cache_mod, "CACHE_DB", db)
        cache_mod._ensure_db()
        cache_mod.set_cached(
            {"AAPL": Quote("AAPL", "苹果", 100.0, None, None, "USD", source="longport")}
        )
        cache_mod.set_cached(
            {"AAPL": Quote("AAPL", None, 101.0, None, None, "USD", source="yfinance")}
        )
        hits, _ = cache_mod.get_cached_with_age(["AAPL"])
        assert hits["AAPL"].name == "苹果"
        assert hits["AAPL"].price == pytest.approx(101.0)
        assert hits["AAPL"].source == "yfinance"

    def test_set_cached_keeps_name_when_new_source_has_empty_string(
        self, tmp_path, monkeypatch
    ):
        db = tmp_path / "var" / "quotes_cache.db"
        monkeypatch.setattr(cache_mod, "CACHE_DB", db)
        cache_mod._ensure_db()
        cache_mod.set_cached(
            {"AAPL": Quote("AAPL", "Apple", 100.0, None, None, "USD")}
        )
        cache_mod.set_cached(
            {"AAPL": Quote("AAPL", "", 101.0, None, None, "USD")}
        )
        hits, _ = cache_mod.get_cached_with_age(["AAPL"])
        assert hits["AAPL"].name == "Apple"


class TestBatchReads:
    def test_get_fundamentals_cached_batch(self, tmp_path, monkeypatch):
        """批量读与逐条读语义一致: 命中/过期/负缓存/缺失各归各位."""
        db = tmp_path / "var" / "quotes_cache.db"
        monkeypatch.setattr(cache_mod, "CACHE_DB", db)
        cache_mod._ensure_db()
        cache_mod.set_fundamental_cached("AAPL", 12.5, market_cap=3e12)
        cache_mod.set_fundamental_cached("MSFT", None, market_cap=None)  # 负缓存
        cache_mod.set_fundamental_cached("OLD", 1.0)
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE fundamentals SET fetched_at = ? WHERE symbol = 'OLD'",
                (time.time() - 10**6,),
            )
            con.commit()

        out = cache_mod.get_fundamentals_cached(
            ["AAPL", "MSFT", "OLD", "MISSING", "AAPL"], ttl=3600
        )
        assert out["AAPL"] == {"book_value": 12.5, "market_cap": 3e12}
        assert out["MSFT"] == {"book_value": None, "market_cap": None}
        assert "OLD" not in out  # 过期
        assert "MISSING" not in out
        # 逐条 API 仍与批量一致
        assert cache_mod.get_fundamental_cached("AAPL", ttl=3600) == out["AAPL"]

    def test_get_calendar_rows_batch(self, tmp_path, monkeypatch):
        db = tmp_path / "var" / "quotes_cache.db"
        monkeypatch.setattr(cache_mod, "CACHE_DB", db)
        cache_mod._ensure_db()
        cache_mod.set_calendar_row("g:AAPL", [{"type": "earnings", "date": "2026-11-05"}])
        cache_mod.set_calendar_row("g:MSFT", [])  # 负缓存
        out = cache_mod.get_calendar_rows(["g:AAPL", "g:MSFT", "g:MISSING"])
        assert out["g:AAPL"]["events"][0]["date"] == "2026-11-05"
        assert out["g:MSFT"]["events"] == []
        assert "g:MISSING" not in out
        assert cache_mod.get_calendar_row("g:AAPL") == out["g:AAPL"]


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


def test_ensure_db_quarantines_wal_sidecars_with_corrupt_main(tmp_path, monkeypatch):
    """隔离坏库时必须一并搬走 -wal/-shm, 避免旧 WAL 重放进新库."""
    db = tmp_path / "cache.db"
    db.write_bytes(b"this is definitely not a sqlite database")
    wal = tmp_path / "cache.db-wal"
    shm = tmp_path / "cache.db-shm"
    wal.write_bytes(b"stale wal")
    shm.write_bytes(b"stale shm")
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)

    cache_mod._ensure_db()

    assert db.exists()
    backups = list(tmp_path.glob("cache.db.corrupt-*"))
    assert any(p.name.endswith("-wal") for p in backups)
    assert any(p.name.endswith("-shm") for p in backups)
    # 旧边车内容已搬到隔离文件 (SQLite 连接坏库时可能重写 -shm, 只校验存在)
    assert any(p.read_bytes() == b"stale wal" for p in backups)
    with sqlite3.connect(db) as con:
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "quotes" in tables


def test_runtime_corruption_is_recovered_on_next_access(tmp_path, monkeypatch):
    """进程启动后同 inode 被写坏: 读路径要摘掉 ready 标记, 下次访问隔离重建."""
    db = tmp_path / "cache.db"
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)
    cache_mod._ensure_db()
    cache_mod.set_cached(
        {"AAPL": Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")}
    )
    assert "AAPL" in cache_mod.get_cached(["AAPL"])

    # 就地截断 (inode 不变), 模拟磁盘错误/外部工具覆盖; 同时清掉 WAL 边车,
    # 否则 SQLite 仍会从 WAL 恢复出旧数据 (那也是合法行为)。
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"cache.db{suffix}").unlink(missing_ok=True)
    db.write_bytes(b"this is definitely not a sqlite database")
    assert cache_mod.get_cached(["AAPL"]) == {}
    assert str(db) not in cache_mod._db_ready

    # 下一次访问触发 _ensure_db → 隔离坏库并重建
    assert cache_mod.get_cached(["AAPL"]) == {}
    assert list(tmp_path.glob("cache.db.corrupt-*"))
    cache_mod.set_cached(
        {"MSFT": Quote("MSFT", "Microsoft", 100.0, 99.0, 1.0, "USD")}
    )
    assert "MSFT" in cache_mod.get_cached(["MSFT"])


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


def test_truncated_database_recreates_schema_at_same_inode(tmp_path, monkeypatch):
    """库被就地截断成 0 字节 (inode 不变) 后仍要自动重建表, 不能永久 miss."""
    db = tmp_path / "cache.db"
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)
    cache_mod._db_ready.clear()
    cache_mod._ensure_db()
    cache_mod.set_cached(
        {"AAPL": Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")}
    )
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"cache.db{suffix}").unlink(missing_ok=True)
    db.write_bytes(b"")

    assert cache_mod.get_cached(["AAPL"]) == {}
    # 读写都要能触发重建, 而不是一直返回空/静默不写
    cache_mod.set_cached(
        {"MSFT": Quote("MSFT", "Microsoft", 100.0, 99.0, 1.0, "USD")}
    )
    assert "MSFT" in cache_mod.get_cached(["MSFT"])
    assert cache_mod.clear_quotes() >= 0


def test_clear_cache_corruption_is_readable_error(tmp_path, monkeypatch):
    """运行期损坏时 cache clear 抛 RuntimeError, 且摘掉 ready 以便下次自愈."""
    db = tmp_path / "cache.db"
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)
    cache_mod._db_ready.clear()
    cache_mod._ensure_db()
    cache_mod.set_cached(
        {"AAPL": Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")}
    )
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"cache.db{suffix}").unlink(missing_ok=True)
    db.write_bytes(b"this is definitely not a sqlite database")

    with pytest.raises(RuntimeError, match="缓存数据库"):
        cache_mod.clear_quotes()
    assert str(db) not in cache_mod._db_ready
    # 下一次访问隔离坏库并重建, 恢复可写
    cache_mod.set_cached(
        {"MSFT": Quote("MSFT", "Microsoft", 100.0, 99.0, 1.0, "USD")}
    )
    assert "MSFT" in cache_mod.get_cached(["MSFT"])


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
