"""本地磁盘缓存: 行情持久化到 SQLite, 避免重复网络请求."""
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


CACHE_DB = Path(__file__).resolve().parent.parent / "data" / "quotes_cache.db"
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
                fetched_at REAL
            )
            """
        )
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
        con.commit()


# ---------- K线 (OHLC) 缓存 ----------


def get_ohlc_cached(symbol: str, months: int, ttl: int = OHLC_TTL) -> pd.DataFrame | None:
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
        cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
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
    cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in recs.columns]
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


def get_cached(symbols: list[str], ttl: int = CACHE_TTL) -> dict[str, "Quote"]:
    """返回缓存命中的 Symbol→Quote 字典, 未命中者不在返回值中."""
    from .prices import Quote

    _ensure_db()
    now = time.time()
    hits: dict[str, Quote] = {}
    with sqlite3.connect(CACHE_DB) as con:
        rows = con.execute(
            f"SELECT symbol, name, price, prev_close, change_pct, currency FROM quotes "
            f"WHERE symbol IN ({','.join('?'*len(symbols))}) AND fetched_at > ?",
            [*symbols, now - ttl],
        ).fetchall()
        for sym, name, price, prev, chg, ccy in rows:
            if price is not None and price > 0:
                hits[sym] = Quote(
                    symbol=sym, name=name, price=price,
                    prev_close=float(prev) if prev is not None else None,
                    change_pct=float(chg) if chg is not None else None,
                    currency=str(ccy),
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
        rows.append((q.symbol, q.name, q.price, q.prev_close, q.change_pct, q.currency, now))
    with _lock, sqlite3.connect(CACHE_DB) as con:
        con.executemany(
            """
            INSERT INTO quotes (symbol, name, price, prev_close, change_pct, currency, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                name=excluded.name, price=excluded.price,
                prev_close=excluded.prev_close, change_pct=excluded.change_pct,
                currency=excluded.currency, fetched_at=excluded.fetched_at
            """,
            rows,
        )
        con.commit()


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
        "oldest_at": oldest,
        "newest_at": newest,
        "size_bytes": size,
    }


def clear() -> int:
    """清空全部缓存 (行情 + K线), 返回删除条数."""
    _ensure_db()
    with _lock, sqlite3.connect(CACHE_DB) as con:
        n_quotes = con.execute("DELETE FROM quotes").rowcount
        n_ohlc = con.execute("DELETE FROM ohlc_cache").rowcount
        con.commit()
        return n_quotes + n_ohlc

