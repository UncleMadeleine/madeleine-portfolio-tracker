"""本地磁盘缓存: 行情/基本面持久化到 SQLite, 避免重复网络请求."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

import pandas as pd

from .paths import var_dir

if TYPE_CHECKING:
    from .prices import Quote


# 行情缓存落在数据目录 var/ (安装位置无关); TRACKER_CACHE_DB 可显式指定
# (测试隔离 / 只读挂载场景), 缺省 var/quotes_cache.db
CACHE_DB = (
    Path(os.environ["TRACKER_CACHE_DB"])
    if os.environ.get("TRACKER_CACHE_DB")
    else var_dir() / "quotes_cache.db"
)
CACHE_TTL = 300  # 秒, 实时行情缓存有效期 (新鲜期)
# 陈旧期: 超过新鲜期但未离谱的行情仍可先用后刷 (首屏 stale-while-revalidate)。
# 首屏渲染用 STALE_TTL 读盘秒开, 后台线程再按 CACHE_TTL 的新鲜期重取;
# 冷启动 (两档都 miss) 才阻塞等待。非美股行情本身延迟 15-30 分钟, 30 分钟
# 陈旧数据对「先看到数」是可接受的代价, 且刷新完成即换新。
STALE_TTL = 1800  # 秒, 实时行情陈旧可渲染期 (30 分钟)
OHLC_TTL = 1800  # 秒, 日线K线缓存有效期 (30 分钟; 收盘后数据不变, TTL 过期重取也准)
# 财经日历整批缓存 (cn 报表期 / 长桥按月): 与单票正缓存同口径 (events.CAL_TTL)
CALENDAR_TTL = 21600  # 秒 (6 小时)

# 汇率即期缓存 (base→currency): 与行情同样的新鲜/陈旧两档。CFETS 一次拉齐
# CNY 货币对表, 但 akshare 首次 import alone 就要 ~10s, 冷进程每屏都付这笔钱;
# 落盘后首屏秒取, 后台再刷新。
FX_FRESH_TTL = CACHE_TTL  # 300s
FX_STALE_TTL = STALE_TTL  # 1800s

_lock = Lock()


def _create_schema(con) -> None:
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
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fx_rates (
            base TEXT NOT NULL,
            currency TEXT NOT NULL,
            rate REAL NOT NULL,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (base, currency)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS series_cache (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
        """
    )
    # 财经日历 (财报/分红事件): key 前缀区分来源 (g:<symbol> 单票 / cn:<period> /
    # lp:<market>:<category>:<month> 整批), 载荷均为 {"events": [...]}
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_cache (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
        """
    )
    try:
        con.execute("ALTER TABLE fundamentals ADD COLUMN market_cap REAL")
    except sqlite3.OperationalError:
        pass
    con.commit()


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    """该异常是否真的表示库文件损坏 (而不是锁竞争/磁盘 I/O 等瞬时错误).

    OperationalError("database is locked") 也是 DatabaseError: 早期实现把
    并发窗口里的锁超时当成损坏, 直接把另一个进程正在写的库 os.replace 隔离,
    缓存被劈成两份。只有 CORRUPT/NOTADB 才走隔离重建。
    """
    name = getattr(exc, "sqlite_errorname", "") or ""
    if name in ("SQLITE_CORRUPT", "SQLITE_NOTADB"):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in ("not a database", "malformed", "corrupt"))


def _ensure_db() -> None:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(CACHE_DB) as con:
            _create_schema(con)
    except sqlite3.DatabaseError as exc:
        if not _is_corruption(exc):
            # 锁/IO 等瞬时错误: 不隔离正在使用的库, 交给本次读写自行解释
            # (读路径通常仍可继续, 写路径会抛出真实错误)
            return
        # 缓存库损坏 (写入中途断电/磁盘满/误写文本): 行情/K线/基本面缓存全是可再生
        # 的派生数据。若不处理, snapshot/quote/kline 与 cache info/clear 全链路都会
        # 因一个坏文件裸 traceback —— 连用户想「cache clear 自愈」的那条路也崩。
        # 备份损坏文件并重建空库, 缓存自然重取。
        stamp = int(time.time())
        quarantined = False
        try:
            os.replace(CACHE_DB, CACHE_DB.with_name(f"{CACHE_DB.name}.corrupt-{stamp}"))
            quarantined = True
        except OSError:
            try:
                CACHE_DB.unlink()
            except OSError:
                pass
        try:
            with sqlite3.connect(CACHE_DB) as con:
                _create_schema(con)
        except sqlite3.DatabaseError:
            # 重建仍失败 (如目录只读/磁盘满): 放弃建库, 让调用方按「无缓存」继续
            if quarantined:
                try:
                    CACHE_DB.with_name(
                        f"{CACHE_DB.name}.corrupt-{stamp}"
                    ).replace(CACHE_DB)
                except OSError:
                    pass


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
            float(bv)
            if isinstance(bv, (int, float)) and math.isfinite(bv) and bv > 0
            else None
        ),
        "market_cap": (
            float(mc)
            if isinstance(mc, (int, float)) and math.isfinite(mc) and mc > 0
            else None
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
        # 与 storage._clean_rows 同口径: ±Infinity 同样不落库 (json 不认, 且
        # 6h 负缓存期内会在视图/snapshot --json 里炸出非法 JSON)
        if v is None or not math.isfinite(v) or v <= 0:
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


def get_cached_with_age(
    symbols: list[str], ttl: int = STALE_TTL
) -> tuple[dict[str, "Quote"], dict[str, float]]:
    """返回 (Symbol→Quote, Symbol→age秒) 两个字典; 未命中/过期/脏数据不在返回值中.

    age = now - fetched_at (秒). 首屏 stale-while-revalidate 按 age 分档:
    age ≤ CACHE_TTL 视为新鲜 (渲染即最终结果), 超过则视为陈旧 (先渲染,
    同时后台重取)。ttl 控制最长可接受年龄 (默认陈旧期)。
    """
    from .prices import Quote

    if not symbols:
        return {}, {}

    _ensure_db()
    now = time.time()
    hits: dict[str, Quote] = {}
    ages: dict[str, float] = {}
    with sqlite3.connect(CACHE_DB) as con:
        rows = con.execute(
            f"SELECT symbol, name, price, prev_close, change_pct, currency, book_value, market_cap, source, fetched_at "
            f"FROM quotes "
            f"WHERE symbol IN ({','.join('?' * len(symbols))}) AND fetched_at > ?",
            [*symbols, now - ttl],
        ).fetchall()
        for sym, name, price, prev, chg, ccy, bv, mc, src, fetched_at in rows:
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
                ages[sym] = max(0.0, now - float(fetched_at or now))
    return hits, ages


def get_cached(symbols: list[str], ttl: int = CACHE_TTL) -> dict[str, "Quote"]:
    """返回缓存命中的 Symbol→Quote 字典, 未命中者不在返回值中."""
    hits, _ages = get_cached_with_age(symbols, ttl=ttl)
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


# ---------- 汇率 (即期) 缓存 ----------


def get_fx_cached(
    base: str, currencies, ttl: int = FX_STALE_TTL
) -> tuple[dict[str, float], dict[str, float]]:
    """返回 (currency→rate, currency→age秒); base 自身/未命中/过期不在返回值中.

    与 get_cached_with_age 同构: 首屏按 age 分新鲜/陈旧两档处理。
    """
    base = str(base).upper()
    curs = [
        c
        for c in dict.fromkeys(str(x).upper() for x in currencies)
        if c != base
    ]
    if not curs:
        return {}, {}

    _ensure_db()
    now = time.time()
    rates: dict[str, float] = {}
    ages: dict[str, float] = {}
    with sqlite3.connect(CACHE_DB) as con:
        rows = con.execute(
            f"SELECT currency, rate, fetched_at FROM fx_rates "
            f"WHERE base=? AND currency IN ({','.join('?' * len(curs))}) AND fetched_at > ?",
            [base, *curs, now - ttl],
        ).fetchall()
        for ccy, rate, fetched_at in rows:
            if rate is None or rate <= 0:
                continue
            rates[ccy] = float(rate)
            ages[ccy] = max(0.0, now - float(fetched_at or now))
    return rates, ages


def set_fx_cached(base: str, rates: dict[str, float]) -> None:
    """写入汇率缓存 (base→currency), 刷新 fetched_at; 无效值 (<=0) 与 base 跳过."""
    base = str(base).upper()
    rows = [
        (base, str(c).upper(), float(r), time.time())
        for c, r in rates.items()
        if r and float(r) > 0 and str(c).upper() != base
    ]
    if not rows:
        return
    _ensure_db()
    with _lock, sqlite3.connect(CACHE_DB) as con:
        con.executemany(
            """
            INSERT INTO fx_rates (base, currency, rate, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(base, currency) DO UPDATE SET
                rate=excluded.rate, fetched_at=excluded.fetched_at
            """,
            rows,
        )
        con.commit()


def clear_fx() -> int:
    """清汇率磁盘缓存: 数据源偏好 (IBKR 开关) 变化后强制按新链路重取."""
    _ensure_db()
    with _lock, sqlite3.connect(CACHE_DB) as con:
        n = con.execute("DELETE FROM fx_rates").rowcount
        con.commit()
        return n


def clear_quotes() -> int:
    """只清实时行情缓存 (保留 K线/基本面): 数据源偏好切换后强制按新源链重取."""
    _ensure_db()
    with _lock, sqlite3.connect(CACHE_DB) as con:
        n = con.execute("DELETE FROM quotes").rowcount
        con.commit()
        return n


def clear_ohlc() -> int:
    """只清 K线(OHLC) 磁盘缓存: 数据源偏好切换后强制按新源链重取K线.

    OHLC 缓存键只有 (symbol, months), 不含数据源偏好; 切换 prefer_akshare /
    IBKR / 长桥 / crypto_source 后不清会命中上一个源的旧K线。与 clear_quotes
    配套, 由设置页在源变化时调用 (clear 全清亦可, 但会连基本面一起清)。
    """
    _ensure_db()
    with _lock, sqlite3.connect(CACHE_DB) as con:
        n = con.execute("DELETE FROM ohlc_cache").rowcount
        con.commit()
        return n


# ---------- 财经日历 (财报/分红事件) 缓存 ----------


def get_calendar_row(key: str) -> dict | None:
    """读取日历缓存行; 未命中/载荷损坏返回 None.

    返回 {"events": [...], "fetched_at": float}。TTL 判定留给调用方:
    单票缓存按「载荷是否为空」区分正/负缓存两个 TTL (events.py), 整批缓
    存统一 6h —— 这里不套 WHERE fetched_at, 只给原始时间戳。
    """
    _ensure_db()
    with sqlite3.connect(CACHE_DB) as con:
        row = con.execute(
            "SELECT payload, fetched_at FROM calendar_cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            return None
    except Exception:
        return None
    return {"events": events, "fetched_at": row[1]}


def set_calendar_row(key: str, events: list) -> None:
    """写入日历缓存行; 空事件列表同样落库 (单票负缓存依赖空载荷)."""
    _ensure_db()
    payload = json.dumps({"events": events}, ensure_ascii=False, default=str)
    with _lock, sqlite3.connect(CACHE_DB) as con:
        con.execute(
            """
            INSERT INTO calendar_cache (key, payload, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                payload=excluded.payload, fetched_at=excluded.fetched_at
            """,
            (key, payload, time.time()),
        )
        con.commit()


# ---------- 通用序列缓存 (历史市值 em / 历史股本 shares 用) ----------


def get_series_cached(key: str, ttl: int) -> pd.DataFrame | None:
    """读取缓存的 (date, close) 序列; 未命中/过期/载荷损坏返回 None.

    与 OHLC 缓存同构: 整段 JSON 载荷按 key 覆盖, TTL 过期重取。
    """
    _ensure_db()
    with sqlite3.connect(CACHE_DB) as con:
        row = con.execute(
            "SELECT payload FROM series_cache WHERE key=? AND fetched_at > ?",
            (key, time.time() - ttl),
        ).fetchone()
    if row is None:
        return None
    try:
        df = pd.DataFrame(json.loads(row[0]))
        if df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def set_series_cached(key: str, df: pd.DataFrame) -> None:
    """写入 (date, close) 序列缓存; 空表跳过 (不落库空载荷)."""
    if df is None or df.empty:
        return
    _ensure_db()
    recs = df[["date", "close"]].copy()
    recs["date"] = pd.to_datetime(recs["date"]).dt.strftime("%Y-%m-%d")
    payload = json.dumps(recs.to_dict(orient="records"))
    with _lock, sqlite3.connect(CACHE_DB) as con:
        con.execute(
            """
            INSERT INTO series_cache (key, payload, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                payload=excluded.payload, fetched_at=excluded.fetched_at
            """,
            (key, payload, time.time()),
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
        # 陈旧期内的条数: 首屏可先渲染后刷新 (stale-while-revalidate)
        usable = con.execute(
            "SELECT COUNT(*) FROM quotes WHERE fetched_at > ?", (now - STALE_TTL,)
        ).fetchone()[0]
        ohlc_total = con.execute("SELECT COUNT(*) FROM ohlc_cache").fetchone()[0]
        ohlc_fresh = con.execute(
            "SELECT COUNT(*) FROM ohlc_cache WHERE fetched_at > ?", (now - OHLC_TTL,)
        ).fetchone()[0]
        fx_total = con.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0]
        fx_fresh = con.execute(
            "SELECT COUNT(*) FROM fx_rates WHERE fetched_at > ?", (now - FX_FRESH_TTL,)
        ).fetchone()[0]
        oldest, newest = con.execute(
            "SELECT MIN(fetched_at), MAX(fetched_at) FROM quotes"
        ).fetchone()
        series_total = con.execute("SELECT COUNT(*) FROM series_cache").fetchone()[0]
        series_fresh = con.execute(
            "SELECT COUNT(*) FROM series_cache WHERE fetched_at > ?",
            (now - OHLC_TTL,),
        ).fetchone()[0]
        cal_total = con.execute("SELECT COUNT(*) FROM calendar_cache").fetchone()[0]
    size = CACHE_DB.stat().st_size if CACHE_DB.exists() else 0
    return {
        "db": str(CACHE_DB),
        "ttl_seconds": CACHE_TTL,
        "stale_ttl_seconds": STALE_TTL,
        "total": total,
        "fresh": fresh,
        "stale": total - fresh,
        "usable": usable,
        "fx_ttl_seconds": FX_FRESH_TTL,
        "fx_stale_ttl_seconds": FX_STALE_TTL,
        "fx_total": fx_total,
        "fx_fresh": fx_fresh,
        "ohlc_ttl_seconds": OHLC_TTL,
        "ohlc_total": ohlc_total,
        "ohlc_fresh": ohlc_fresh,
        "oldest_at": oldest or 0.0,
        "newest_at": newest or 0.0,
        "series_total": series_total,
        "series_fresh": series_fresh,
        "calendar_total": cal_total,
        "calendar_ttl_seconds": CALENDAR_TTL,
        "size_bytes": size,
    }


def clear() -> int:
    """清空全部缓存 (行情 + K线 + 基本面 + 汇率), 返回删除条数."""
    _ensure_db()
    with _lock, sqlite3.connect(CACHE_DB) as con:
        n_quotes = con.execute("DELETE FROM quotes").rowcount
        n_ohlc = con.execute("DELETE FROM ohlc_cache").rowcount
        n_fx = con.execute("DELETE FROM fx_rates").rowcount
        n_series = con.execute("DELETE FROM series_cache").rowcount
        n_cal = con.execute("DELETE FROM calendar_cache").rowcount
        con.commit()
        n_fund = con.execute("DELETE FROM fundamentals").rowcount
        return n_quotes + n_ohlc + n_fund + n_fx + n_series + n_cal
