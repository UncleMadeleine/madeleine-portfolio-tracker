"""本地磁盘缓存: 行情持久化到 SQLite, 避免重复网络请求."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .prices import Quote


CACHE_DB = Path(__file__).resolve().parent.parent / "data" / "quotes_cache.db"
CACHE_TTL = 300  # 秒, 缓存有效期

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
                    prev_close=float(prev) if prev else None,
                    change_pct=float(chg) if chg else None,
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
    with sqlite3.connect(CACHE_DB) as con:
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
        "oldest_at": oldest,
        "newest_at": newest,
        "size_bytes": size,
    }


def clear() -> int:
    """清空全部缓存, 返回删除条数."""
    _ensure_db()
    with sqlite3.connect(CACHE_DB) as con:
        cur = con.execute("DELETE FROM quotes")
        con.commit()
        return cur.rowcount

