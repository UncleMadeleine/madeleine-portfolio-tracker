"""本地磁盘缓存: 行情/基本面持久化到 SQLite, 避免重复网络请求."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from .prices import Quote


CACHE_DB = Path(__file__).resolve().parent.parent / "var" / "quotes_cache.db"
CACHE_TTL = 300  # 秒, 实时行情缓存有效期
OHLC_TTL = 1800  # 秒, 日线K线缓存有效期 (30 分钟; 收盘后数据不变, TTL 过期重取也准)

_lock = Lock()


def _ensure_db() -> None:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CACHE_DB) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                price REAL,
                prev_close REAL,
                change_pct REAL,
                currency TEXT,
                book_value REAL,
                market_cap REAL,
                source TEXT,
                fetched_at REAL
            )
            """
        )
        # 旧库无 book_value/source 列: 补列 (ALTER 失败说明已存在, 忽略)
        try:
            con.execute("ALTER TABLE quotes ADD COLUMN book_value REAL")
        except sqlite3.OperationalError:
            pass
        try:
            con.execute("ALTER TABLE quotes ADD COLUMN market_cap REAL")
        except sqlite3.OperationalError:
            pass
        try:
            con.execute("ALTER TABLE quotes ADD COLUMN source TEXT")
        except sqlite3.OperationalError:
            pass
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ohlc_cache (
                symbol TEXT NOT NULL,
                months INTEGER NOT NULL,
                payload TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (symbol, months)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS fundamentals (
                symbol TEXT PRIMARY KEY,
                book_value REAL,
                market_cap REAL,
                fetched_at REAL
            )
            """
        )
        try:
            con.execute("ALTER TABLE fundamentals ADD COLUMN market_cap REAL")
        except sqlite3.OperationalError:
            pass
        con.commit()


# ---------- K线 (OHLC) 缓存 ----------


def get_ohlc_cached(
    symbol: str, months: int, ttl: int = OHLC_TTL
) -> pd.DataFrame | None:
    """读取缓存的日线 OHLC; 未命中 / 过期 / 载荷损坏返回 None."""
    _ensure_db()
    with sqlite3.connect(CACHE_DB) as con:
        row = con.execute(
            "SELECT payload FROM ohlc_cache WHERE symbol=? AND months=? AND fetched_at > ?",
            (symbol, int(months), time.time() - ttl),
        ).fetchone()
    if row is None:
        return None
    try:
        df = pd.DataFrame(json.loads(row[0]))
        if df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        cols = [
            c
            for c in ("date", "open", "high", "low", "close", "volume")
            if c in df.columns
        ]
        return df[cols]
    except Exception:
        return None


def set_ohlc_cached(symbol: str, months: int, df: pd.DataFrame) -> None:
    """写入日线 OHLC 缓存 (整段 JSON 载荷, 按 symbol+months 覆盖)."""
    if df is None or df.empty:
        return
    _ensure_db()
    recs = df.copy()
    recs["date"] = pd.to_datetime(recs["date"]).dt.strftime("%Y-%m-%d")
    cols = [
        c
        for c in ("date", "open", "high", "low", "close", "volume")
        if c in recs.columns
    ]
    payload = json.dumps(recs[cols].to_dict(orient="records"))
    with _lock, sqlite3.connect(CACHE_DB) as con:
        con.execute(
            """
            INSERT INTO ohlc_cache (symbol, months, payload, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, months) DO UPDATE SET
                payload=excluded.payload, fetched_at=excluded.fetched_at
            """,
            (symbol, int(months), payload, time.time()),
        )
        con.commit()


# ---------- 基本面 (每股净资产) 缓存 ----------


def get_fundamental_cached(symbol: str, ttl: int = 21600) -> dict | None:
    """读取缓存的基本面指标; 未命中/过期返回 None.

    负缓存 (已确认无有效值的标的) 命中返回
    {"book_value": None, "market_cap": None}, 与「未缓存」(None) 区分,
    避免 TTL 内重复网络请求。
    """
    _ensure_db()
    now = time.time()
    with sqlite3.connect(CACHE_DB) as con:
        row = con.execute(
            "SELECT book_value, market_cap, fetched_at FROM fundamentals WHERE symbol = ?",
            (symbol,),
        ).fetchone()
    if row is None:
        return None
    bv, mc, fetched_at = row
    if fetched_at is None or now - fetched_at > ttl:
        return None
    return {
        "book_value": (
            float(bv) if isinstance(bv, (int, float)) and bv == bv and bv > 0 else None
        ),
        "market_cap": (
            float(mc) if isinstance(mc, (int, float)) and mc == mc and mc > 0 else None
        ),
    }


def set_fundamental_cached(
    symbol: str, book_value: float | None, market_cap: float | None = None
) -> None:
    """写入基本面缓存; 指标无效 (≤0/NaN) 时也落库 (负缓存, 6h 不重试).

    market_cap 为可选增量: 只补 BVPS 的旧调用方写 None, 由后续 mcap 请求覆盖。
    两个指标各自独立 COALESCE: 本次为 None 的字段保留库中旧值, 不被冲成
    NULL —— 否则 yfinance 只回 marketCap 的那次会把已缓存的 BVPS 抹掉,
    之后 6 小时 PB 提醒全部误报「PB 缺失」(且负缓存阻止重试)。
    fetched_at 刷新即整行 TTL 重新计期 (负缓存语义保留)。
    """
    _ensure_db()
    now = time.time()

    def _valid(v) -> float | None:
        if v is None or v != v or v <= 0:
            return None
        return float(v)

    bv = _valid(book_value)
    mc = _valid(market_cap)
    with _lock, sqlite3.connect(CACHE_DB) as con:
        con.execute(
            """
            INSERT INTO fundamentals (symbol, book_value, market_cap, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                book_value=COALESCE(excluded.book_value, fundamentals.book_value),
                market_cap=COALESCE(excluded.market_cap, fundamentals.market_cap),
                fetched_at=excluded.fetched_at
            """,
            (symbol, bv, mc, now),
        )
        con.commit()


def get_cached(symbols: list[str], ttl: int = CACHE_TTL) -> dict[str, "Quote"]:
    """返回缓存命中的 Symbol→Quote 字典, 未命中者不在返回值中."""
    from .prices import Quote

    if not symbols:
        return {}

    _ensure_db()
    now = time.time()
    hits: dict[str, Quote] = {}
    with sqlite3.connect(CACHE_DB) as con:
        rows = con.execute(
            f"SELECT symbol, name, price, prev_close, change_pct, currency, book_value, market_cap, source "
            f"FROM quotes "
            f"WHERE symbol IN ({','.join('?' * len(symbols))}) AND fetched_at > ?",
            [*symbols, now - ttl],
        ).fetchall()
        for sym, name, price, prev, chg, ccy, bv, mc, src in rows:
            if price is not None and price > 0:
                hits[sym] = Quote(
                    symbol=sym,
                    name=name,
                    price=price,
                    prev_close=float(prev) if prev is not None else None,
                    change_pct=float(chg) if chg is not None else None,
                    currency=str(ccy),
                    book_value=float(bv) if bv is not None else None,
                    market_cap=float(mc) if mc is not None else None,
                    source=str(src) if src else None,
                )
    return hits


def set_cached(quotes: dict[str, "Quote"]) -> None:
    """将 Quote 写入缓存, 更新 fetched_at. 过滤掉 price<=0 的脏数据."""
    if not quotes:
        return
    _ensure_db()
    now = time.time()
    rows: list[tuple] = []
    for q in quotes.values():
        if q.price is None or q.price <= 0:
            continue
        rows.append(
            (
                q.symbol,
                q.name,
                q.price,
                q.prev_close,
                q.change_pct,
                q.currency,
                q.book_value,
                getattr(q, "market_cap", None),
                getattr(q, "source", None),
                now,
            )
        )
    with _lock, sqlite3.connect(CACHE_DB) as con:
        con.executemany(
            """
            INSERT INTO quotes (symbol, name, price, prev_close, change_pct, currency, book_value, market_cap, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                name=excluded.name, price=excluded.price,
                prev_close=excluded.prev_close, change_pct=excluded.change_pct,
                currency=excluded.currency, book_value=excluded.book_value,
                market_cap=excluded.market_cap,
                source=excluded.source, fetched_at=excluded.fetched_at
            """,
            rows,
        )
        con.commit()


def clear_quotes() -> int:
    """只清实时行情缓存 (保留 K线/基本面): 数据源偏好切换后强制按新源链重取."""
    _ensure_db()
    with _lock, sqlite3.connect(CACHE_DB) as con:
        n = con.execute("DELETE FROM quotes").rowcount
        con.commit()
        return n


def info() -> dict:
    """缓存统计: 记录总数 / 有效条数 / 数据库大小."""
    _ensure_db()
    now = time.time()
    with sqlite3.connect(CACHE_DB) as con:
        total = con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        fresh = con.execute(
            "SELECT COUNT(*) FROM quotes WHERE fetched_at > ?", (now - CACHE_TTL,)
        ).fetchone()[0]
        ohlc_total = con.execute("SELECT COUNT(*) FROM ohlc_cache").fetchone()[0]
        ohlc_fresh = con.execute(
            "SELECT COUNT(*) FROM ohlc_cache WHERE fetched_at > ?", (now - OHLC_TTL,)
        ).fetchone()[0]
        oldest, newest = con.execute(
            "SELECT MIN(fetched_at), MAX(fetched_at) FROM quotes"
        ).fetchone()
    size = CACHE_DB.stat().st_size if CACHE_DB.exists() else 0
    return {
        "db": str(CACHE_DB),
        "ttl_seconds": CACHE_TTL,
        "total": total,
        "fresh": fresh,
        "stale": total - fresh,
        "ohlc_ttl_seconds": OHLC_TTL,
        "ohlc_total": ohlc_total,
        "ohlc_fresh": ohlc_fresh,
        "oldest_at": oldest or 0.0,
        "newest_at": newest or 0.0,
        "size_bytes": size,
    }


def clear() -> int:
    """清空全部缓存 (行情 + K线 + 基本面), 返回删除条数."""
    _ensure_db()
    with _lock, sqlite3.connect(CACHE_DB) as con:
        n_quotes = con.execute("DELETE FROM quotes").rowcount
        n_ohlc = con.execute("DELETE FROM ohlc_cache").rowcount
        con.commit()
        n_fund = con.execute("DELETE FROM fundamentals").rowcount
        return n_quotes + n_ohlc + n_fund
